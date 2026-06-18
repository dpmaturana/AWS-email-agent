import aws_cdk as cdk
from aws_cdk import (
    aws_cognito as cognito,
    aws_apigateway as apigw,
    aws_lambda as lambda_,
    aws_iam as iam,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_logs as logs,
)
from constructs import Construct


class FrontendStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, waiver, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------ #
        # COGNITO USER POOL
        # ------------------------------------------------------------------ #

        self.user_pool = cognito.UserPool(
            self, "ApproverUserPool",
            user_pool_name="email-agent-approvers",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
            ),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        self.user_pool_client = cognito.UserPoolClient(
            self, "ApproverUserPoolClient",
            user_pool=self.user_pool,
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            generate_secret=False,
        )

        # ------------------------------------------------------------------ #
        # API LAMBDA ROLE
        # ------------------------------------------------------------------ #

        api_role = iam.Role(
            self, "ApiLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
            inline_policies={
                "ApiPolicy": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=[
                            "dynamodb:GetItem",
                            "dynamodb:Scan",
                            "dynamodb:Query",
                        ],
                        resources=[
                            waiver.waiver_table.table_arn,
                            f"{waiver.waiver_table.table_arn}/index/*",
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["lambda:InvokeFunction"],
                        resources=[waiver.approval_lambda.function_arn],
                    ),
                    iam.PolicyStatement(
                        actions=["s3:GetObject"],
                        resources=["*"],  # for presigned URL generation
                    ),
                ])
            },
        )

        shared_env = {
            "WAIVER_TABLE_NAME": waiver.waiver_table.table_name,
            "APPROVAL_LAMBDA_ARN": waiver.approval_lambda.function_arn,
        }

        # ------------------------------------------------------------------ #
        # API LAMBDAS
        # Person 5: implement each handler
        # ------------------------------------------------------------------ #

        list_waivers_lambda = lambda_.Function(
            self, "ListWaiversLambda",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/api/list_waivers"),
            role=api_role,
            timeout=cdk.Duration.seconds(10),
            environment=shared_env,
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        get_waiver_lambda = lambda_.Function(
            self, "GetWaiverLambda",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/api/get_waiver"),
            role=api_role,
            timeout=cdk.Duration.seconds(10),
            environment=shared_env,
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        decide_waiver_lambda = lambda_.Function(
            self, "DecideWaiverLambda",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/api/decide_waiver"),
            role=api_role,
            timeout=cdk.Duration.seconds(10),
            environment=shared_env,
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        # ------------------------------------------------------------------ #
        # API GATEWAY
        # ------------------------------------------------------------------ #

        authorizer = apigw.CognitoUserPoolsAuthorizer(
            self, "CognitoAuthorizer",
            cognito_user_pools=[self.user_pool],
        )

        api = apigw.RestApi(
            self, "WaiverApi",
            rest_api_name="email-agent-api",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=apigw.Cors.ALL_METHODS,
                allow_headers=["Authorization", "Content-Type"],
            ),
        )

        auth_opts = {"authorizer": authorizer, "authorization_type": apigw.AuthorizationType.COGNITO}

        waivers = api.root.add_resource("waivers")
        waivers.add_method("GET", apigw.LambdaIntegration(list_waivers_lambda), **auth_opts)

        waiver_resource = waivers.add_resource("{waiver_id}")
        waiver_resource.add_method("GET", apigw.LambdaIntegration(get_waiver_lambda), **auth_opts)

        decide = waiver_resource.add_resource("decide")
        decide.add_method("POST", apigw.LambdaIntegration(decide_waiver_lambda), **auth_opts)

        # ------------------------------------------------------------------ #
        # S3 + CLOUDFRONT — React SPA hosting
        # ------------------------------------------------------------------ #

        spa_bucket = s3.Bucket(
            self, "SpaBucket",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        )

        oac = cloudfront.S3OriginAccessControl(
            self, "OAC",
            signing=cloudfront.Signing.SIGV4_NO_OVERRIDE,
        )

        distribution = cloudfront.Distribution(
            self, "SpaDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    spa_bucket, origin_access_control=oac
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
            ],
            default_root_object="index.html",
        )

        # Deploy React build to S3 and invalidate CloudFront cache
        s3deploy.BucketDeployment(
            self, "DeployReactApp",
            sources=[s3deploy.Source.asset("frontend/dist")],
            destination_bucket=spa_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        # ------------------------------------------------------------------ #
        # OUTPUTS
        # ------------------------------------------------------------------ #

        cdk.CfnOutput(self, "UserPoolId",
            value=self.user_pool.user_pool_id,
            export_name="UserPoolId",
        )
        cdk.CfnOutput(self, "UserPoolClientId",
            value=self.user_pool_client.user_pool_client_id,
            export_name="UserPoolClientId",
        )
        cdk.CfnOutput(self, "ApiUrl",
            value=api.url,
            export_name="ApiUrl",
        )
        cdk.CfnOutput(self, "CloudFrontUrl",
            value=f"https://{distribution.distribution_domain_name}",
            export_name="CloudFrontUrl",
        )
