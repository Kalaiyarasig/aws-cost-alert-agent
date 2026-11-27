#!/usr/bin/env python3
import aws_cdk as cdk

from cost_alert_agent.stack import CostAlertAgentStack

app = cdk.App()
CostAlertAgentStack(
    app, "CostAlertAgent",
    description="Daily AWS cost spike detection with a model on Amazon Bedrock (Nova or Claude) explaining spikes in plain language",
)
app.synth()
