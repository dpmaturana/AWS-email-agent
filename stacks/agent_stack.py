import aws_cdk as cdk
from aws_cdk import (
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_bedrock as bedrock,
    aws_bedrockagentcore as agentcore,
    aws_s3_assets as s3_assets,
    aws_ssm as ssm,
)
from constructs import Construct


class AgentStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, infra, rag, waiver, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------ #
        # BEDROCK GUARDRAILS — applied to both agents
        # ------------------------------------------------------------------ #

        self.guardrail = bedrock.CfnGuardrail(
            self, "EmailAgentGuardrail",
            name="email-agent-guardrail",
            blocked_input_messaging="This request contains content that cannot be processed.",
            blocked_outputs_messaging="The response was blocked due to content policy.",
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        type="CREDIT_DEBIT_CARD_NUMBER", action="BLOCK"
                    ),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        type="EMAIL", action="ANONYMIZE"
                    ),
                ],
            ),
            topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(
                topics_config=[
                    bedrock.CfnGuardrail.TopicConfigProperty(
                        name="legal_advice",
                        definition="Legal advice or legal interpretations of contracts or regulations",
                        examples=["Is this contract enforceable?", "Am I liable for this?"],
                        type="DENY",
                    ),
                    bedrock.CfnGuardrail.TopicConfigProperty(
                        name="financial_advice",
                        definition="Financial recommendations or investment advice",
                        examples=["Should I invest in this?", "What stocks should I buy?"],
                        type="DENY",
                    ),
                ],
            ),
        )

        guardrail_version = bedrock.CfnGuardrailVersion(
            self, "GuardrailVersion",
            guardrail_identifier=self.guardrail.attr_guardrail_id,
        )

        # ------------------------------------------------------------------ #
        # IAM ROLE FOR BOTH AGENTS
        # ------------------------------------------------------------------ #

        agent_role = iam.Role(
            self, "AgentRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
            inline_policies={
                "AgentPolicy": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                        resources=[
                            "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-6",
                            f"arn:aws:bedrock:{self.region}::foundation-model/eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["bedrock:ApplyGuardrail"],
                        resources=[self.guardrail.attr_guardrail_arn],
                    ),
                    iam.PolicyStatement(
                        actions=["lambda:InvokeFunction"],
                        resources=[
                            rag.rag_lambda.function_arn,
                            waiver.start_waiver_lambda.function_arn,
                            waiver.update_waiver_lambda.function_arn,
                            waiver.get_waiver_lambda.function_arn,
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["ses:SendEmail", "ses:SendRawEmail"],
                        resources=["*"],
                    ),
                    iam.PolicyStatement(
                        actions=["s3:GetObject"],
                        resources=[f"{infra.waiver_criteria_bucket.bucket_arn}/*"],
                    ),
                ])
            },
        )

        # ------------------------------------------------------------------ #
        # AGENT 1 — EMAIL ROUTER
        # Person 2: implement the agent logic in lambdas/agents/router/handler.py
        # ------------------------------------------------------------------ #

        # --- Person 2: replace the system prompt below with your final version ---
        router_system_prompt = """You are an email routing agent for a multi-department organization.
Your job is to:
1. Read the incoming email and identify which department it belongs to (hr, legal, it, general)
2. Classify the email intent: forward, rag, or waiver
3. Execute the appropriate action using your available tools

Always be concise and professional. Never provide legal or financial advice.
Never share PII from one user with another."""

        self.router_agent = bedrock.CfnAgent(
            self, "RouterAgent",
            agent_name="email-router-agent",
            agent_resource_role_arn=agent_role.role_arn,
            foundation_model="eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
            instruction=router_system_prompt,
            guardrail_configuration=bedrock.CfnAgent.GuardrailConfigurationProperty(
                guardrail_identifier=self.guardrail.attr_guardrail_id,
                guardrail_version=guardrail_version.attr_version,
            ),
            auto_prepare=True,
            # Person 2: add action groups here once tool Lambdas are defined
        )

        router_alias = bedrock.CfnAgentAlias(
            self, "RouterAgentAlias",
            agent_id=self.router_agent.attr_agent_id,
            agent_alias_name="live",
        )

        # ------------------------------------------------------------------ #
        # AGENT 2 — WAIVER PROCESSOR
        # Person 2: implement the agent logic in lambdas/agents/waiver/handler.py
        # ------------------------------------------------------------------ #

        waiver_system_prompt = """You are a waiver processing agent.
Your job is to manage the full lifecycle of a waiver request:
1. Identify the type of waiver from the email content
2. Fetch the criteria required for that waiver type
3. Check if all required information and documents are present
4. If incomplete: request the missing information from the user
5. If complete: start the approval workflow for human review

Be precise about what information is missing. Always address the user by name.
Never approve or reject a waiver yourself — that decision belongs to a human approver."""

        self.waiver_agent = bedrock.CfnAgent(
            self, "WaiverAgent",
            agent_name="waiver-processor-agent",
            agent_resource_role_arn=agent_role.role_arn,
            foundation_model="eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
            instruction=waiver_system_prompt,
            guardrail_configuration=bedrock.CfnAgent.GuardrailConfigurationProperty(
                guardrail_identifier=self.guardrail.attr_guardrail_id,
                guardrail_version=guardrail_version.attr_version,
            ),
            auto_prepare=True,
            # Person 2: add action groups here once tool Lambdas are defined
        )

        _waiver_alias = bedrock.CfnAgentAlias(
            self, "WaiverAgentAlias",
            agent_id=self.waiver_agent.attr_agent_id,
            agent_alias_name="live",
        )

        # ------------------------------------------------------------------ #
        # PUBLISH AGENT IDs FOR THE INGESTION LAMBDA
        # Published to SSM (by convention name) rather than injected directly
        # into the ingestion Lambda's environment. Mutating InfraStack's Lambda
        # from here would make InfraStack depend on AgentStack and create a
        # cyclic stack reference (InfraStack -> AgentStack -> RagStack ->
        # InfraStack). The ingestion Lambda reads these parameters at runtime.
        # ------------------------------------------------------------------ #

        ssm.StringParameter(
            self, "RouterAgentIdParam",
            parameter_name=self.node.try_get_context("router_agent_id_param"),
            string_value=self.router_agent.attr_agent_id,
        )
        ssm.StringParameter(
            self, "RouterAgentAliasParam",
            parameter_name=self.node.try_get_context("router_agent_alias_param"),
            string_value=router_alias.attr_agent_alias_id,
        )

        # ------------------------------------------------------------------ #
        # STRANDS AGENTS — Lambda-hosted (the actual agentic logic)
        # ------------------------------------------------------------------ #

        email_from = self.node.try_get_context("email_from")
        # ARN of the waiver agent deployed on Amazon Bedrock AgentCore (created
        # out-of-band by scripts/deploy_agentcore.sh). When set, the router
        # invokes the AgentCore runtime instead of the waiver Lambda.
        waiver_runtime_arn = self.node.try_get_context("waiver_agent_runtime_arn") or ""

        strands_layer = lambda_.LayerVersion(
            self, "StrandsAgentsLayer",
            code=lambda_.Code.from_asset("layers/strands"),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="strands-agents library",
        )

        # Waiver Agent Lambda — processes waiver requests end-to-end
        waiver_agent_role = iam.Role(
            self, "WaiverAgentLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
            inline_policies={
                "WaiverAgentPolicy": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=["ses:SendEmail", "ses:SendRawEmail"],
                        resources=["*"],
                    ),
                    iam.PolicyStatement(
                        actions=["s3:GetObject"],
                        resources=[f"{infra.waiver_criteria_bucket.bucket_arn}/*"],
                    ),
                    iam.PolicyStatement(
                        actions=["lambda:InvokeFunction"],
                        resources=[
                            waiver.start_waiver_lambda.function_arn,
                            waiver.update_waiver_lambda.function_arn,
                            waiver.get_waiver_lambda.function_arn,
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                        resources=[
                            "arn:aws:bedrock:*::foundation-model/amazon.nova-pro-v1:0",
                            f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/eu.amazon.nova-pro-v1:0",
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["bedrock:ApplyGuardrail"],
                        resources=[self.guardrail.attr_guardrail_arn],
                    ),
                ])
            },
        )

        self.waiver_agent_lambda = lambda_.Function(
            self, "WaiverAgentLambda",
            function_name="email-agent-waiver",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="waiver_handler.handler",
            code=lambda_.Code.from_asset("lambdas/agents/waiver"),
            role=waiver_agent_role,
            layers=[strands_layer],
            timeout=cdk.Duration.seconds(300),
            memory_size=512,
            environment={
                "EMAIL_FROM":              email_from,
                "WAIVER_CRITERIA_BUCKET":  infra.waiver_criteria_bucket.bucket_name,
                "START_WAIVER_LAMBDA_ARN": waiver.start_waiver_lambda.function_arn,
                "UPDATE_WAIVER_LAMBDA_ARN": waiver.update_waiver_lambda.function_arn,
                "GET_WAIVER_LAMBDA_ARN":   waiver.get_waiver_lambda.function_arn,
                "GUARDRAIL_ID":            self.guardrail.attr_guardrail_id,
                "GUARDRAIL_VERSION":       guardrail_version.attr_version,
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        # Router Agent Lambda — classifies emails and routes them
        router_agent_role = iam.Role(
            self, "RouterAgentLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
            inline_policies={
                "RouterAgentPolicy": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=["ses:SendEmail", "ses:SendRawEmail"],
                        resources=["*"],
                    ),
                    iam.PolicyStatement(
                        actions=["s3:PutObject"],
                        resources=[
                            f"{infra.raw_emails_bucket.bucket_arn}/routed/*",
                            f"{infra.raw_emails_bucket.bucket_arn}/responses/*",
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["lambda:InvokeFunction"],
                        resources=[
                            rag.rag_lambda.function_arn,
                            self.waiver_agent_lambda.function_arn,
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["bedrock-agentcore:InvokeAgentRuntime"],
                        resources=[
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/*",
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                        resources=[
                            "arn:aws:bedrock:*::foundation-model/amazon.nova-pro-v1:0",
                            f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/eu.amazon.nova-pro-v1:0",
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["bedrock:ApplyGuardrail"],
                        resources=[self.guardrail.attr_guardrail_arn],
                    ),
                ])
            },
        )

        self.router_lambda = lambda_.Function(
            self, "RouterAgentLambda",
            function_name="email-agent-router",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/agents/router"),
            role=router_agent_role,
            layers=[strands_layer],
            timeout=cdk.Duration.seconds(300),
            memory_size=512,
            environment={
                "EMAIL_FROM":               email_from,
                "RAG_LAMBDA_ARN":           rag.rag_lambda.function_arn,
                "WAIVER_AGENT_LAMBDA_ARN":  self.waiver_agent_lambda.function_arn,
                "WAIVER_AGENT_RUNTIME_ARN": waiver_runtime_arn,
                "RAW_EMAILS_BUCKET":        infra.raw_emails_bucket.bucket_name,
                "GUARDRAIL_ID":             self.guardrail.attr_guardrail_id,
                "GUARDRAIL_VERSION":        guardrail_version.attr_version,
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        # Publish router Lambda ARN to SSM so InfraStack can read it at runtime
        ssm.StringParameter(
            self, "RouterLambdaArnParam",
            parameter_name="/email-agent/router/lambda-arn",
            string_value=self.router_lambda.function_arn,
        )

        # ------------------------------------------------------------------ #
        # AMAZON BEDROCK AGENTCORE RUNTIMES (pure CDK — both Strands agents)
        # Code is shipped as an S3 asset (build/ac_*, built by
        # scripts/build_agentcore_assets.sh — ARM64 wheels, no Docker) and
        # registered via CfnRuntime's code_configuration (direct code deploy).
        # ------------------------------------------------------------------ #

        region, account = self.region, self.account
        # Baseline execution-role permissions every AgentCore runtime needs
        # (logs, tracing, workload identity, Bedrock) — mirrors the policy the
        # AgentCore starter toolkit creates for an auto-provisioned role.
        agentcore_baseline = [
            iam.PolicyStatement(actions=["logs:DescribeLogStreams", "logs:CreateLogGroup"],
                resources=[f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*"]),
            iam.PolicyStatement(actions=["logs:DescribeLogGroups"],
                resources=[f"arn:aws:logs:{region}:{account}:log-group:*"]),
            iam.PolicyStatement(actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"]),
            iam.PolicyStatement(actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords",
                                         "xray:GetSamplingRules", "xray:GetSamplingTargets"], resources=["*"]),
            iam.PolicyStatement(actions=["cloudwatch:PutMetricData"], resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}),
            iam.PolicyStatement(actions=["bedrock-agentcore:GetResourceApiKey",
                                         "bedrock-agentcore:GetResourceOauth2Token",
                                         "bedrock-agentcore:CreateWorkloadIdentity",
                                         "bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
                resources=[f"arn:aws:bedrock-agentcore:{region}:{account}:token-vault/default",
                           f"arn:aws:bedrock-agentcore:{region}:{account}:token-vault/default/*",
                           f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default",
                           f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default/workload-identity/*"]),
            iam.PolicyStatement(actions=["secretsmanager:GetSecretValue"],
                resources=[f"arn:aws:secretsmanager:{region}:{account}:secret:bedrock-agentcore-identity!default/*"]),
            iam.PolicyStatement(actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
                                         "bedrock:ApplyGuardrail"],
                resources=["arn:aws:bedrock:*::foundation-model/*",
                           "arn:aws:bedrock:*:*:inference-profile/*",
                           f"arn:aws:bedrock:{region}:{account}:*"]),
            iam.PolicyStatement(actions=["sts:GetWebIdentityToken"], resources=["*"]),
        ]

        def _runtime_role(cid, tool_statements):
            role = iam.Role(self, cid,
                assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com",
                    conditions={
                        "StringEquals": {"aws:SourceAccount": account},
                        "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account}:*"},
                    }),
            )
            for st in agentcore_baseline + tool_statements:
                role.add_to_policy(st)
            return role

        def _code_config(asset):
            return agentcore.CfnRuntime.CodeConfigurationProperty(
                code=agentcore.CfnRuntime.CodeProperty(
                    s3=agentcore.CfnRuntime.S3LocationProperty(
                        bucket=asset.s3_bucket_name, prefix=asset.s3_object_key)),
                entry_point=["runtime_app.py"],
                runtime="PYTHON_3_10")

        # --- Waiver Processor runtime ---
        waiver_code = s3_assets.Asset(self, "WaiverAgentCodeAsset", path="build/ac_waiver")
        waiver_runtime_role = _runtime_role("WaiverRuntimeRole", [
            iam.PolicyStatement(actions=["lambda:InvokeFunction"], resources=[
                waiver.start_waiver_lambda.function_arn, waiver.update_waiver_lambda.function_arn,
                waiver.get_waiver_lambda.function_arn]),
            iam.PolicyStatement(actions=["ses:SendEmail", "ses:SendRawEmail"], resources=["*"]),
            iam.PolicyStatement(actions=["s3:GetObject", "s3:ListBucket"], resources=[
                infra.waiver_criteria_bucket.bucket_arn, f"{infra.waiver_criteria_bucket.bucket_arn}/*"]),
        ])
        waiver_code.grant_read(waiver_runtime_role)
        self.waiver_runtime = agentcore.CfnRuntime(self, "WaiverAgentRuntime",
            agent_runtime_name="waiveragentcdk",
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                code_configuration=_code_config(waiver_code)),
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(network_mode="PUBLIC"),
            protocol_configuration="HTTP",
            role_arn=waiver_runtime_role.role_arn,
            environment_variables={
                "EMAIL_FROM":              email_from,
                "WAIVER_CRITERIA_BUCKET":  infra.waiver_criteria_bucket.bucket_name,
                "START_WAIVER_LAMBDA_ARN": waiver.start_waiver_lambda.function_arn,
                "UPDATE_WAIVER_LAMBDA_ARN": waiver.update_waiver_lambda.function_arn,
                "GET_WAIVER_LAMBDA_ARN":   waiver.get_waiver_lambda.function_arn,
                "GUARDRAIL_ID":            self.guardrail.attr_guardrail_id,
                "GUARDRAIL_VERSION":       guardrail_version.attr_version,
            })

        # --- Email Router runtime (delegates to the waiver runtime) ---
        router_code = s3_assets.Asset(self, "RouterAgentCodeAsset", path="build/ac_router")
        router_runtime_role = _runtime_role("RouterRuntimeRole", [
            iam.PolicyStatement(actions=["lambda:InvokeFunction"], resources=[rag.rag_lambda.function_arn]),
            iam.PolicyStatement(actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/*"]),
            iam.PolicyStatement(actions=["ses:SendEmail", "ses:SendRawEmail"], resources=["*"]),
            iam.PolicyStatement(actions=["s3:PutObject"], resources=[
                f"{infra.raw_emails_bucket.bucket_arn}/routed/*",
                f"{infra.raw_emails_bucket.bucket_arn}/responses/*"]),
        ])
        router_code.grant_read(router_runtime_role)
        self.router_runtime = agentcore.CfnRuntime(self, "RouterAgentRuntime",
            agent_runtime_name="routeragentcdk",
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                code_configuration=_code_config(router_code)),
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(network_mode="PUBLIC"),
            protocol_configuration="HTTP",
            role_arn=router_runtime_role.role_arn,
            environment_variables={
                "EMAIL_FROM":               email_from,
                "RAG_LAMBDA_ARN":           rag.rag_lambda.function_arn,
                "WAIVER_AGENT_RUNTIME_ARN": self.waiver_runtime.attr_agent_runtime_arn,
                "RAW_EMAILS_BUCKET":        infra.raw_emails_bucket.bucket_name,
                "GUARDRAIL_ID":             self.guardrail.attr_guardrail_id,
                "GUARDRAIL_VERSION":        guardrail_version.attr_version,
            })

        # Publish the router runtime ARN to SSM so the ingestion Lambda (InfraStack)
        # invokes it without a cyclic stack dependency.
        ssm.StringParameter(self, "RouterRuntimeArnParam",
            parameter_name="/email-agent/router/runtime-arn",
            string_value=self.router_runtime.attr_agent_runtime_arn,
        )

        # ------------------------------------------------------------------ #
        # OUTPUTS
        # ------------------------------------------------------------------ #

        cdk.CfnOutput(self, "RouterAgentId",
            value=self.router_agent.attr_agent_id,
            export_name="RouterAgentId",
        )
        cdk.CfnOutput(self, "WaiverAgentId",
            value=self.waiver_agent.attr_agent_id,
            export_name="WaiverAgentId",
        )
        cdk.CfnOutput(self, "GuardrailId",
            value=self.guardrail.attr_guardrail_id,
            export_name="GuardrailId",
        )
