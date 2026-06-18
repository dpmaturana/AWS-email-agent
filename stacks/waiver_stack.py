import os
import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as sfn_tasks,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_ssm as ssm,
)
from constructs import Construct


class WaiverStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, infra, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        approver_email = self.node.try_get_context("approver_email")
        portal_url = self.node.try_get_context("portal_url") or "https://placeholder.cloudfront.net"

        # ------------------------------------------------------------------ #
        # DYNAMODB — waiver state + thread memory
        # ------------------------------------------------------------------ #

        self.waiver_table = dynamodb.Table(
            self, "WaiverTable",
            table_name="waivers",
            partition_key=dynamodb.Attribute(
                name="waiver_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            point_in_time_recovery=True,
            time_to_live_attribute="ttl",
        )

        # GSI: look up waiver by any message_id in the thread
        self.waiver_table.add_global_secondary_index(
            index_name="MessageIdIndex",
            partition_key=dynamodb.Attribute(
                name="message_id",
                type=dynamodb.AttributeType.STRING,
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # ------------------------------------------------------------------ #
        # SNS — approver + requestor notification topics
        # ------------------------------------------------------------------ #

        self.approver_topic = sns.Topic(
            self, "ApproverTopic",
            topic_name="waiver-approver-notifications",
            display_name="Waiver Approval Requests",
        )
        self.approver_topic.add_subscription(
            subs.EmailSubscription(approver_email)
        )

        self.requestor_topic = sns.Topic(
            self, "RequestorTopic",
            topic_name="waiver-requestor-notifications",
            display_name="Waiver Outcome Notifications",
        )

        # ------------------------------------------------------------------ #
        # SHARED IAM ROLE FOR ALL LAMBDAS
        # ------------------------------------------------------------------ #

        lambda_role = iam.Role(
            self, "WaiverLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
            inline_policies={
                "WaiverPolicy": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=[
                            "dynamodb:GetItem",
                            "dynamodb:PutItem",
                            "dynamodb:UpdateItem",
                            "dynamodb:Query",
                        ],
                        resources=[
                            self.waiver_table.table_arn,
                            f"{self.waiver_table.table_arn}/index/*",
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["states:StartExecution", "states:SendTaskSuccess", "states:SendTaskFailure"],
                        resources=["*"],
                    ),
                    iam.PolicyStatement(
                        actions=["sns:Publish"],
                        resources=[
                            self.approver_topic.topic_arn,
                            self.requestor_topic.topic_arn,
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

        shared_env = {
            "WAIVER_TABLE": self.waiver_table.table_name,
            "APPROVER_SNS_ARN": self.approver_topic.topic_arn,
            "REQUESTOR_SNS_ARN": self.requestor_topic.topic_arn,
            "EMAIL_FROM": self.node.try_get_context("email_from"),
            "WAIVER_CRITERIA_BUCKET": infra.waiver_criteria_bucket.bucket_name,
        }

        common_lambda_kwargs = dict(
            runtime=lambda_.Runtime.PYTHON_3_12,
            role=lambda_role,
            timeout=cdk.Duration.minutes(5),
            memory_size=512,
            log_retention=logs.RetentionDays.THREE_MONTHS,
            tracing=lambda_.Tracing.ACTIVE,
            environment=shared_env,
        )

        # ------------------------------------------------------------------ #
        # STEP FUNCTIONS LAMBDAS
        # ------------------------------------------------------------------ #

        self.store_token_lambda = lambda_.Function(
            self, "StoreTaskTokenLambda",
            function_name="waiver-store-task-token",
            handler="store_task_token.handler",
            code=lambda_.Code.from_asset("lambdas/waiver_tools"),
            **{**common_lambda_kwargs, "environment": {
                **shared_env,
                "REVIEW_PORTAL_URL": portal_url,
            }},
        )

        self.notify_lambda = lambda_.Function(
            self, "NotifyRequestorLambda",
            function_name="waiver-notify-requestor",
            handler="notify_requestor.handler",
            code=lambda_.Code.from_asset("lambdas/waiver_tools"),
            **common_lambda_kwargs,
        )

        # ------------------------------------------------------------------ #
        # STEP FUNCTIONS — waiver human review workflow
        # Implemented by Person 4
        # ------------------------------------------------------------------ #

        sfn_role = iam.Role(
            self, "SfnRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
        )
        self.store_token_lambda.grant_invoke(sfn_role)
        self.notify_lambda.grant_invoke(sfn_role)

        # StoreToken — waitForTaskToken: SFN injects task token,
        # Lambda writes it to DynamoDB and notifies approver via SNS
        store_token_state = sfn_tasks.LambdaInvoke(
            self, "StoreToken",
            lambda_function=self.store_token_lambda,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            payload=sfn.TaskInput.from_object({
                "waiver_id":   sfn.JsonPath.string_at("$.waiver_id"),
                "email_from":  sfn.JsonPath.string_at("$.email_from"),
                "waiver_type": sfn.JsonPath.string_at("$.waiver_type"),
                "department":  sfn.JsonPath.string_at("$.department"),
                "task_token":  sfn.JsonPath.task_token,
            }),
            heartbeat=cdk.Duration.hours(72),
            result_path="$.approvalResult",
        )

        approved_state = sfn_tasks.LambdaInvoke(
            self, "Approved",
            lambda_function=self.notify_lambda,
            payload=sfn.TaskInput.from_object({
                "waiver_id": sfn.JsonPath.string_at("$.waiver_id"),
                "decision":  "approved",
                "comment":   sfn.JsonPath.string_at("$.approvalResult.comment"),
            }),
            result_path=sfn.JsonPath.DISCARD,
        )

        rejected_state = sfn_tasks.LambdaInvoke(
            self, "Rejected",
            lambda_function=self.notify_lambda,
            payload=sfn.TaskInput.from_object({
                "waiver_id": sfn.JsonPath.string_at("$.waiver_id"),
                "decision":  "rejected",
                "comment":   sfn.JsonPath.string_at("$.Cause"),
            }),
            result_path=sfn.JsonPath.DISCARD,
        )

        done_state = sfn.Succeed(self, "Done")
        approved_state.next(done_state)
        rejected_state.next(done_state)

        store_token_state.add_catch(
            rejected_state,
            errors=["WaiverRejected", "States.HeartbeatTimeout", "States.TaskFailed"],
            result_path="$",
        )
        store_token_state.next(approved_state)

        self.state_machine = sfn.StateMachine(
            self, "WaiverStateMachine",
            state_machine_name="waiver-human-review",
            state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_chainable(store_token_state),
            role=sfn_role,
            logs=sfn.LogOptions(
                destination=logs.LogGroup(
                    self, "SfnLogGroup",
                    log_group_name="/aws/states/waiver-human-review",
                    removal_policy=cdk.RemovalPolicy.DESTROY,
                    retention=logs.RetentionDays.THREE_MONTHS,
                ),
                level=sfn.LogLevel.ERROR,
            ),
            tracing_enabled=True,
        )

        # ------------------------------------------------------------------ #
        # TOOL LAMBDAS — called by Agent 2 (Person 2)
        # ------------------------------------------------------------------ #

        tools_env = {**shared_env, "SFN_ARN": self.state_machine.state_machine_arn}

        self.tools_lambda = lambda_.Function(
            self, "WaiverToolsLambda",
            function_name="waiver-tools",
            handler="tool_lambda_handler.handler",
            code=lambda_.Code.from_asset("lambdas/waiver_tools"),
            runtime=lambda_.Runtime.PYTHON_3_12,
            timeout=cdk.Duration.minutes(10),
            memory_size=512,
            role=lambda_role,
            log_retention=logs.RetentionDays.THREE_MONTHS,
            tracing=lambda_.Tracing.ACTIVE,
            environment=tools_env,
        )

        # ------------------------------------------------------------------ #
        # APPROVAL LAMBDA — called by frontend (Person 5) via API Gateway
        # ------------------------------------------------------------------ #

        self.approval_lambda = lambda_.Function(
            self, "ApprovalLambda",
            function_name="waiver-approval-handler",
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/approval"),
            **common_lambda_kwargs,
        )

        # ------------------------------------------------------------------ #
        # WAIVER API LAMBDAS — called by agent or frontend
        # ------------------------------------------------------------------ #

        self.start_waiver_lambda = lambda_.Function(
            self, "StartWaiverLambda",
            function_name="waiver-start-workflow",
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/waiver_tools/start_workflow"),
            **common_lambda_kwargs,
        )

        self.update_waiver_lambda = lambda_.Function(
            self, "UpdateWaiverLambda",
            function_name="waiver-update-state",
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/waiver_tools/update_state"),
            **common_lambda_kwargs,
        )

        self.get_waiver_lambda = lambda_.Function(
            self, "GetWaiverLambda",
            function_name="waiver-get-state",
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/waiver_tools/get_state"),
            **common_lambda_kwargs,
        )

        # ------------------------------------------------------------------ #
        # SSM PARAMETERS — cross-stack sharing with Person 2
        # ------------------------------------------------------------------ #

        ssm.StringParameter(self, "ParamToolsArn",
            parameter_name="/email-agent/waiver-tools-lambda-arn",
            string_value=self.tools_lambda.function_arn,
        )
        ssm.StringParameter(self, "ParamTableName",
            parameter_name="/email-agent/waiver-table-name",
            string_value=self.waiver_table.table_name,
        )
        ssm.StringParameter(self, "ParamSfnArn",
            parameter_name="/email-agent/sfn-arn",
            string_value=self.state_machine.state_machine_arn,
        )

        # ------------------------------------------------------------------ #
        # OUTPUTS
        # ------------------------------------------------------------------ #

        cdk.CfnOutput(self, "WaiverTableName",
            value=self.waiver_table.table_name,
            export_name="WaiverTableName",
        )
        cdk.CfnOutput(self, "StateMachineArn",
            value=self.state_machine.state_machine_arn,
            export_name="StateMachineArn",
        )
        cdk.CfnOutput(self, "ApprovalLambdaArn",
            value=self.approval_lambda.function_arn,
            export_name="ApprovalLambdaArn",
        )
        cdk.CfnOutput(self, "WaiverToolsLambdaArn",
            value=self.tools_lambda.function_arn,
            export_name="WaiverToolsLambdaArn",
        )
