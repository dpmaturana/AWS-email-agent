import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
)
from constructs import Construct


class WaiverStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, infra, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        approver_email = self.node.try_get_context("approver_email")

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
            time_to_live_attribute="ttl",
        )

        # GSI: look up waiver by any message_id in the thread
        self.waiver_table.add_global_secondary_index(
            index_name="message_id_index",
            partition_key=dynamodb.Attribute(
                name="message_id",
                type=dynamodb.AttributeType.STRING,
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # ------------------------------------------------------------------ #
        # SNS — approver notification
        # ------------------------------------------------------------------ #

        self.approver_topic = sns.Topic(
            self, "ApproverTopic",
            display_name="Waiver Approval Notifications",
        )

        self.approver_topic.add_subscription(
            subs.EmailSubscription(approver_email)
        )

        # ------------------------------------------------------------------ #
        # STEP FUNCTIONS — waiver approval workflow
        # Person 4: implement the state machine logic below
        # The waitForTaskToken pattern pauses the workflow until
        # the approval Lambda calls sendTaskSuccess or sendTaskFailure
        # ------------------------------------------------------------------ #

        sfn_role = iam.Role(
            self, "StateMachineRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            inline_policies={
                "SFNPolicy": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=["sns:Publish"],
                        resources=[self.approver_topic.topic_arn],
                    ),
                    iam.PolicyStatement(
                        actions=["dynamodb:UpdateItem"],
                        resources=[self.waiver_table.table_arn],
                    ),
                ])
            },
        )

        # --- Person 4: replace this placeholder with the real state machine ---
        wait_for_approval = sfn.Pass(
            self, "WaitForApproval",
            comment="PLACEHOLDER — Person 4: replace with waitForTaskToken state",
        )

        approved_state = sfn.Pass(
            self, "Approved",
            comment="PLACEHOLDER — Person 4: update DynamoDB status to approved, publish SNS",
        )

        rejected_state = sfn.Pass(
            self, "Rejected",
            comment="PLACEHOLDER — Person 4: update DynamoDB status to rejected, publish SNS",
        )

        definition = wait_for_approval.next(
            sfn.Choice(self, "WasApproved")
            .when(sfn.Condition.string_equals("$.decision", "approve"), approved_state)
            .otherwise(rejected_state)
        )

        self.state_machine = sfn.StateMachine(
            self, "WaiverStateMachine",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            role=sfn_role,
            timeout=cdk.Duration.days(7),
        )
        # --- end Person 4 placeholder ---

        # ------------------------------------------------------------------ #
        # SHARED IAM ROLE FOR TOOL LAMBDAS
        # ------------------------------------------------------------------ #

        tools_role = iam.Role(
            self, "WaiverToolsRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
            inline_policies={
                "WaiverToolsPolicy": iam.PolicyDocument(statements=[
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
                        actions=["states:StartExecution"],
                        resources=[self.state_machine.state_machine_arn],
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
            "WAIVER_TABLE_NAME": self.waiver_table.table_name,
            "STATE_MACHINE_ARN": self.state_machine.state_machine_arn,
            "WAIVER_CRITERIA_BUCKET": infra.waiver_criteria_bucket.bucket_name,
            "EMAIL_FROM": self.node.try_get_context("email_from"),
        }

        # ------------------------------------------------------------------ #
        # TOOL LAMBDAS — Person 4: implement each handler
        # ------------------------------------------------------------------ #

        self.start_waiver_lambda = lambda_.Function(
            self, "StartWaiverWorkflowLambda",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/waiver_tools/start_workflow"),
            role=tools_role,
            timeout=cdk.Duration.seconds(30),
            environment=shared_env,
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        self.update_waiver_lambda = lambda_.Function(
            self, "UpdateWaiverStateLambda",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/waiver_tools/update_state"),
            role=tools_role,
            timeout=cdk.Duration.seconds(30),
            environment=shared_env,
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        self.get_waiver_lambda = lambda_.Function(
            self, "GetWaiverStateLambda",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/waiver_tools/get_state"),
            role=tools_role,
            timeout=cdk.Duration.seconds(30),
            environment=shared_env,
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        # ------------------------------------------------------------------ #
        # APPROVAL LAMBDA — called by frontend via API Gateway
        # Person 4: implement in lambdas/approval/handler.py
        # ------------------------------------------------------------------ #

        approval_role = iam.Role(
            self, "ApprovalLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
            inline_policies={
                "ApprovalPolicy": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=[
                            "states:SendTaskSuccess",
                            "states:SendTaskFailure",
                        ],
                        resources=["*"],
                    ),
                    iam.PolicyStatement(
                        actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
                        resources=[self.waiver_table.table_arn],
                    ),
                ])
            },
        )

        self.approval_lambda = lambda_.Function(
            self, "ApprovalLambda",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/approval"),
            role=approval_role,
            timeout=cdk.Duration.seconds(30),
            environment={
                "WAIVER_TABLE_NAME": self.waiver_table.table_name,
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
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
        cdk.CfnOutput(self, "StartWaiverLambdaArn",
            value=self.start_waiver_lambda.function_arn,
            export_name="StartWaiverLambdaArn",
        )
        cdk.CfnOutput(self, "UpdateWaiverLambdaArn",
            value=self.update_waiver_lambda.function_arn,
            export_name="UpdateWaiverLambdaArn",
        )
        cdk.CfnOutput(self, "GetWaiverLambdaArn",
            value=self.get_waiver_lambda.function_arn,
            export_name="GetWaiverLambdaArn",
        )
