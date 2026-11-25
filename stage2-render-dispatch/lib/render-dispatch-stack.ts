// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import * as path from "path";
import * as cdk from "aws-cdk-lib";
import {
    aws_apigatewayv2 as apigwv2,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecr_assets as ecrAssets,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_lambda as lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_s3_notifications as s3n,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
} from "aws-cdk-lib";
import { HttpLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import { HttpUserPoolAuthorizer } from "aws-cdk-lib/aws-apigatewayv2-authorizers";
import { Construct } from "constructs";

/**
 * Stem for the two globally-unique names this stack needs: the upload bucket and
 * the Cognito Hosted UI domain. Both are suffixed with account + region (see
 * `resourceName` below) so a fresh clone deploys into any account with no edits.
 *
 * "render-inputs" rather than "episodes" because the bucket holds both halves of
 * what the pipeline consumes: the recorded teleoperation trajectories under
 * `episodes/`, and the visual-randomization asset libraries under `base-scenes/`
 * (base scene, rig, textures, distractor props, table assets, HDRI backgrounds).
 */
const RESOURCE_STEM = "teleoperation-render-inputs";

/** S3 prefix that episode JSONs are uploaded under (drives the pipeline). */
const EPISODE_PREFIX = "episodes/";

/** Base-scene assets (uploaded once via CLI — see README). */
const BASE_SCENE_KEY = "base-scenes/scene_twin.blend";
const RIG_KEY = "base-scenes/rig.json";

/**
 * S3 prefix holding texture zips for domain randomization, with `table_texture/`
 * and `box_texture/` subfolders. Optional: if nothing is uploaded here, the
 * Blender task skips randomization and renders the plain base scene.
 */
const TEXTURE_PREFIX = "base-scenes/blender_texture/";

/**
 * S3 prefix holding distractor-object zips (Poly Haven `<name>.blend.zip`) for
 * domain randomization. Optional: if nothing is uploaded here, the Blender task
 * skips distractor placement. When present, one asset is placed on the table per
 * episode, inside the context-camera FOV and clear of the manipulated cube.
 */
const DISTRACTOR_PREFIX = "base-scenes/distractor/";

/**
 * Per-episode HDRI environment ("0" = off, "1" = on). When on and HDRIs are
 * present under BACKGROUND_PREFIX, the sealed lab room (shell + ceiling
 * fixtures) is removed for the episode and a random `.exr`/`.hdr` becomes both
 * the backdrop and the light source (image-based lighting), at a random yaw,
 * seeded by episode id. Mutually exclusive with the room look by construction —
 * an environment is invisible from inside a closed box. Optional: with nothing
 * uploaded under the prefix the step is skipped and the room-lit scene renders.
 */
const BACKGROUND = "1";

/**
 * Which HDRI *set* the environment step draws from. Sets live in their own
 * subfolder under `base-scenes/background/` so several can coexist in the bucket
 * and the deploy picks one — the environment should suit the task being
 * demonstrated, so this is deliberately not a random choice across all HDRIs.
 *
 * The pointed-at folder must hold the `.exr`/`.hdr` files DIRECTLY:
 * randomize_background.py globs one level only, and the Blender task mirrors
 * this prefix into /tmp/background with the prefix stripped. Nesting another
 * folder inside would make the step silently find nothing and skip.
 */
const BACKGROUND_PREFIX = "base-scenes/background/hospital/";

/** SSM parameter names holding the existing Deadline Cloud farm/queue ids. */
const FARM_ID_PARAM = "/render-dispatch/farm-id";
const QUEUE_ID_PARAM = "/render-dispatch/queue-id";

/**
 * Automated rendering pipeline for LeRobot data collection.
 *
 *   Intake:   upload episodes/*.json -> S3 -> EpisodeIntakeFn -> DynamoDB
 *             (status=RECEIVED) + StartExecution(pipeline, name=episodeId)
 *   Pipeline: Step Functions -> [Blender Fargate: reconstruct+prepare+submit] ->
 *             [poll Deadline] -> [ffmpeg Fargate: encode] -> Finalize
 *   Web app:  browser -> CloudFront (static site) + Cognito + API Gateway
 *             (GET /feed status, POST /episodes presigned upload URL)
 */
export class RenderDispatchStack extends cdk.Stack {
    constructor(scope: Construct, id: string, props?: cdk.StackProps) {
        super(scope, id, props);

        // Region the Deadline Cloud farm/queue live in. May differ from the
        // stack region (buckets/DynamoDB/SSM). Defaults to us-west-2 where the
        // farm is; override with `-c deadlineRegion=<region>`.
        const deadlineRegion =
            (this.node.tryGetContext("deadlineRegion") as string) || "us-west-2";

        // ------------------------------------------------------------------
        // Globally-unique names, DERIVED so a fresh clone deploys anywhere.
        //
        // S3 bucket names and Cognito Hosted UI domains are unique across all
        // AWS accounts, so a hardcoded name means the second person to deploy
        // this gets BucketAlreadyExists. Suffixing account + region makes the
        // name deterministic per deployment and collision-free between them.
        //
        // Each can still be overridden, which is what lets an EXISTING
        // deployment keep the bucket it already has: changing `bucketName`
        // REPLACES the bucket, and since it carries RemovalPolicy.DESTROY +
        // autoDeleteObjects, that would delete the base scene and every uploaded
        // asset with it. Pin the old name to avoid that:
        //
        //   npx cdk deploy -c uploadBucketName=<existing-bucket>
        //                  -c cognitoDomainPrefix=<existing-prefix>
        // ------------------------------------------------------------------
        const derivedName = `${RESOURCE_STEM}-${this.account}-${this.region}`;
        const uploadBucketName =
            (this.node.tryGetContext("uploadBucketName") as string) || derivedName;
        const cognitoDomainPrefix =
            (this.node.tryGetContext("cognitoDomainPrefix") as string) ||
            derivedName;

        // ------------------------------------------------------------------
        // Feed / status store.
        //   pk = "FEED"; sk = "<uploadedAt ISO>#<episodeId>" (newest-first query)
        // Records carry pipeline status: RECEIVED -> RECONSTRUCTING -> SUBMITTED
        //   -> RENDERING -> ENCODING -> DONE | FAILED
        // ------------------------------------------------------------------
        const feedTable = new dynamodb.Table(this, "FeedTable", {
            partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
            sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
        });

        // ------------------------------------------------------------------
        // Buckets: uploads (event source + base assets) and rendered outputs.
        // ------------------------------------------------------------------
        const uploadBucket = new s3.Bucket(this, "UploadBucket", {
            bucketName: uploadBucketName,
            encryption: s3.BucketEncryption.S3_MANAGED,
            enforceSSL: true,
            blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
            autoDeleteObjects: true,
            cors: [
                {
                    // Allow the browser to PUT episode JSONs via presigned URLs.
                    allowedMethods: [s3.HttpMethods.PUT],
                    allowedOrigins: ["*"],
                    allowedHeaders: ["*"],
                    maxAge: 3000,
                },
            ],
        });

        const outputBucket = new s3.Bucket(this, "OutputBucket", {
            encryption: s3.BucketEncryption.S3_MANAGED,
            enforceSSL: true,
            blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
            autoDeleteObjects: true,
        });

        // ------------------------------------------------------------------
        // SSM parameters: existing Deadline farm/queue ids.
        //
        // These are NEITHER created NOR referenced as resources by this stack:
        // the farm/queue are ephemeral (torn down to save cost, recreated with
        // new ids), so their ids are operational DATA, not code. If
        // CloudFormation owned them, a plain `cdk deploy` would clobber the
        // values back to a placeholder; if it merely looked them up at deploy
        // time, a deploy would FAIL whenever the farm (and its params) had been
        // deleted. Instead the stack only grants read access to the two ARNs.
        //
        // Create/update the values out-of-band with the AWS CLI; the change
        // takes effect on the next pipeline run with NO redeploy:
        //
        //   aws ssm put-parameter --name /render-dispatch/farm-id \
        //       --type String --value farm-xxxx --overwrite
        //   aws ssm put-parameter --name /render-dispatch/queue-id \
        //       --type String --value queue-xxxx --overwrite
        // ------------------------------------------------------------------
        const ssmReadPolicy = new iam.PolicyStatement({
            actions: ["ssm:GetParameter", "ssm:GetParameters"],
            resources: [
                this.formatArn({
                    service: "ssm",
                    resource: "parameter",
                    resourceName: FARM_ID_PARAM.replace(/^\//, ""),
                }),
                this.formatArn({
                    service: "ssm",
                    resource: "parameter",
                    resourceName: QUEUE_ID_PARAM.replace(/^\//, ""),
                }),
            ],
        });

        // ------------------------------------------------------------------
        // Container images (built by CDK, pushed to ECR).
        //   The Blender image builds from the project root so it can COPY
        //   blender_twin/ and the randomizers directly; .dockerignore keeps
        //   the context to just those files.
        // ------------------------------------------------------------------
        const blenderImage = new ecrAssets.DockerImageAsset(this, "BlenderImage", {
            directory: path.join(__dirname, ".."),
            file: path.join("docker", "blender", "Dockerfile"),
            platform: ecrAssets.Platform.LINUX_AMD64,
        });
        const ffmpegImage = new ecrAssets.DockerImageAsset(this, "FfmpegImage", {
            directory: path.join(__dirname, "..", "docker", "ffmpeg"),
            platform: ecrAssets.Platform.LINUX_AMD64,
        });

        // ------------------------------------------------------------------
        // Networking + ECS cluster for the Fargate tasks. Public subnets only
        // (no NAT) — tasks get a public IP to reach AWS + Deadline endpoints.
        // ------------------------------------------------------------------
        const vpc = new ec2.Vpc(this, "PipelineVpc", {
            maxAzs: 2,
            natGateways: 0,
            subnetConfiguration: [
                { name: "public", subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
            ],
        });
        const cluster = new ecs.Cluster(this, "PipelineCluster", { vpc });

        // Deadline Cloud permissions for submit/monitor/download. The job-bundle
        // submit flow touches many read/list APIs (GetFarm, GetQueue,
        // ListQueueEnvironments, GetQueueEnvironment, ListStorageProfilesForQueue,
        // AssumeQueueRoleForUser, CreateJob, GetJob, ...). For the prototype we
        // grant deadline:* to avoid per-action whack-a-mole; tighten to the
        // specific actions + farm/queue ARNs before anything shared.
        const deadlinePolicy = new iam.PolicyStatement({
            actions: ["deadline:*"],
            resources: ["*"],
        });

        // ---- Blender task (reconstruct + prepare + submit) ----------------
        const blenderTaskDef = new ecs.FargateTaskDefinition(this, "BlenderTaskDef", {
            cpu: 4096,
            memoryLimitMiB: 16384,
            runtimePlatform: {
                cpuArchitecture: ecs.CpuArchitecture.X86_64,
                operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
            },
        });
        const blenderContainer = blenderTaskDef.addContainer("blender", {
            image: ecs.ContainerImage.fromDockerImageAsset(blenderImage),
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: "blender",
                logRetention: logs.RetentionDays.ONE_MONTH,
            }),
            environment: {
                AWS_REGION: this.region,
                DEADLINE_REGION: deadlineRegion,
                UPLOAD_BUCKET: uploadBucket.bucketName,
                BASE_SCENE_KEY,
                RIG_KEY,
                TEXTURE_PREFIX,
                DISTRACTOR_PREFIX,
                BACKGROUND,
                BACKGROUND_PREFIX,
                FEED_TABLE: feedTable.tableName,
                FARM_ID_PARAM,
                QUEUE_ID_PARAM,
            },
        });
        uploadBucket.grantRead(blenderTaskDef.taskRole);
        feedTable.grantReadWriteData(blenderTaskDef.taskRole);
        blenderTaskDef.taskRole.addToPrincipalPolicy(ssmReadPolicy);
        blenderTaskDef.taskRole.addToPrincipalPolicy(deadlinePolicy);

        // ---- ffmpeg task (download frames + encode + upload) --------------
        const encodeTaskDef = new ecs.FargateTaskDefinition(this, "EncodeTaskDef", {
            cpu: 2048,
            memoryLimitMiB: 8192,
            runtimePlatform: {
                cpuArchitecture: ecs.CpuArchitecture.X86_64,
                operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
            },
        });
        const encodeContainer = encodeTaskDef.addContainer("encode", {
            image: ecs.ContainerImage.fromDockerImageAsset(ffmpegImage),
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: "encode",
                logRetention: logs.RetentionDays.ONE_MONTH,
            }),
            environment: {
                AWS_REGION: this.region,
                DEADLINE_REGION: deadlineRegion,
                OUTPUT_BUCKET: outputBucket.bucketName,
                FEED_TABLE: feedTable.tableName,
                FARM_ID_PARAM,
                QUEUE_ID_PARAM,
            },
        });
        outputBucket.grantWrite(encodeTaskDef.taskRole);
        feedTable.grantReadWriteData(encodeTaskDef.taskRole);
        encodeTaskDef.taskRole.addToPrincipalPolicy(ssmReadPolicy);
        encodeTaskDef.taskRole.addToPrincipalPolicy(deadlinePolicy);

        // ------------------------------------------------------------------
        // Pipeline Lambdas: poller (Deadline GetJob) and finalize (terminal).
        // ------------------------------------------------------------------
        const pollerFn = new lambda.Function(this, "RenderPollerFn", {
            runtime: lambda.Runtime.PYTHON_3_13,
            architecture: lambda.Architecture.ARM_64,
            handler: "render_poller.handler",
            code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambda", "render-poller")),
            timeout: cdk.Duration.seconds(30),
            memorySize: 128,
            logGroup: new logs.LogGroup(this, "RenderPollerFnLogs", {
                retention: logs.RetentionDays.ONE_MONTH,
                removalPolicy: cdk.RemovalPolicy.DESTROY,
            }),
            environment: {
                FEED_TABLE: feedTable.tableName,
                FARM_ID_PARAM,
                QUEUE_ID_PARAM,
                DEADLINE_REGION: deadlineRegion,
            },
        });
        feedTable.grantReadWriteData(pollerFn);
        pollerFn.addToRolePolicy(ssmReadPolicy);
        pollerFn.addToRolePolicy(deadlinePolicy);

        const finalizeFn = new lambda.Function(this, "FinalizeFn", {
            runtime: lambda.Runtime.PYTHON_3_13,
            architecture: lambda.Architecture.ARM_64,
            handler: "finalize.handler",
            code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambda", "finalize")),
            timeout: cdk.Duration.seconds(15),
            memorySize: 128,
            logGroup: new logs.LogGroup(this, "FinalizeFnLogs", {
                retention: logs.RetentionDays.ONE_MONTH,
                removalPolicy: cdk.RemovalPolicy.DESTROY,
            }),
            environment: { FEED_TABLE: feedTable.tableName },
        });
        feedTable.grantReadWriteData(finalizeFn);

        // ------------------------------------------------------------------
        // Step Functions state machine wiring the pipeline together.
        // ------------------------------------------------------------------
        const stateMachine = this.buildStateMachine(
            cluster,
            blenderTaskDef,
            blenderContainer,
            encodeTaskDef,
            encodeContainer,
            pollerFn,
            finalizeFn,
            uploadBucket,
            outputBucket,
        );

        // ------------------------------------------------------------------
        // EpisodeIntakeFn: S3 ObjectCreated -> record + start the pipeline.
        // ------------------------------------------------------------------
        const intakeFn = new lambda.Function(this, "EpisodeIntakeFn", {
            runtime: lambda.Runtime.PYTHON_3_13,
            architecture: lambda.Architecture.ARM_64,
            handler: "episode_intake.handler",
            code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambda", "episode-intake")),
            timeout: cdk.Duration.seconds(15),
            memorySize: 128,
            logGroup: new logs.LogGroup(this, "EpisodeIntakeFnLogs", {
                retention: logs.RetentionDays.ONE_MONTH,
                removalPolicy: cdk.RemovalPolicy.DESTROY,
            }),
            environment: {
                FEED_TABLE: feedTable.tableName,
                EPISODE_PREFIX,
                STATE_MACHINE_ARN: stateMachine.stateMachineArn,
            },
        });
        feedTable.grantWriteData(intakeFn);
        stateMachine.grantStartExecution(intakeFn);

        uploadBucket.addEventNotification(
            s3.EventType.OBJECT_CREATED,
            new s3n.LambdaDestination(intakeFn),
        );

        // ------------------------------------------------------------------
        // RenderStatusFn: read API returning status + presigned video URLs.
        // ------------------------------------------------------------------
        const statusFn = new lambda.Function(this, "RenderStatusFn", {
            runtime: lambda.Runtime.PYTHON_3_13,
            architecture: lambda.Architecture.ARM_64,
            handler: "render_status.handler",
            code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambda", "render-status")),
            timeout: cdk.Duration.seconds(15),
            memorySize: 128,
            logGroup: new logs.LogGroup(this, "RenderStatusFnLogs", {
                retention: logs.RetentionDays.ONE_MONTH,
                removalPolicy: cdk.RemovalPolicy.DESTROY,
            }),
            environment: {
                FEED_TABLE: feedTable.tableName,
                OUTPUT_BUCKET: outputBucket.bucketName,
            },
        });
        feedTable.grantReadData(statusFn);
        outputBucket.grantRead(statusFn);

        // ------------------------------------------------------------------
        // EpisodeUploadUrlFn: issues presigned PUT URLs for UI uploads.
        // ------------------------------------------------------------------
        const uploadUrlFn = new lambda.Function(this, "EpisodeUploadUrlFn", {
            runtime: lambda.Runtime.PYTHON_3_13,
            architecture: lambda.Architecture.ARM_64,
            handler: "episode_upload_url.handler",
            code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambda", "episode-upload-url")),
            timeout: cdk.Duration.seconds(15),
            memorySize: 128,
            logGroup: new logs.LogGroup(this, "EpisodeUploadUrlFnLogs", {
                retention: logs.RetentionDays.ONE_MONTH,
                removalPolicy: cdk.RemovalPolicy.DESTROY,
            }),
            environment: {
                UPLOAD_BUCKET: uploadBucket.bucketName,
                EPISODE_PREFIX,
            },
        });
        uploadBucket.grantPut(uploadUrlFn, `${EPISODE_PREFIX}*`);

        // ------------------------------------------------------------------
        // EpisodeDeleteFn: removes a terminal episode (record + S3 objects).
        // ------------------------------------------------------------------
        const deleteFn = new lambda.Function(this, "EpisodeDeleteFn", {
            runtime: lambda.Runtime.PYTHON_3_13,
            architecture: lambda.Architecture.ARM_64,
            handler: "episode_delete.handler",
            code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambda", "episode-delete")),
            timeout: cdk.Duration.seconds(15),
            memorySize: 128,
            logGroup: new logs.LogGroup(this, "EpisodeDeleteFnLogs", {
                retention: logs.RetentionDays.ONE_MONTH,
                removalPolicy: cdk.RemovalPolicy.DESTROY,
            }),
            environment: {
                FEED_TABLE: feedTable.tableName,
                UPLOAD_BUCKET: uploadBucket.bucketName,
                OUTPUT_BUCKET: outputBucket.bucketName,
            },
        });
        feedTable.grantReadWriteData(deleteFn);
        uploadBucket.grantDelete(deleteFn, `${EPISODE_PREFIX}*`);
        outputBucket.grantDelete(deleteFn, "output/*");

        // ------------------------------------------------------------------
        // Static site bucket + CloudFront (unchanged pattern).
        // ------------------------------------------------------------------
        const siteBucket = new s3.Bucket(this, "SiteBucket", {
            encryption: s3.BucketEncryption.S3_MANAGED,
            enforceSSL: true,
            blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
            autoDeleteObjects: true,
        });

        const distribution = new cloudfront.Distribution(this, "SiteDistribution", {
            defaultRootObject: "index.html",
            defaultBehavior: {
                origin: origins.S3BucketOrigin.withOriginAccessControl(siteBucket),
                viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            },
        });
        const webAppUrl = `https://${distribution.distributionDomainName}`;

        // ------------------------------------------------------------------
        // Cognito user pool + Hosted UI + SPA client (OAuth PKCE).
        // ------------------------------------------------------------------
        // Self sign-up is OFF: every signed-in user can upload episodes (which
        // start paid GPU renders) and view/delete the shared feed, so only
        // users an administrator creates may sign in. See README "Sign in".
        const userPool = new cognito.UserPool(this, "UserPool", {
            selfSignUpEnabled: false,
            signInAliases: { email: true },
            autoVerify: { email: true },
            standardAttributes: { email: { required: true, mutable: true } },
            passwordPolicy: {
                minLength: 8,
                requireLowercase: true,
                requireUppercase: true,
                requireDigits: true,
                requireSymbols: false,
            },
            removalPolicy: cdk.RemovalPolicy.DESTROY,
        });
        userPool.addDomain("UserPoolDomain", {
            cognitoDomain: { domainPrefix: cognitoDomainPrefix },
        });
        const userPoolClient = userPool.addClient("WebClient", {
            generateSecret: false,
            authFlows: { userSrp: true },
            oAuth: {
                flows: { authorizationCodeGrant: true },
                scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callbackUrls: [`${webAppUrl}/`, webAppUrl],
                logoutUrls: [`${webAppUrl}/`, webAppUrl],
            },
            preventUserExistenceErrors: true,
        });

        // ------------------------------------------------------------------
        // HTTP API: GET /feed (status) and POST /episodes (presigned upload).
        // Both behind the Cognito JWT authorizer.
        // ------------------------------------------------------------------
        const authorizer = new HttpUserPoolAuthorizer("FeedAuthorizer", userPool, {
            userPoolClients: [userPoolClient],
        });
        const httpApi = new apigwv2.HttpApi(this, "FeedHttpApi", {
            corsPreflight: {
                allowOrigins: [webAppUrl],
                allowMethods: [
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.DELETE,
                ],
                allowHeaders: ["authorization", "content-type"],
                maxAge: cdk.Duration.hours(1),
            },
        });
        httpApi.addRoutes({
            path: "/feed",
            methods: [apigwv2.HttpMethod.GET],
            integration: new HttpLambdaIntegration("FeedIntegration", statusFn),
            authorizer,
        });
        httpApi.addRoutes({
            path: "/episodes",
            methods: [apigwv2.HttpMethod.POST],
            integration: new HttpLambdaIntegration("UploadUrlIntegration", uploadUrlFn),
            authorizer,
        });
        httpApi.addRoutes({
            path: "/episodes",
            methods: [apigwv2.HttpMethod.DELETE],
            integration: new HttpLambdaIntegration("DeleteIntegration", deleteFn),
            authorizer,
        });
        const apiUrl = httpApi.apiEndpoint;

        // ------------------------------------------------------------------
        // Deploy the static site + runtime config.json.
        // ------------------------------------------------------------------
        new s3deploy.BucketDeployment(this, "SiteDeployment", {
            sources: [
                s3deploy.Source.asset(path.join(__dirname, "..", "web")),
                s3deploy.Source.jsonData("config.json", {
                    region: this.region,
                    apiUrl,
                    cognitoDomain: `https://${cognitoDomainPrefix}.auth.${this.region}.amazoncognito.com`,
                    clientId: userPoolClient.userPoolClientId,
                }),
            ],
            destinationBucket: siteBucket,
            distribution,
            distributionPaths: ["/*"],
        });

        // ------------------------------------------------------------------
        // Outputs
        // ------------------------------------------------------------------
        new cdk.CfnOutput(this, "UploadBucketName", {
            value: uploadBucket.bucketName,
            description: "Bucket for episode uploads (episodes/) and base scenes (base-scenes/)",
        });
        new cdk.CfnOutput(this, "OutputBucketName", {
            value: outputBucket.bucketName,
            description: "Bucket where rendered videos are stored",
        });
        new cdk.CfnOutput(this, "WebAppUrl", { value: webAppUrl });
        new cdk.CfnOutput(this, "ApiUrl", { value: apiUrl });
        new cdk.CfnOutput(this, "UserPoolId", { value: userPool.userPoolId });
        new cdk.CfnOutput(this, "UserPoolClientId", { value: userPoolClient.userPoolClientId });
        new cdk.CfnOutput(this, "StateMachineArn", { value: stateMachine.stateMachineArn });
    }

    /**
     * Build the render pipeline state machine:
     *   Reconstruct (Fargate) -> Wait/Poll loop -> Encode (Fargate) -> Finalize
     * with a catch-all that records FAILED.
     */
    private buildStateMachine(
        cluster: ecs.ICluster,
        blenderTaskDef: ecs.FargateTaskDefinition,
        blenderContainer: ecs.ContainerDefinition,
        encodeTaskDef: ecs.FargateTaskDefinition,
        encodeContainer: ecs.ContainerDefinition,
        pollerFn: lambda.IFunction,
        finalizeFn: lambda.IFunction,
        uploadBucket: s3.IBucket,
        outputBucket: s3.IBucket,
    ): sfn.StateMachine {
        // Finalize as FAILED (terminal error path).
        // Catch-path finalizer: a Fargate/Lambda step threw, so $.errorInfo holds
        // the cause. The container usually already wrote a concise `error`; this
        // only fills it in as a fallback (finalize uses if_not_exists).
        const finalizeFail = new tasks.LambdaInvoke(this, "FinalizeFail", {
            lambdaFunction: finalizeFn,
            payload: sfn.TaskInput.fromObject({
                episodeId: sfn.JsonPath.stringAt("$.episodeId"),
                sk: sfn.JsonPath.stringAt("$.sk"),
                error: sfn.JsonPath.stringAt("$.errorInfo.Cause"),
            }),
            payloadResponseOnly: true,
        }).next(new sfn.Fail(this, "PipelineFailed", { error: "PipelineFailed" }));

        // Render-failed-on-farm finalizer: reached from the poll Choice, where
        // there is no $.errorInfo — so use a static reason instead.
        const finalizeRenderFailed = new tasks.LambdaInvoke(this, "FinalizeRenderFailed", {
            lambdaFunction: finalizeFn,
            payload: sfn.TaskInput.fromObject({
                episodeId: sfn.JsonPath.stringAt("$.episodeId"),
                sk: sfn.JsonPath.stringAt("$.sk"),
                error: "Deadline render job failed (see the Deadline Cloud job for task details)",
            }),
            payloadResponseOnly: true,
        }).next(new sfn.Fail(this, "RenderFailed", { error: "RenderFailed" }));

        // 1. Reconstruct + prepare + submit (Blender Fargate task).
        const reconstruct = new tasks.EcsRunTask(this, "ReconstructAndSubmit", {
            cluster,
            taskDefinition: blenderTaskDef,
            launchTarget: new tasks.EcsFargateLaunchTarget(),
            integrationPattern: sfn.IntegrationPattern.RUN_JOB,
            assignPublicIp: true,
            subnets: { subnetType: ec2.SubnetType.PUBLIC },
            resultPath: sfn.JsonPath.DISCARD,
            containerOverrides: [
                {
                    containerDefinition: blenderContainer,
                    environment: [
                        { name: "EPISODE_KEY", value: sfn.JsonPath.stringAt("$.key") },
                        { name: "SK", value: sfn.JsonPath.stringAt("$.sk") },
                        { name: "EPISODE_ID", value: sfn.JsonPath.stringAt("$.episodeId") },
                        // "Test render" frame cap (0 = full episode); run.py
                        // passes it to submit_to_deadline as --max-frames.
                        { name: "MAX_FRAMES", value: sfn.JsonPath.stringAt("$.maxFrames") },
                        // Index of this render among the episode's variants
                        // ("0" when there's only one). run.py salts the
                        // randomizer seeds with it, so N variants of one episode
                        // differ instead of reproducing the same scene N times.
                        { name: "VARIANT", value: sfn.JsonPath.stringAt("$.variant") },
                    ],
                },
            ],
        });

        // 2. Wait + poll loop.
        const wait = new sfn.Wait(this, "WaitBeforePoll", {
            time: sfn.WaitTime.duration(cdk.Duration.seconds(45)),
        });
        const poll = new tasks.LambdaInvoke(this, "PollRenderJob", {
            lambdaFunction: pollerFn,
            payload: sfn.TaskInput.fromObject({
                episodeId: sfn.JsonPath.stringAt("$.episodeId"),
                sk: sfn.JsonPath.stringAt("$.sk"),
            }),
            payloadResponseOnly: true,
            resultPath: "$.poll",
        });

        // 3. Encode (ffmpeg Fargate task).
        const encode = new tasks.EcsRunTask(this, "EncodeVideo", {
            cluster,
            taskDefinition: encodeTaskDef,
            launchTarget: new tasks.EcsFargateLaunchTarget(),
            integrationPattern: sfn.IntegrationPattern.RUN_JOB,
            assignPublicIp: true,
            subnets: { subnetType: ec2.SubnetType.PUBLIC },
            resultPath: sfn.JsonPath.DISCARD,
            containerOverrides: [
                {
                    containerDefinition: encodeContainer,
                    environment: [
                        { name: "SK", value: sfn.JsonPath.stringAt("$.sk") },
                        // Per-episode prefix; the encode task writes one MP4 per
                        // camera under it (output/<episodeId>/<Camera>.mp4).
                        { name: "OUTPUT_PREFIX", value: sfn.JsonPath.stringAt("$.outputPrefix") },
                        { name: "CAMERAS", value: "Cam_EyeInHand,Cam_Context" },
                        { name: "PRIMARY_CAMERA", value: "Cam_Context" },
                    ],
                },
            ],
        });

        // 4. Finalize as DONE. The encode task already recorded the per-camera
        //    videoKeys (+ primary videoKey) on the record, so finalize just flips
        //    the status to DONE — the single place a terminal state is reached.
        const finalizeDone = new tasks.LambdaInvoke(this, "FinalizeDone", {
            lambdaFunction: finalizeFn,
            payload: sfn.TaskInput.fromObject({
                episodeId: sfn.JsonPath.stringAt("$.episodeId"),
                sk: sfn.JsonPath.stringAt("$.sk"),
            }),
            payloadResponseOnly: true,
        });

        // Branch on the poll result.
        const choice = new sfn.Choice(this, "RenderDone?")
            .when(
                sfn.Condition.stringEquals("$.poll.renderState", "COMPLETED"),
                encode.next(finalizeDone),
            )
            .when(
                sfn.Condition.stringEquals("$.poll.renderState", "FAILED"),
                finalizeRenderFailed,
            )
            .otherwise(wait);

        // Wire the happy path and attach catches to the FAILED finalizer.
        const catchProps: sfn.CatchProps = { resultPath: "$.errorInfo" };
        reconstruct.addCatch(finalizeFail, catchProps);
        poll.addCatch(finalizeFail, catchProps);
        encode.addCatch(finalizeFail, catchProps);

        const definition = reconstruct.next(wait).next(poll).next(choice);

        const stateMachine = new sfn.StateMachine(this, "RenderPipeline", {
            definitionBody: sfn.DefinitionBody.fromChainable(definition),
            timeout: cdk.Duration.hours(12),
            logs: {
                destination: new logs.LogGroup(this, "RenderPipelineLogs", {
                    retention: logs.RetentionDays.ONE_MONTH,
                    removalPolicy: cdk.RemovalPolicy.DESTROY,
                }),
                level: sfn.LogLevel.ALL,
            },
        });

        // The Blender task reads base assets from the upload bucket; the encode
        // task writes to the output bucket. Grants are on the task roles above.
        void uploadBucket;
        void outputBucket;
        return stateMachine;
    }
}
