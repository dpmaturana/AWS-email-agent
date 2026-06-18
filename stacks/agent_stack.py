import aws_cdk as cdk
from aws_cdk import (
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_bedrock as bedrock,
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
                        actions=["bedrock:InvokeModel"],
                        resources=["arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-6"],
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
            foundation_model="anthropic.claude-sonnet-4-6",
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
            foundation_model="anthropic.claude-sonnet-4-6",
            instruction=waiver_system_prompt,
            guardrail_configuration=bedrock.CfnAgent.GuardrailConfigurationProperty(
                guardrail_identifier=self.guardrail.attr_guardrail_id,
                guardrail_version=guardrail_version.attr_version,
            ),
            auto_prepare=True,
            # Person 2: add action groups here once tool Lambdas are defined
        )

        waiver_alias = bedrock.CfnAgentAlias(
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
