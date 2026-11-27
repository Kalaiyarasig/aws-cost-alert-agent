# Deploy guide

This takes about 20 minutes the first time. Commands are for macOS or Linux. On Windows, use WSL or swap `source .venv/bin/activate` for `.venv\Scripts\activate`.

## 1. Install the tools

| Tool | Check | Install |
| --- | --- | --- |
| Python 3.12 or newer | `python3 --version` | python.org or your package manager |
| Node.js 20 or newer | `node --version` | nodejs.org |
| AWS CLI v2 | `aws --version` | https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html |
| AWS CDK CLI | `cdk --version` | `npm install -g aws-cdk` |

Docker is **not** required. The stack installs the Lambda's Python packages with your local pip, and only falls back to Docker if that fails.

## 2. Prepare the AWS account

1. **Sign in with an admin user or role** for the first deploy. Avoid the root user for day-to-day work: create an IAM Identity Center user, then run `aws configure sso`. A quicker option for a personal sandbox is an IAM user's access keys with `aws configure`.
2. Check it works:
   ```bash
   aws sts get-caller-identity
   ```
3. **Pick a region** where your model is offered on Bedrock (Nova and Claude both are), for example `us-east-1`, and use it for everything below:
   ```bash
   export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1
   ```
4. **Enable Cost Explorer**: Billing and Cost Management console, Cost Explorer, Launch Cost Explorer. On a new account it can take up to 24 hours before data appears.
5. **Model access in Bedrock**: the default model, Amazon Nova 2 Lite, works on any account, including the AWS Free plan, with no extra steps. To use Claude instead, your account needs a paid AWS plan and the one-time Anthropic use-case form in the Bedrock console; then deploy with `-c model_id=anthropic.claude-opus-5-5`.
6. Optional but recommended: set up an **AWS Budget** with an email alert as a safety net while you experiment.

## 3. Get the code and run the tests

```bash
cd aws-cost-alert-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

All tests should pass. They mock AWS and the model, so they cost nothing.

## 4. Deploy

```bash
cdk bootstrap                                   # once per account and region
cdk diff -c alert_email=you@example.com          # preview what will be created
cdk deploy -c alert_email=you@example.com
```

The stack creates one Lambda function and its IAM role, an SNS topic with your email subscription, an EventBridge daily rule, a CloudWatch alarm and a log group.

When the deploy finishes, **open the email from AWS Notifications and click "Confirm subscription"**. Until you do, no alerts are delivered.

## 5. Test it

Send a test alert now instead of waiting for a real spike. `force` sends an explanation even on a normal day:

```bash
FN=$(aws cloudformation describe-stacks --stack-name CostAlertAgent \
  --query "Stacks[0].Outputs[?OutputKey=='FunctionName'].OutputValue" --output text)

aws lambda invoke --function-name "$FN" \
  --cli-binary-format raw-in-base64-out \
  --payload '{"force": true}' out.json && cat out.json
```

`{"force": true, "date": "2026-11-20"}` checks a specific day instead of yesterday.

You should get an email within a minute. To watch what the agent did, including each tool call:

```bash
LOGS=$(aws logs describe-log-groups --log-group-name-prefix CostAlertAgent \
  --query "logGroups[0].logGroupName" --output text)
aws logs tail "$LOGS" --since 10m --follow
```

## 6. Tune it

Redeploy with different values at any time:

```bash
cdk deploy -c alert_email=you@example.com -c pct_threshold=50 -c min_abs_increase=20
cdk deploy -c alert_email=you@example.com -c model_id=us.amazon.nova-pro-v1:0          # larger Nova model
cdk deploy -c alert_email=you@example.com -c model_id=anthropic.claude-opus-5-5        # Claude (paid AWS plan)
```

All options are listed in the README's Configuration table.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| No email at all | Confirm the SNS subscription, and check the spam folder |
| Email says "AI explanation was unavailable" | Read the Lambda logs. Usually the model isn't available to the account (step 2.5), or isn't offered in this region (`-c bedrock_region=us-east-1`) |
| `... is not available for this account` for a Claude model | The account is on the AWS Free plan. Use the default Nova model, or upgrade the plan |
| `AccessDenied` on `bedrock-mantle:CreateInference` in the logs | Check model access first. If it persists, look at the denied resource ARN in the log message and widen the policy resource in `cost_alert_agent/stack.py` to match |
| `DataUnavailableException` from Cost Explorer | Cost Explorer was enabled recently; wait up to 24 hours. The daily run steps back up to 3 days on its own; for a manual test pass an older `date` |
| `Not enough history` in the result | The account has under 3 days of cost data; this resolves itself |
| Local bundling errors during deploy | Make sure `pip` works in your virtualenv, or install Docker and CDK will use it instead |

## Clean up

```bash
cdk destroy
```

This removes everything the stack created. The CDK bootstrap stack (`CDKToolkit`) stays; delete it in CloudFormation if you no longer use CDK in that region.

## Push to GitHub

The project is already a git repository with an initial commit. After you create an **empty** repository on GitHub (no README or .gitignore):

```bash
git remote add origin https://github.com/<your-user>/aws-cost-alert-agent.git
git push -u origin main
```
