import aws_cdk as cdk
from aws_cdk import (
    aws_opensearchserverless as oss,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_bedrock as bedrock,
)
from constructs import Construct
import json


class RagStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, infra, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------ #
        # OPENSEARCH SERVERLESS — vector store
        # ------------------------------------------------------------------ #

        encryption_policy = oss.CfnSecurityPolicy(
            self, "OSSEncryptionPolicy",
            name="email-agent-enc",
            type="encryption",
            policy=json.dumps({
                "Rules": [{"Resource": ["collection/email-agent-kb"], "ResourceType": "collection"}],
                "AWSOwnedKey": True,
            }),
        )

        network_policy = oss.CfnSecurityPolicy(
            self, "OSSNetworkPolicy",
            name="email-agent-net",
            type="network",
            policy=json.dumps([{
                "Rules": [
                    {"Resource": ["collection/email-agent-kb"], "ResourceType": "collection"},
                    {"Resource": ["collection/email-agent-kb"], "ResourceType": "dashboard"},
                ],
                "AllowFromPublic": True,
            }]),
        )

        self.oss_collection = oss.CfnCollection(
            self, "KnowledgeBaseCollection",
            name="email-agent-kb",
            type="VECTORSEARCH",
        )
        self.oss_collection.add_dependency(encryption_policy)
        self.oss_collection.add_dependency(network_policy)

        # ------------------------------------------------------------------ #
        # IAM ROLE FOR BEDROCK KNOWLEDGE BASE
        # ------------------------------------------------------------------ #

        kb_role = iam.Role(
            self, "KnowledgeBaseRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
            inline_policies={
                "KBPolicy": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=["s3:GetObject", "s3:ListBucket"],
                        resources=[
                            infra.documents_bucket.bucket_arn,
                            f"{infra.documents_bucket.bucket_arn}/*",
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["aoss:APIAccessAll"],
                        resources=[self.oss_collection.attr_arn],
                    ),
                    iam.PolicyStatement(
                        actions=["bedrock:InvokeModel"],
                        resources=["arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2:0"],
                    ),
                ])
            },
        )

        # ------------------------------------------------------------------ #
        # BEDROCK KNOWLEDGE BASE
        # Person 3: the KB is created here. Your job is to implement the
        # query_knowledge_base Lambda in lambdas/rag/handler.py
        # ------------------------------------------------------------------ #

        self.knowledge_base = bedrock.CfnKnowledgeBase(
            self, "EmailAgentKnowledgeBase",
            name="email-agent-kb",
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn="arn:aws:bedrock:eu-west-1::foundation-model/amazon.titan-embed-text-v2:0",
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="OPENSEARCH_SERVERLESS",
                opensearch_serverless_configuration=bedrock.CfnKnowledgeBase.OpenSearchServerlessConfigurationProperty(
                    collection_arn=self.oss_collection.attr_arn,
                    vector_index_name="email-agent-index",
                    field_mapping=bedrock.CfnKnowledgeBase.OpenSearchServerlessFieldMappingProperty(
                        vector_field="embedding",
                        text_field="text",
                        metadata_field="metadata",
                    ),
                ),
            ),
        )

        # Data source per department
        for dept in ["hr", "legal", "it", "general"]:
            bedrock.CfnDataSource(
                self, f"DataSource{dept.upper()}",
                name=f"email-agent-{dept}",
                knowledge_base_id=self.knowledge_base.attr_knowledge_base_id,
                data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                    type="S3",
                    s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                        bucket_arn=infra.documents_bucket.bucket_arn,
                        inclusion_prefixes=[f"{dept}/"],
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

        # ------------------------------------------------------------------ #
        # RETRIEVAL LAMBDA — query_knowledge_base tool implementation
        # Person 3: implement the retrieval logic in lambdas/rag/handler.py
        # ------------------------------------------------------------------ #

        rag_role = iam.Role(
            self, "RagLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
            inline_policies={
                "BedrockKB": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=["bedrock:Retrieve"],
                        resources=[self.knowledge_base.attr_knowledge_base_id],
                    ),
                ])
            },
        )

        self.rag_lambda = lambda_.Function(
            self, "RagLambda",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/rag"),
            role=rag_role,
            timeout=cdk.Duration.seconds(30),
            environment={
                "KNOWLEDGE_BASE_ID": self.knowledge_base.attr_knowledge_base_id,
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        # ------------------------------------------------------------------ #
        # OUTPUTS
        # ------------------------------------------------------------------ #

        cdk.CfnOutput(self, "KnowledgeBaseId",
            value=self.knowledge_base.attr_knowledge_base_id,
            export_name="KnowledgeBaseId",
        )
        cdk.CfnOutput(self, "RagLambdaArn",
            value=self.rag_lambda.function_arn,
            export_name="RagLambdaArn",
        )
