from __future__ import annotations

import os
import shutil
import subprocess
import sys

import jsii
from aws_cdk import (
    BundlingOptions,
    CfnOutput,
    Duration,
    ILocalBundling,
    Stack,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
)
from constructs import Construct

LAMBDA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lambdas", "cost_agent")


@jsii.implements(ILocalBundling)
class PipLocalBundling:
    """Install the Lambda's dependencies with the local pip, so no Docker is needed.

    pip downloads Linux ARM64 wheels for Python 3.13 whatever OS you deploy from.
    If this fails, CDK falls back to bundling inside the Lambda build image (Docker).
    """

    def try_bundle(self, output_dir: str, *_args, **_kwargs) -> bool:
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet",
                 "-r", os.path.join(LAMBDA_DIR, "requirements.txt"),
                 "--target", output_dir,
                 "--platform", "manylinux2014_aarch64", "--implementation", "cp",
                 "--python-version", "3.13", "--only-binary=:all:", "--upgrade"],
                check=True,
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"Local bundling failed ({exc}); falling back to Docker", file=sys.stderr)
            return False
        for name in os.listdir(LAMBDA_DIR):
            if name.endswith(".py"):
                shutil.copy2(os.path.join(LAMBDA_DIR, name), output_dir)
        return True


class CostAlertAgentStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        ctx = self.node.try_get_context
        alert_email = ctx("alert_email")
        model_id = ctx("model_id") or "anthropic.claude-opus-5-5"
        bedrock_region = ctx("bedrock_region") or self.region
        schedule = ctx("schedule_cron") or "cron(0 14 * * ? *)"

        # ---------- Alerts ----------
        topic = sns.Topic(self, "CostAlertTopic", display_name="AWS cost spike alerts")
        if alert_email:
            topic.add_subscription(subs.EmailSubscription(alert_email))

        # ---------- The agent Lambda: detector + Claude tool loop ----------
        runtime = _lambda.Runtime.PYTHON_3_13
        fn = _lambda.Function(
            self, "CostAgentFn",
            description="Detects AWS cost spikes and has Claude on Bedrock explain them",
            runtime=runtime,
            architecture=_lambda.Architecture.ARM_64,
            handler="handler.handler",
            code=_lambda.Code.from_asset(
                LAMBDA_DIR,
                bundling=BundlingOptions(
                    image=runtime.bundling_image,
                    platform="linux/arm64",
                    command=["bash", "-c",
                             "pip install -r requirements.txt -t /asset-output && cp *.py /asset-output"],
                    local=PipLocalBundling(),
                ),
            ),
            timeout=Duration.minutes(5),
            memory_size=512,
            log_group=logs.LogGroup(self, "CostAgentLogs", retention=logs.RetentionDays.TWO_WEEKS),
            environment={
                "TOPIC_ARN": topic.topic_arn,
                "MODEL_ID": model_id,
                "BEDROCK_REGION": bedrock_region,
                "EFFORT": str(ctx("effort") or "medium"),
                "MAX_TOOL_CALLS": str(ctx("max_tool_calls") or 6),
                "LOOKBACK_DAYS": str(ctx("lookback_days") or 14),
                "PCT_THRESHOLD": str(ctx("pct_threshold") or 30),
                "MIN_ABS_INCREASE": str(ctx("min_abs_increase") or 5),
                "Z_THRESHOLD": str(ctx("z_threshold") or 2),
            },
        )

        # Read-only cost data. Cost Explorer has no resource-level permissions, hence "*".
        fn.add_to_role_policy(iam.PolicyStatement(actions=["ce:GetCostAndUsage"], resources=["*"]))
        # Claude on Amazon Bedrock through the Messages API endpoint (bedrock-mantle).
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock-mantle:CreateInference"],
            resources=[f"arn:{self.partition}:bedrock-mantle:{bedrock_region}:{self.account}:project/*"],
        ))
        topic.grant_publish(fn)

        events.Rule(
            self, "DailyCostCheck",
            description="Runs the AWS cost spike check once a day",
            schedule=events.Schedule.expression(schedule),
            targets=[targets.LambdaFunction(fn, retry_attempts=1)],
        )

        # Email the owner if the checker itself breaks, so silence never hides a failure.
        alarm = cloudwatch.Alarm(
            self, "CostAgentErrors",
            alarm_description="The AWS cost alert agent Lambda failed",
            metric=fn.metric_errors(period=Duration.days(1), statistic="Sum"),
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(cw_actions.SnsAction(topic))

        CfnOutput(self, "TopicArn", value=topic.topic_arn)
        CfnOutput(self, "FunctionName", value=fn.function_name)
        CfnOutput(self, "ModelId", value=model_id)
