import aws_cdk as cdk
from aws_cdk import (
    aws_s3 as s3,
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

        self.raw_emails_bucket.grant_read(ingestion_role)

        self.ingestion_lambda = lambda_.Function(
            self, "IngestionLambda",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/ingestion"),
            role=ingestion_role,
            timeout=cdk.Duration.seconds(30),
            environment={
                "EMAIL_FROM": email_from,
                "EMAIL_DEMO_RECIPIENT": email_demo_recipient,
                "RAW_EMAILS_BUCKET": self.raw_emails_bucket.bucket_name,
                # AGENT_CORE_AGENT_ID and AGENT_CORE_ALIAS_ID injected by AgentStack
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
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
