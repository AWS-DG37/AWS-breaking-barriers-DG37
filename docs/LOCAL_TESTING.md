# Test the survey worker locally

The local and Fargate paths use the same `mel-worker` graph. This is a batch
worker plus a separate FastAPI console. `docker compose up` starts PostgreSQL and
the console at <http://127.0.0.1:8080>, with live model execution disabled. It does
not start a polling job runner. `docker compose up -d postgres` starts only the
database. See [console usage](explorer.md) for recorded demos and live job setup.

## Install and test without AWS calls

From the repository root:

```bash
uv sync --locked
uv run --locked pytest -q
```

The graph and worker tests inject model responses and test storage. They exercise
workflow behavior, not real Bedrock availability or question quality. The legacy
`dg_agent` tests use the same tracked `docs/participant-pack/` contracts as the
worker, so a fresh checkout needs no separate ignored pack copy.

Run the policy scorecard without credentials, model calls or PostgreSQL:

```bash
uv run --locked mel-worker eval policy
```

It prints a new JSON report path under `outputs/eval/`. See the
[evaluation guide](../eval/README.md) for the dataset checks, separate legacy
baseline, metric coverage and current disagreements. This is a repository-local
evaluation path: the full source and split manifest are not included in the
image. The image includes `eval/golden_holdout.jsonl` for the console's rule and
comparison checks; run `eval policy` from a checkout with all evaluation assets.

## Iterate on prompts with real Bedrock

Use your authenticated workshop environment. With a named profile:

```bash
export AWS_PROFILE=workshop
export MEL_AWS_REGION=us-east-1
uv run --locked mel-worker check-model
uv run --locked mel-worker preview
```

If using exported temporary AWS access/secret/session credentials, unset
`AWS_PROFILE` and `AWS_DEFAULT_PROFILE` and omit profile assignments in this guide.
Check the selected account with `aws sts get-caller-identity`.

`preview` defaults to the first 10 nonblank fixture records and writes a fresh directory
under `outputs/local/`. Inspect `results.jsonl`, `summary.json`, `run.json` and
`bundle.json`. Rejected drafts are evidence too; a zero process exit does not mean
all surveys were accepted. This path calls Bedrock but needs no database or AWS
deployment. Use a one-row JSONL input with `--input` for the smallest experiment.
Use `--full` only when you intend to process the entire selected input.

Model IDs, reasoning effort and token budgets are in `config/agent.yaml`.
Generator and reviewer settings are separate. Null IDs use
`MEL_BEDROCK_MODEL_ID`, currently `openai.gpt-6-luna`. Prompt edits are reread on
each new run; no container rebuild is needed for native preview.

## Exercise PostgreSQL checkpoints and the scheduler outbox

On **macdaddy-laptop**, start Colima if it is stopped. With Docker Desktop on
another machine, start that engine instead.

```bash
colima start
docker compose -p dg37-evals up -d --wait postgres
AWS_PROFILE=workshop MEL_AWS_REGION=us-east-1 MEL_DATABASE_SSLMODE=disable \
  uv run --locked mel-worker run \
  --input tests/fixtures/smoke_insights.jsonl \
  --output outputs/local-db-results.jsonl
```

The SSL override applies to this local command because the local PostgreSQL
container has no TLS setup. The Fargate configuration retains certificate
verification. Each run has an identity and configuration fingerprint; keep its
run ID when resuming unchanged work, and use a new ID for a different experiment.

An accepted survey writes an `insight_results` row and `survey_handoffs` row in
one transaction. This outbox is the boundary for the scheduler team's future
application. It does not deliver a message or enforce the farmer's weekly cap.

After testing:

```bash
docker compose -p dg37-evals down
```

This preserves the named database volume. If you started Colima solely for the
test and no other work needs it, stop it with `colima stop`.

## Check the Fargate image locally

```bash
docker build --platform linux/amd64 -t dg37-mel:evals-local .
docker run --rm --platform linux/amd64 dg37-mel:evals-local --help
```

The image runs as user `10001`. Its default command prints help and makes no
inference request. Actual container runs need temporary AWS credentials and a
reachable PostgreSQL instance. Keep credentials in runtime environment/provider
configuration, never in the image, committed files or command-line values.

## Verification recorded on 3 November 2026

On `ben/evals-and-checks`, after integrating upstream `9ad0578`:

- 251 combined tests passed before the offline scorer was added, including a
  fresh-checkout schema-validation test.
- Worker/launcher Python lint, deployment shell syntax and CloudFormation lint
  passed. The unchanged documentation builder has 44 pre-existing E501/E741 lint
  findings; those are separate from the worker checks.
- The Linux AMD64 image built and ran as user `10001`.
- The configured Mantle model returned `OK` using the workshop AWS profile.
- One eligible synthetic insight ran inside the container and produced an
  accepted three-question Hindi survey. Both the survey and handoff passed the
  retained schema evaluator and deterministic survey checks.
- PostgreSQL contained one accepted result and one handoff. Resuming the same
  container command added no model calls or duplicate handoffs.

This proves a local integration path, not production delivery or a validated MEL
quality score. The live worker reported `usage_complete=false`; no token total
or dollar estimate is inferred from its zero-valued counters. No cloud resources
were deployed for these checks.

After adding the offline policy scorer, 276 combined tests passed. The scorer
also ran with a nonexistent AWS profile, reporting 132/138 held-out eligibility
agreement and the independent 145/150 legacy baseline. Worker/launcher lint and
the new evaluation tests passed. The scorecard's unassessed quality dimensions
and unresolved policy/timing diagnostics are documented in the evaluation guide.
The updated AMD64 image built, printed CLI help, and imported both the scorer
and retained reference package as user `10001`. No new live inference run was
needed for this policy-only slice.
