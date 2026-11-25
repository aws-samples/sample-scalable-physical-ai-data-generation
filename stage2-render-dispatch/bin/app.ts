// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { RenderDispatchStack } from "../lib/render-dispatch-stack";

const app = new cdk.App();

// Account is resolved from the environment / CDK context; never hardcoded.
const account =
    app.node.tryGetContext("account") ||
    process.env.CDK_DEPLOY_ACCOUNT ||
    process.env.CDK_DEFAULT_ACCOUNT;

// Region defaults to us-west-2 (workspace convention). Override with `-c region=...`.
const region =
    app.node.tryGetContext("region") ||
    process.env.CDK_DEPLOY_REGION ||
    process.env.CDK_DEFAULT_REGION ||
    "us-west-2";

const stackName = app.node.tryGetContext("stack_name") || "RenderDispatchStack";

new RenderDispatchStack(app, stackName, {
    env: { account, region },
    stackName,
    description:
        "S3 upload -> Lambda -> DynamoDB event layer, plus a static S3 + CloudFront web app that displays uploaded file names",
});

app.synth();
