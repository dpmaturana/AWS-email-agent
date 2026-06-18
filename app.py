import aws_cdk as cdk
from stacks.infra_stack import InfraStack
from stacks.rag_stack import RagStack
from stacks.waiver_stack import WaiverStack
from stacks.agent_stack import AgentStack
from stacks.frontend_stack import FrontendStack

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account") or None,
    region=app.node.try_get_context("region") or "eu-west-1",
)

infra = InfraStack(app, "InfraStack", env=env)
rag = RagStack(app, "RagStack", infra=infra, env=env)
waiver = WaiverStack(app, "WaiverStack", infra=infra, env=env)
agent = AgentStack(app, "AgentStack", infra=infra, rag=rag, waiver=waiver, env=env)
frontend = FrontendStack(app, "FrontendStack", waiver=waiver, env=env)

app.synth()
