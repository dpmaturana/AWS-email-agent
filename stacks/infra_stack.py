import aws_cdk as cdk
from aws_cdk import (
    aws_s3 as s3,
    aws_s3_notifications as s3n,
    aws_ses as ses,
    aws_ses_actions as ses_actions,
    aws_lambda as lambda_,
    aws_lambda_event_sources as event_sources,
    aws_iam as iam,
    aws_logs as logs,
)
from constructs import Construct


class InfraStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        email_from = self.node.try_get_context("email_from")
        email_demo_recipient = self.node.try_get_context("email_demo_recipient")

        # ------------------------------------------------------------------ #
        # S3 BUCKETS
        # ------------------------------------------------------------------ #

        self.raw_emails_bucket = s3.Bucket(
            self, "RawEmailsBucket",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
        )

        self.documents_bucket = s3.Bucket(
            self, "DocumentsBucket",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
        )

        self.waiver_criteria_bucket = s3.Bucket(
            self, "WaiverCriteriaBucket",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
        )

        # ------------------------------------------------------------------ #
        # SES — verified email identities (sandbox mode, no domain needed)
        # ------------------------------------------------------------------ #
        # Set context "manage_ses_identities": false when the addresses are
        # already verified / owned by another stack in the account (an
        # AWS::SES::EmailIdentity cannot be owned by two stacks at once).
        manage_ses_identities = self.node.try_get_context("manage_ses_identities")
        if manage_ses_identities is None:
            manage_ses_identities = True

        if manage_ses_identities:
            ses.CfnEmailIdentity(
                self, "SenderIdentity",
                email_identity=email_from,
            )

            ses.CfnEmailIdentity(
                self, "RecipientIdentity",
                email_identity=email_demo_recipient,
            )

        # ------------------------------------------------------------------ #
        # INGESTION LAMBDA
        # Person 1: implement the logic in lambdas/ingestion/handler.py
        # ------------------------------------------------------------------ #

        ingestion_role = iam.Role(
            self, "IngestionLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )

        # Read the raw .eml + write extracted attachments back under attachments/.
        self.raw_emails_bucket.grant_read_write(ingestion_role)

        # Thread detection reads Person 4's DynamoDB `waivers` table via the
        # message_id GSI. The table name + index are part of the team contract
        # (see stacks/waiver_stack.py); referenced by name to avoid a circular
        # stack dependency (WaiverStack already depends on InfraStack).
        waiver_table_name = self.node.try_get_context("waiver_table_name") or "waivers"
        waiver_message_id_index = "message_id_index"
        waiver_table_arn = (
            f"arn:aws:dynamodb:{self.region}:{self.account}:table/{waiver_table_name}"
        )
        ingestion_role.add_to_policy(iam.PolicyStatement(
            actions=["dynamodb:Query", "dynamodb:GetItem"],
            resources=[waiver_table_arn, f"{waiver_table_arn}/index/*"],
        ))

        # Invoke the Bedrock router agent. The concrete agent ARN is unknown at
        # InfraStack synth time (created later in AgentStack), so scope to any
        # agent alias in this account/region.
        ingestion_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeAgent"],
            resources=[
                f"arn:aws:bedrock:{self.region}:{self.account}:agent-alias/*",
            ],
        ))

        # The router agent id/alias are published to SSM by AgentStack (decoupled
        # to avoid a cyclic stack dependency). Read them by convention name.
        router_id_param = self.node.try_get_context("router_agent_id_param")
        router_alias_param = self.node.try_get_context("router_agent_alias_param")
        ingestion_role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[
                f"arn:aws:ssm:{self.region}:{self.account}:parameter{router_id_param}",
                f"arn:aws:ssm:{self.region}:{self.account}:parameter{router_alias_param}",
            ],
        ))

        self.ingestion_lambda = lambda_.Function(
            self, "IngestionLambda",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/ingestion"),
            role=ingestion_role,
            timeout=cdk.Duration.seconds(60),
            environment={
                "EMAIL_FROM": email_from,
                "EMAIL_DEMO_RECIPIENT": email_demo_recipient,
                "RAW_EMAILS_BUCKET": self.raw_emails_bucket.bucket_name,
                "WAIVER_TABLE_NAME": waiver_table_name,
                "WAIVER_MESSAGE_ID_INDEX": waiver_message_id_index,
                # Router agent id/alias are resolved at runtime from these SSM
                # parameters (published by AgentStack after it deploys).
                "ROUTER_AGENT_ID_PARAM": router_id_param,
                "ROUTER_AGENT_ALIAS_PARAM": router_alias_param,
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        # ------------------------------------------------------------------ #
        # S3 TRIGGER — auto-invoke ingestion when a raw email lands in the bucket
        # ------------------------------------------------------------------ #
        # Single entry point for BOTH simulated and real inbound:
        #   * simulated: drop an .eml under incoming/ manually
        #   * real (domain): the SES receipt rule below writes incoming/<msgId>
        # Scoped to the incoming/ prefix so the attachments the Lambda writes
        # back (under attachments/) do NOT re-trigger it — that would loop.
        # No suffix filter: SES-stored objects are keyed by message id (no .eml).
        self.raw_emails_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(self.ingestion_lambda),
            s3.NotificationKeyFilter(prefix="incoming/"),
        )

        # ------------------------------------------------------------------ #
        # SES INBOUND (optional) — real email receiving via a verified domain
        # ------------------------------------------------------------------ #
        # Enabled only when context "inbound_domain" is set (e.g. "myteam.click").
        # Requires: domain registered + verified in SES, MX record pointing to
        # SES, and the rule set activated (see docs/inbound-email-setup.md).
        # The rule uses an S3 action ONLY (no Lambda action): SES writes the raw
        # email to incoming/, which fires the S3 trigger above. Using both would
        # double-invoke the Lambda.
        inbound_domain = self.node.try_get_context("inbound_domain")
        if inbound_domain:
            rule_set = ses.ReceiptRuleSet(
                self, "InboundRuleSet",
                receipt_rule_set_name="email-agent-inbound",
            )
            rule_set.add_rule(
                "StoreInboundToS3",
                recipients=[inbound_domain],  # any address @inbound_domain
                actions=[
                    ses_actions.S3(
                        bucket=self.raw_emails_bucket,
                        object_key_prefix="incoming/",
                    )
                ],
                scan_enabled=True,
            )
            cdk.CfnOutput(self, "InboundRuleSetName",
                value="email-agent-inbound",
                export_name="InboundRuleSetName",
            )

        # ------------------------------------------------------------------ #
        # OUTPUTS
        # ------------------------------------------------------------------ #

        cdk.CfnOutput(self, "RawEmailsBucketName",
            value=self.raw_emails_bucket.bucket_name,
            export_name="RawEmailsBucketName",
        )
        cdk.CfnOutput(self, "DocumentsBucketName",
            value=self.documents_bucket.bucket_name,
            export_name="DocumentsBucketName",
        )
        cdk.CfnOutput(self, "WaiverCriteriaBucketName",
            value=self.waiver_criteria_bucket.bucket_name,
            export_name="WaiverCriteriaBucketName",
        )
        cdk.CfnOutput(self, "IngestionLambdaArn",
            value=self.ingestion_lambda.function_arn,
            export_name="IngestionLambdaArn",
        )
