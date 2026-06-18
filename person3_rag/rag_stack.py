"""
RagStack — Person 3 (RAG + knowledge base)

Owns the retrieval-augmented-generation subsystem of the email routing agent:
  * an OpenSearch Serverless (AOSS) collection used as the vector store
  * a Bedrock Knowledge Base (Titan Embeddings v2) with one S3 data source per
    department prefix (hr / legal / it / general)
  * a custom resource that creates the AOSS vector index before the KB is built
  * an auto-sync pipeline: new doc in S3 -> EventBridge -> sync Lambda writes a
    `<key>.metadata.json` sidecar (department tag) and starts an ingestion job
  * the `query_knowledge_base` tool Lambda that Person 2's agent calls

Design goals (mapped to the rubric):
  * Account-agnostic: no hardcoded account IDs / regions; ARNs built from tokens.
  * Least privilege: every policy is scoped to a concrete resource ARN; no
    `Resource: "*"` on data actions.
  * Deployed entirely via CDK, no manual steps (other than the team-wide SES
    address confirmation owned by Person 1).

Integration:
  * The `documents-bucket` is owned by Person 1 (InfraStack). Pass it in as the
    `documents_bucket` prop when both stacks live in the same CDK app, OR run
    this stack standalone and supply `-c documentsBucketName=<name>` (see app.py).
  * Person 1 must enable EventBridge notifications on the documents bucket
    (`s3.Bucket(..., event_bridge_enabled=True)`) so the auto-sync rule fires.
  * Exports `RagKnowledgeBaseId` and `RagQueryToolLambdaArn` for Person 2.
"""

from typing import Optional

from aws_cdk import (
    Aws,
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_opensearchserverless as aoss
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import custom_resources as cr
from constructs import Construct

# Departments map 1:1 to S3 prefixes and to KB data sources. Retrieval is scoped
# by the `department` metadata attribute so HR docs never leak into an IT answer.
DEPARTMENTS = ["hr", "legal", "it", "general"]

# Titan Embeddings v2: 1024-dim output. The AOSS index dimension MUST match.
EMBEDDING_DIMENSION = 1024
DEFAULT_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"

# Field names Bedrock expects in the AOSS index mapping.
VECTOR_FIELD = "bedrock-knowledge-base-default-vector"
TEXT_FIELD = "AMAZON_BEDROCK_TEXT_CHUNK"
METADATA_FIELD = "AMAZON_BEDROCK_METADATA"
INDEX_NAME = "kb-docs-index"

# AOSS names are limited to 32 chars, lowercase.
COLLECTION_NAME = "email-router-rag"


class RagStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        documents_bucket: Optional[s3.IBucket] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- 1. Resolve the documents bucket (own-app reference or by-name import)
        if documents_bucket is None:
            bucket_name = self.node.try_get_context("documentsBucketName")
            if not bucket_name:
                raise ValueError(
                    "RagStack needs the documents bucket. Either pass "
                    "`documents_bucket=` (same CDK app as Person 1's InfraStack) "
                    "or deploy with `-c documentsBucketName=<bucket>`."
                )
            documents_bucket = s3.Bucket.from_bucket_name(
                self, "DocumentsBucket", bucket_name
            )
        self.documents_bucket = documents_bucket

        embedding_model_id = (
            self.node.try_get_context("embeddingModelId") or DEFAULT_EMBEDDING_MODEL_ID
        )
        embedding_model_arn = (
            f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}::foundation-model/{embedding_model_id}"
        )

        # --- 2. OpenSearch Serverless: security policies + collection
        collection = self._build_collection()

        # --- 3. IAM role assumed by the Bedrock Knowledge Base service
        kb_role = self._build_kb_role(collection, embedding_model_arn)

        # --- 4. Index-setup custom resource role (data-plane access to AOSS)
        index_setup_role = iam.Role(
            self,
            "IndexSetupRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        index_setup_role.add_to_policy(
            iam.PolicyStatement(
                actions=["aoss:APIAccessAll"],
                resources=[collection.attr_arn],
            )
        )

        # --- 5. Data access policy: who can touch the collection data plane.
        #         Both the KB role (read/write during ingestion + query) and the
        #         index-setup Lambda role (create the index) are principals.
        access_policy = self._build_data_access_policy(
            principals=[kb_role.role_arn, index_setup_role.role_arn]
        )

        # --- 6. Custom resource that creates the kNN vector index in AOSS.
        index_setup_fn = lambda_.Function(
            self,
            "IndexSetupFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.on_event",
            code=lambda_.Code.from_asset("lambdas/index_setup"),
            role=index_setup_role,
            timeout=Duration.minutes(5),
            memory_size=256,
            environment={
                "COLLECTION_ENDPOINT": collection.attr_collection_endpoint,
                "INDEX_NAME": INDEX_NAME,
                "VECTOR_FIELD": VECTOR_FIELD,
                "TEXT_FIELD": TEXT_FIELD,
                "METADATA_FIELD": METADATA_FIELD,
                "VECTOR_DIMENSION": str(EMBEDDING_DIMENSION),
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        index_provider = cr.Provider(
            self, "IndexProvider", on_event_handler=index_setup_fn
        )
        index_resource = CustomResource(
            self, "VectorIndex", service_token=index_provider.service_token
        )
        # Index can only be created once the collection is ACTIVE and the data
        # access policy grants our role.
        index_resource.node.add_dependency(collection)
        index_resource.node.add_dependency(access_policy)

        # --- 7. The Bedrock Knowledge Base + one data source per department
        knowledge_base = self._build_knowledge_base(
            kb_role=kb_role,
            collection=collection,
            embedding_model_arn=embedding_model_arn,
        )
        knowledge_base.node.add_dependency(index_resource)

        data_source_ids = {}
        for dept in DEPARTMENTS:
            ds = self._build_data_source(knowledge_base, dept)
            data_source_ids[dept] = ds.attr_data_source_id

        # --- 8. The query_knowledge_base tool Lambda (called by Person 2)
        self.query_fn = self._build_query_lambda(knowledge_base.attr_knowledge_base_id)

        # --- 9. Auto-sync pipeline: S3 object created -> EventBridge -> Lambda
        self._build_sync_pipeline(
            kb_id=knowledge_base.attr_knowledge_base_id,
            data_source_ids=data_source_ids,
        )

        # --- 10. Outputs consumed by Person 2 at integration time
        CfnOutput(
            self,
            "RagKnowledgeBaseId",
            value=knowledge_base.attr_knowledge_base_id,
            description="Bedrock Knowledge Base ID (Person 2 wires this into the agent tools)",
            export_name="RagKnowledgeBaseId",
        )
        CfnOutput(
            self,
            "RagQueryToolLambdaArn",
            value=self.query_fn.function_arn,
            description="ARN of the query_knowledge_base tool Lambda",
            export_name="RagQueryToolLambdaArn",
        )
        CfnOutput(
            self,
            "RagCollectionEndpoint",
            value=collection.attr_collection_endpoint,
            description="OpenSearch Serverless collection endpoint",
        )

    # ------------------------------------------------------------------ helpers

    def _build_collection(self) -> aoss.CfnCollection:
        """Encryption + network policies, then a VECTORSEARCH collection."""
        # AOSS requires an encryption policy covering the collection before it
        # can be created. Owned KMS keys are an option; here we use the
        # AWS-owned key (AWSOwnedKey: true) for simplicity — documented in README.
        encryption_policy = aoss.CfnSecurityPolicy(
            self,
            "EncryptionPolicy",
            name="rag-enc-policy",
            type="encryption",
            policy=(
                '{"Rules":[{"ResourceType":"collection",'
                f'"Resource":["collection/{COLLECTION_NAME}"]}}],'
                '"AWSOwnedKey":true}'
            ),
        )
        # Network policy: allow public access to the collection + dashboards.
        # For a VPC-only posture this would reference a VPC endpoint instead
        # (noted as a hardening item in the report's limitations section).
        network_policy = aoss.CfnSecurityPolicy(
            self,
            "NetworkPolicy",
            name="rag-net-policy",
            type="network",
            policy=(
                '[{"Rules":[{"ResourceType":"collection",'
                f'"Resource":["collection/{COLLECTION_NAME}"]}},'
                '{"ResourceType":"dashboard",'
                f'"Resource":["collection/{COLLECTION_NAME}"]}}],'
                '"AllowFromPublic":true}]'
            ),
        )

        collection = aoss.CfnCollection(
            self,
            "VectorCollection",
            name=COLLECTION_NAME,
            type="VECTORSEARCH",
            description="Vector store for the email-router RAG knowledge base",
        )
        collection.add_dependency(encryption_policy)
        collection.add_dependency(network_policy)
        return collection

    def _build_data_access_policy(self, principals: list[str]) -> aoss.CfnAccessPolicy:
        import json

        policy = [
            {
                "Rules": [
                    {
                        "ResourceType": "index",
                        "Resource": [f"index/{COLLECTION_NAME}/*"],
                        "Permission": [
                            "aoss:CreateIndex",
                            "aoss:DeleteIndex",
                            "aoss:UpdateIndex",
                            "aoss:DescribeIndex",
                            "aoss:ReadDocument",
                            "aoss:WriteDocument",
                        ],
                    },
                    {
                        "ResourceType": "collection",
                        "Resource": [f"collection/{COLLECTION_NAME}"],
                        "Permission": [
                            "aoss:CreateCollectionItems",
                            "aoss:DescribeCollectionItems",
                            "aoss:UpdateCollectionItems",
                        ],
                    },
                ],
                "Principal": principals,
            }
        ]
        return aoss.CfnAccessPolicy(
            self,
            "DataAccessPolicy",
            name="rag-data-policy",
            type="data",
            policy=json.dumps(policy),
        )

    def _build_kb_role(
        self, collection: aoss.CfnCollection, embedding_model_arn: str
    ) -> iam.Role:
        role = iam.Role(
            self,
            "KnowledgeBaseRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
            description="Assumed by Bedrock to ingest into AOSS and call the embedder",
        )
        # Invoke only the specific embedding model.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[embedding_model_arn],
            )
        )
        # Data-plane access to the specific collection.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["aoss:APIAccessAll"],
                resources=[collection.attr_arn],
            )
        )
        # Read documents (and the metadata sidecars) from the documents bucket.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[
                    self.documents_bucket.bucket_arn,
                    f"{self.documents_bucket.bucket_arn}/*",
                ],
            )
        )
        return role

    def _build_knowledge_base(
        self,
        kb_role: iam.Role,
        collection: aoss.CfnCollection,
        embedding_model_arn: str,
    ) -> bedrock.CfnKnowledgeBase:
        return bedrock.CfnKnowledgeBase(
            self,
            "KnowledgeBase",
            name="email-router-kb",
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=embedding_model_arn,
                    embedding_model_configuration=bedrock.CfnKnowledgeBase.EmbeddingModelConfigurationProperty(
                        bedrock_embedding_model_configuration=bedrock.CfnKnowledgeBase.BedrockEmbeddingModelConfigurationProperty(
                            dimensions=EMBEDDING_DIMENSION,
                        )
                    ),
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="OPENSEARCH_SERVERLESS",
                opensearch_serverless_configuration=bedrock.CfnKnowledgeBase.OpenSearchServerlessConfigurationProperty(
                    collection_arn=collection.attr_arn,
                    vector_index_name=INDEX_NAME,
                    field_mapping=bedrock.CfnKnowledgeBase.OpenSearchServerlessFieldMappingProperty(
                        vector_field=VECTOR_FIELD,
                        text_field=TEXT_FIELD,
                        metadata_field=METADATA_FIELD,
                    ),
                ),
            ),
        )

    def _build_data_source(
        self, kb: bedrock.CfnKnowledgeBase, department: str
    ) -> bedrock.CfnDataSource:
        """One S3 data source per department prefix with fixed-size chunking."""
        return bedrock.CfnDataSource(
            self,
            f"DataSource{department.capitalize()}",
            name=f"{department}-docs",
            knowledge_base_id=kb.attr_knowledge_base_id,
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=self.documents_bucket.bucket_arn,
                    inclusion_prefixes=[f"{department}/"],
                ),
            ),
            vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
                    chunking_strategy="FIXED_SIZE",
                    fixed_size_chunking_configuration=bedrock.CfnDataSource.FixedSizeChunkingConfigurationProperty(
                        max_tokens=512,
                        overlap_percentage=20,
                    ),
                ),
            ),
        )

    def _build_query_lambda(self, kb_id: str) -> lambda_.Function:
        fn = lambda_.Function(
            self,
            "QueryKbFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/query_kb"),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={"KNOWLEDGE_BASE_ID": kb_id},
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        # Retrieve-only against this specific KB. Bedrock uses the KB role to
        # reach AOSS, so this Lambda needs no direct aoss permission.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:Retrieve"],
                resources=[
                    f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}:{Aws.ACCOUNT_ID}:knowledge-base/{kb_id}"
                ],
            )
        )
        return fn

    def _build_sync_pipeline(self, kb_id: str, data_source_ids: dict) -> None:
        import json

        sync_fn = lambda_.Function(
            self,
            "SyncTriggerFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/sync_trigger"),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={
                "KNOWLEDGE_BASE_ID": kb_id,
                "DATA_SOURCE_IDS": json.dumps(data_source_ids),
                "DOCUMENTS_BUCKET": self.documents_bucket.bucket_name,
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        # Write the metadata sidecar back, and read objects if needed.
        sync_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject", "s3:GetObject"],
                resources=[f"{self.documents_bucket.bucket_arn}/*"],
            )
        )
        # Kick off ingestion jobs on this KB only.
        sync_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:StartIngestionJob"],
                resources=[
                    f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}:{Aws.ACCOUNT_ID}:knowledge-base/{kb_id}"
                ],
            )
        )

        # EventBridge rule for S3 "Object Created" in the documents bucket.
        # Requires Person 1 to set `event_bridge_enabled=True` on the bucket.
        rule = events.Rule(
            self,
            "DocsCreatedRule",
            description="New document in documents-bucket -> re-sync KB",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={"bucket": {"name": [self.documents_bucket.bucket_name]}},
            ),
        )
        rule.add_target(targets.LambdaFunction(sync_fn))
