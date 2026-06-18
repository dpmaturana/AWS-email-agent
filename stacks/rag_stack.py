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
import boto3


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

        _kb_role_name = "email-agent-kb-role"

        kb_role = iam.Role(
            self, "KnowledgeBaseRole",
            role_name=_kb_role_name,
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
        # AOSS DATA ACCESS POLICY
        # Uses Fn.sub so ${AWS::AccountId} is resolved before reaching AOSS
        # ------------------------------------------------------------------ #

        account_id = boto3.client("sts").get_caller_identity()["Account"]
        # Use the stack's deploy region, NOT boto3's config-default region.
        # The CfnIndex CreateIndex call is made by the cfn-exec-role in the
        # DEPLOY region; if this ARN names a different region the data access
        # policy won't match the caller and AOSS returns AccessDenied.
        region = self.region
        # CFN execution role is what CfnIndex uses to call the AOSS API
        cfn_exec_role = f"arn:aws:iam::{account_id}:role/cdk-hnb659fds-cfn-exec-role-{account_id}-{region}"
        principals = [
            f"arn:aws:iam::{account_id}:role/{_kb_role_name}",
            cfn_exec_role,
        ]

        data_access_policy = oss.CfnAccessPolicy(
            self, "OSSDataAccessPolicy",
            name="email-agent-data",
            type="data",
            policy=json.dumps([{
                "Rules": [
                    {
                        "Resource": ["index/email-agent-kb/*"],
                        "Permission": [
                            "aoss:CreateIndex",
                            "aoss:DeleteIndex",
                            "aoss:UpdateIndex",
                            "aoss:DescribeIndex",
                            "aoss:ReadDocument",
                            "aoss:WriteDocument",
                        ],
                        "ResourceType": "index",
                    },
                    {
                        "Resource": ["collection/email-agent-kb"],
                        "Permission": ["aoss:DescribeCollectionItems"],
                        "ResourceType": "collection",
                    },
                ],
                "Principal": principals,
            }]),
        )

        # AOSS propagates data access policies asynchronously (~60-90 s).
        # This waiter fires once at stack creation to absorb that delay before
        # CfnIndex calls CreateIndex.
        waiter_fn = lambda_.Function(
            self, "OSSPolicyWaiter",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(
                "import time, json, urllib.request\n"
                "def handler(e, c):\n"
                "  status = 'SUCCESS'\n"
                "  try:\n"
                "    if e['RequestType'] == 'Create': time.sleep(90)\n"
                "  except Exception:\n"
                "    status = 'FAILED'\n"
                "  b = json.dumps({'Status':status,'PhysicalResourceId':'waiter',"
                "'StackId':e['StackId'],'RequestId':e['RequestId'],"
                "'LogicalResourceId':e['LogicalResourceId'],'Data':{}}).encode()\n"
                "  urllib.request.urlopen(urllib.request.Request("
                "e['ResponseURL'],data=b,"
                "headers={'content-type':'','content-length':str(len(b))},"
                "method='PUT'))\n"
            ),
            timeout=cdk.Duration.seconds(150),
        )

        waiter_cr = cdk.CustomResource(
            self, "OSSPolicyWaiterCR",
            service_token=waiter_fn.function_arn,
        )
        waiter_cr.node.add_dependency(data_access_policy)

        # ------------------------------------------------------------------ #
        # AOSS INDEX — native CloudFormation resource, no custom resource needed
        # ------------------------------------------------------------------ #

        oss_index = oss.CfnIndex(
            self, "OSSIndex",
            collection_endpoint=self.oss_collection.attr_collection_endpoint,
            index_name="email-agent-index",
            mappings=oss.CfnIndex.MappingsProperty(
                properties={
                    "embedding": oss.CfnIndex.PropertyMappingProperty(
                        type="knn_vector",
                        dimension=1024,
                        method=oss.CfnIndex.MethodProperty(
                            name="hnsw",
                            engine="faiss",
                            space_type="l2",
                            parameters=oss.CfnIndex.ParametersProperty(
                                ef_construction=512,
                                m=16,
                            ),
                        ),
                    ),
                    "text": oss.CfnIndex.PropertyMappingProperty(type="text"),
                    "metadata": oss.CfnIndex.PropertyMappingProperty(type="text"),
                },
            ),
            settings=oss.CfnIndex.IndexSettingsProperty(
                index=oss.CfnIndex.IndexProperty(knn=True),
            ),
        )
        oss_index.add_dependency(self.oss_collection)
        oss_index.node.add_dependency(waiter_cr)

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
        self.knowledge_base.add_dependency(oss_index)

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
                        resources=[self.knowledge_base.attr_knowledge_base_arn],
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