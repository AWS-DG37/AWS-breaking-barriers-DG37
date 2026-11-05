# Run locally with your own workshop AWS account

Use your own workshop account and credits to test real Bedrock calls before a PR.
Run PostgreSQL in Docker and the API and job runner on your computer. Each host
process uses your AWS credentials. No teammate deployment is needed.

Signing into the shared website with a Cognito email does not select an AWS billing
account. That website uses the shared host role. To spend your own credits, use
this local API and runner. Identical IAM role names in different accounts do not
grant access to the shared stack. Do not copy another teammate's credentials.

## Prepare a checkout

Use bash or zsh on macOS, Linux, or WSL2. Install Git, uv, Python 3.12 or 3.13,
Docker with Compose, Node.js, and AWS CLI v2. Start the Docker engine.

For a fresh checkout:

```bash
git clone --branch main https://github.com/AWS-DG37/AWS-breaking-barriers-DG37.git
cd AWS-breaking-barriers-DG37
uv sync --locked
```

For an existing checkout, preserve uncommitted work and your current branch before
updating. Do not reset it to follow this guide. Run `uv sync --locked` at its root.

Create a local environment file from that root. The file is ignored by Git and
contains only local settings. Keep AWS credentials and profile choices out of it.
The isolated Compose project and ports leave an existing instance on 8877 and
PostgreSQL on 55437 running.

```bash
mkdir -p outputs
cat > outputs/local-env.sh <<'EOF'
unset MEL_AUTH_DOMAIN MEL_AUTH_CLIENT_ID MEL_AUTH_ISSUER MEL_CONSOLE_URL
unset MEL_DATABASE_SECRET_ARN MEL_DATABASE_SSLROOTCERT
unset MEL_JOB_CLUSTER MEL_JOB_TASK_DEFINITION MEL_JOB_CONTAINER
unset MEL_JOB_SUBNETS MEL_JOB_SECURITY_GROUPS
unset MEL_ALLOWED_MODELS MEL_VOICE_BUCKET MEL_VOICE_KEY_SECRET_ARN
export MEL_CONFIG_PATH=config/agent.yaml
export MEL_AWS_REGION=us-east-1
export MEL_BEDROCK_MODEL_ID=openai.gpt-6-luna
export MEL_BEDROCK_PROJECT_ID=default
export MEL_DATABASE_HOST=127.0.0.1
export MEL_DATABASE_PORT=55438
export MEL_DATABASE_NAME=farmerchat
export MEL_DATABASE_USER=mel
export MEL_DATABASE_PASSWORD=mel-local-only
export MEL_DATABASE_SSLMODE=disable
export MEL_LOCAL_DATABASE_PORT=55438
export MEL_PACK_URI=docs/participant-pack/data/insights.jsonl
export AWS_EC2_METADATA_DISABLED=true
EOF
source outputs/local-env.sh
```

Source this file from the repository root in every API, runner, and test terminal.
It clears inherited hosted sign-in, database-secret, and ECS job settings.
It does not load automatically.

## Check the recorded workflow first

This step needs no AWS credentials and makes no model calls.

```bash
docker compose -p dg37-local up -d --wait postgres
uv run --locked mel-explorer --host 127.0.0.1 --port 8765 --no-live
```

Open [the local console](http://127.0.0.1:8765). In **Compare**, select
**Import recorded run**. Inspect a version, an insight, its journey, and the survey
preview. Check the phone-width browser layout too. This validates the local import
and inspection workflow, not live model access.

Keep the API bound to loopback. The local console has no sign-in.
The default Compose `explorer` service also uses `--no-live`, does not mount AWS
credentials, and starts no job runner. Do not put keys into Docker or source files.

## Select your own AWS identity

Choose one credential method in each terminal that will run the API or runner.

For an existing named profile, replace the example profile name with your own:

```bash
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN
unset AWS_DEFAULT_PROFILE
export AWS_PROFILE=your-workshop-profile
```

Only if your workshop supplies IAM Identity Center access, follow the
[AWS SSO setup instructions](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html)
and sign into that profile with `aws sso login --profile your-workshop-profile`.
A workshop portal with temporary keys does not imply SSO support.

For temporary credentials, unset profile selection, then paste the three current
exports from **your own** workshop portal into the terminal:

```bash
unset AWS_PROFILE AWS_DEFAULT_PROFILE AWS_SECURITY_TOKEN
# Paste the portal's AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, and AWS_SESSION_TOKEN exports here.
```

Do not commit those exports, paste them into chat, or copy them between teammates.
Raw environment credentials override profile credentials. See the
[AWS credential environment rules](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-envvars.html).

Verify the account and ARN with both the AWS CLI and this project's Python SDK:

```bash
aws sts get-caller-identity --region "$MEL_AWS_REGION"
uv run --locked python - <<'PYIDENTITY'
import boto3
import os
identity = boto3.client("sts", region_name=os.environ["MEL_AWS_REGION"]).get_caller_identity()
print({key: identity[key] for key in ("Account", "Arn")})
PYIDENTITY
```

Both results must identify your workshop account and intended role before any paid
step. These STS calls do not invoke a model. Repeat them in the runner terminal.
Discovery and STS success do not prove inference permission or tool support.

## Start a bounded live console run

Stop the recorded API with Ctrl-C. In the same authenticated terminal, start it
without `--no-live`:

```bash
uv run --locked mel-explorer --host 127.0.0.1 --port 8765
```

In a second terminal, enter the same checkout, source `outputs/local-env.sh`, select
the same AWS identity, and repeat both identity checks. Then start the runner:

```bash
uv run --locked mel-worker jobs
```

The API queues work. The runner executes queued jobs. Starting these processes does
not generate a survey. Existing queued jobs can execute as soon as the runner starts.

1. Open **Configure** and choose an available model and API for both Generate and
   Review. Save a runnable version. The baseline uses `openai.gpt-6-luna` in
   `us-east-1`. `MEL_BEDROCK_MODEL_ID` fills only null YAML model IDs. Explicit IDs
   take precedence. Choose the API separately. The fallback does not change saved
   database versions.
2. In **Generate** or **Compare**, set the cumulative console spend cap before
   selecting **Run**. The cap starts unset and refuses paid console jobs.
3. Select one insight and one saved version. Enter an explicit per-run budget.
   The form defaults to $5. Compare and Configure tests apply that limit per
   candidate, so two candidates at $5 allow $10 combined.
4. Check **Before you run**, then start the run. Inspect its status, cost records,
   journey, and result before expanding the selection.

A budgeted model needs a verified price row for its exact model, API, and region.
If the selection is unpriced, choose a priced available model or add verified prices
as described in [run costs and budgets](explorer.md#run-costs-and-budgets).
Do not raise the cap automatically to bypass an error. Conservative reservations
use the model's full context bound and can refuse a small budget before any call.
No universal dollar amount guarantees a run. These controls estimate configured
prices, not an AWS invoice or an account-wide hard cap.

This app uses AWS SigV4 for Mantle Responses, with
`bedrock-mantle:CreateInference`, or Converse, with `bedrock:InvokeModel` and any
required inference-profile permissions. It needs no OpenAI API key. Check your
account's permissions and the selected model's tool support against the
[AWS inference endpoint documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-responses-api.html)
and [Converse permissions](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html).

**Run**, **Run comparison**, **Test against parent**, and **Improve a configuration**
can call models and incur AWS charges. Reading recorded runs and checking rules do not.

## Use CLI preview only if you accept unbudgeted calls

`check-model` and `preview` are paid, optional shortcuts. They bypass the console
spend cap and persisted request-cost ledger. Prefer the bounded console above.
If you accept that limit, use the same verified host identity and local environment:

```bash
uv run --locked mel-worker check-model
head -n 1 tests/fixtures/smoke_insights.jsonl > outputs/one-insight.jsonl
uv run --locked mel-worker preview --input outputs/one-insight.jsonl
```

Preview defaults to ten input records. The one-row file restricts this example to
one insight, but generation, review, retries, and revisions can make multiple calls.
Do not add `--full`. Preview has no `--limit` or `--concurrency` flags.
Inspect `outputs/local/<run-id>/run.json`, `bundle.json`, and `results.jsonl`.
See [fast local prompt iteration](../README.md#fast-local-prompt-iteration) for artifact details.

## Check changes before a PR

Local UI versions live only in your local PostgreSQL database. Commit intended
source YAML, prompt, or code changes on a branch for review. Never commit keys or
local outputs. Do not retarget `deploy/target.json` to your account for local work.
You do not need SAM or `scripts/deploy.sh` here.

Use a dedicated test database. Tests initialize tables, roll back test transactions,
and create temporary schemas. Do not point them at a shared or valuable database.
With this guide's isolated PostgreSQL running, run the existing offline CI checks:

```bash
uv run --locked ruff check src deploy
uv run --locked cfn-lint template.yaml infra/bootstrap.yaml deploy/*.yaml
uv run --locked playwright install chromium
MEL_TEST_DB_PORT=55438 uv run --locked pytest -q
node --test src/mel/explorer/static/*.test.mjs
```

On Linux, install Playwright's system dependencies if needed with
`uv run --locked playwright install --with-deps chromium`.
The [CI workflow](../.github/workflows/main.yml) also builds and smoke-tests the image.
Branch PR checks need no AWS credentials. Iterate through a PR rather than pushing
to `main`. A push or merge to `main` builds and deploys the shared URL.

## Stop and preserve your work

Stop the API and runner with Ctrl-C in their terminals, then stop PostgreSQL:

```bash
docker compose -p dg37-local stop postgres
```

This preserves the database volume and saved versions. Do not use `down -v`.
No AWS account cleanup is needed for this local setup.

## Resolve common failures

- Wrong account or expired token. Refresh your own credentials, clear the other
  credential method, and repeat both identity checks in both terminals.
- Access denied. Check the exact account, region, API, model, and inference-profile
  permissions. A discovered model is not proof of permission to invoke it.
- Job stays queued. Start `mel-worker jobs` with the API's database and AWS identity.
- Unpriced model or reservation refusal. Check the exact price row and reservation
  rules in [run costs and budgets](explorer.md#run-costs-and-budgets).
- Live execution disabled. Restart the host API without `--no-live`.
- Port already in use. Stop only your conflicting process, or choose unused API
  and database ports and update the environment and commands consistently.
