#!/usr/bin/env python3
"""
Standalone CDK app entry point for Person 3's RagStack.

In the integrated project, Person 1's `app.py` instantiates all stacks in one
app and passes the documents bucket object directly:

    from rag_stack import RagStack
    RagStack(app, "RagStack", documents_bucket=infra.documents_bucket, env=env)

This file lets Person 3 deploy/test the stack in isolation by importing the
documents bucket by name:

    cdk deploy -c documentsBucketName=my-existing-documents-bucket
"""

import os

import aws_cdk as cdk

from rag_stack import RagStack

app = cdk.App()

# Account-agnostic: resolve account/region from the standard CDK env vars at
# synth time rather than hardcoding them.
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION"),
)

RagStack(app, "RagStack", env=env)

app.synth()
