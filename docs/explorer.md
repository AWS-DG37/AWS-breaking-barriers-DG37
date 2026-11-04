# MEL Console: local and hosted usage

The console has Generate, Runs, Compare and Configure sections. It generates
surveys, browses saved outcomes and compares configuration versions using the
worker's graph, prompts, schemas and PostgreSQL store. Schedule/delivery,
response write-back and a feedback editor are not implemented.
The [architecture walkthrough](architecture-walkthrough.html) explains the flow.
The [mac-mini handoff](handoffs/mac-mini-setup.md) preserves historical setup and
visual direction; use this guide for the current commands and navigation.

## Start the recorded demo with Docker

From the repository root, with a running Docker engine:

```bash
MEL_LOCAL_DATABASE_PORT=55437 MEL_EXPLORER_PORT=8877 \
  docker compose -p dg37-explorer up --build -d
```

Open <http://127.0.0.1:8877>, select **Compare**, then **Import recorded run**. This imports an
actual Bedrock comparison on one synthetic mustard insight. OpenAI and Anthropic
produced accepted surveys. Amazon Nova's original execution failure remains visible.
This example demonstrates the inspection workflow; one scenario cannot establish
which model performs best overall.

Select a version, insight, recorded journey step, and question. Inspect the draft,
review findings, revision, and final ruling. The comparison includes failures and
missing cases. Unknown token usage remains unknown. Question explanations are
model-supplied diagnostics. Relevance is a survey-wide model judgment.

Choose **Saved execution rules** or **Current golden reference** to reanalyze the
saved outputs. This makes no model calls. Golden matching requires an identical
source insight, and the response identifies the current reference file's SHA-256.
Legacy participant-pack checks are diagnostic evidence with their own fixed rules;
their language check compares metadata and their PII/advice patterns cover English.
Independent MEL and Hindi review remain necessary for a quality claim.

The Docker explorer disables live execution by default and needs no AWS credentials
for recorded comparisons. It does not start a job runner. The image omits the full
participant dataset; **Load participant pack** needs a readable `MEL_PACK_URI` (see
[datasets and imports](#datasets-and-imports)).
Stop this instance while retaining the database volume:

```bash
docker compose -p dg37-explorer down
```

## Start a bounded live comparison

Start PostgreSQL with the same compose project, then run the API in the host's
authenticated AWS environment. Temporary workshop credentials must still be valid.
The examples use a named `workshop` profile. If credentials are exported directly
as `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN`, unset
`AWS_PROFILE`/`AWS_DEFAULT_PROFILE` and omit `AWS_PROFILE=workshop` from both commands.
Use `MEL_AWS_REGION=us-east-1` in both terminals if overriding the default region.

```bash
MEL_LOCAL_DATABASE_PORT=55437 docker compose -p dg37-explorer up -d postgres
uv sync --locked
AWS_PROFILE=workshop AWS_EC2_METADATA_DISABLED=true \
  MEL_DATABASE_PORT=55437 MEL_DATABASE_SSLMODE=disable \
  uv run --locked mel-explorer --port 8765
```

Start a job runner in a second terminal, with the same environment. The API
queues batch and comparison jobs; the runner executes them:

```bash
AWS_PROFILE=workshop AWS_EC2_METADATA_DISABLED=true \
  MEL_DATABASE_PORT=55437 MEL_DATABASE_SSLMODE=disable \
  uv run --locked mel-worker jobs
```

Open <http://127.0.0.1:8765>, open **Compare** and select **New comparison**. Pick one
to six saved configuration versions and a dataset selection (at most fifty insights);
every version runs the same insights. Model discovery does not prove access or
structured-output compatibility. Unsupported calls remain failures; there is no
automatic provider or API substitution.

**Run comparison**, **Test against parent** and **Run** in Generate invoke models
and incur AWS usage. Reading saved runs, changing analysis references and checking
rules do not invoke models. Each runner executes one
job at a time; start more runners for more concurrency.

## Edit, test and save a configuration

**Configure** lists the saved versions (the active one first), test drafts, and
retired versions. A version's page shows what it changed from its parent, its rules'
agreement with the golden examples (instant, no model calls), each pipeline step's
settings and prompts, and the comparisons it took part in.

**Edit as draft** copies a version into an editor organized by step: Decide (human-call
severities, send-window policy and days, the rules file), Generate and Review (model,
reasoning effort, temperature, token budget, prompt with a line diff against the
parent, acceptance threshold), and Loop (revisions, timeouts, concurrency). Each step
shows how many settings changed. Edits stay in the browser tab until you act:

- **Test against parent** keeps the draft as a draft, named after what it changed (or a
  test name you give), runs it beside its parent on the chosen insights, and opens the
  comparison. The page shows the versions and progress until results arrive.
- **Save version** (or **Save as version** on a tested draft) makes it a saved version.
  Saved versions are the runnable ones: Generate lists them, and **Make active** makes
  one the default (the previous one is kept as retired).

The **Send window timing source** choices have different meanings:

- **Timing inferred from golden examples** (`golden_v1`): our interpretation of
  the supplied example surveys. The baseline uses 09:00–19:00, seasonal days 30–45,
  and no action-window/minimum-width constraint. Its editable offsets and times
  apply only to this choice.
- **Timing from participant pack’s written rules** (`pack`): follows `rules.json`,
  including 07:00–19:00, seasonal days 21–45, waiting until the action window ends,
  and a minimum two-day width.

The supplied sources disagree; Digital Green has not confirmed precedence. This
selects runtime timing behavior, not the input dataset or evaluation reference.
The agreement score compares against the 138 golden holdout cases. Written-rule
compliance can reduce that score. The rules were developed using all 150 source
cases, so the score is a regression check, not independent unseen-case evidence.

Model labels identify the API connection used by this app: **Mantle** means the
OpenAI-compatible Responses API on Bedrock's Mantle endpoint; **Converse** means
Bedrock's native Converse API. Both use AWS credentials, and generator and reviewer
can choose separately. The app sends the reasoning-effort setting only for
Responses agents. Model availability and tool support must be checked for the
selected API and account.

A definition must validate before it is saved: prompts cannot be empty, and hosted
versions may only use the models the worker may call (`MEL_ALLOWED_MODELS`). The stack
allows GPT-6 Luna through Mantle (`BedrockAllowedModelIds`) and the Converse models in
`ConverseModelIds`: Claude Haiku, Sonnet and Opus 5.5, Nova Pro, Nova 2 Lite, Mistral
Large 3, DeepSeek V3.2 and GPT 5.5. These are configured candidates, not a guarantee that every workshop account can
invoke them. Structured survey output requires tool support; prior tool-call
checks do not replace verification in the deployment's account. Converse agents never force the
output tool (`mel.agents.converse_profile`), because Claude Sonnet and Opus 5.5 refuse
a forced tool choice. Comparison results never enter the scheduler
handoff queue.

A local API defaults to loopback and has no sign-in. Keep it local.

## The hosted console

`scripts/deploy.sh` also deploys the console: one CloudFront distribution serves the
UI from a private S3 bucket and `/api/*` from API Gateway, which invokes this same
image as a Lambda function in a private subnet. The browser therefore talks to one
origin; there is no CORS. Each queued job starts its own Fargate task
(`mel-worker jobs --job-id <id>`).

Sign-in runs on the server (`src/mel/explorer/auth.py`). `/api/auth/login` sends the
browser to Cognito with PKCE; `/api/auth/callback` exchanges the code and sets an
HttpOnly, Secure, `SameSite=Strict` session cookie holding the access token, which is
checked against Cognito's signing keys on every other `/api` request. The page never
handles a token. Administrators invite users; there is no self sign-up:

```bash
AWS_PROFILE=workshop bash scripts/add_console_user.sh person@example.org
```

The console opens on **Generate**: choose a dataset (load the participant pack from the
empty state the first time), filter it, pick a configuration version, and check
**Before you run**, which applies the rules to the selection without model calls and
shows how many insights get a survey, which rules skip the rest, and the rough number
of model calls. **Run** queues a batch job; its page shows progress, outcome counts and
every result, and selecting one shows the recorded Insight journey and a survey
preview. Runs and Compare share the journey view: it preserves event order,
revision attempts and early exits. Select a step for saved evidence; missing
history is never reconstructed. **How the process works** explains the possible
branches separately. Acceptance includes a handoff payload, not delivery.
**Compare** is the model comparison described above. **Runs** browses saved production runs without configuring a new run. **Configure**
edits, tests, saves and activates versions. Schedule and Feedback are not available
as console sections yet. Hosted, `deploy.sh` copies the pack to the data
bucket, from which the API imports it (`MEL_PACK_URI`).

The worker's IAM policy authorizes Mantle inference for `BedrockAllowedModelIds` and
Converse inference for Bedrock models and inference profiles; the console limits
the model/API combinations versions may use. Deployment prerequisites, account/profile setup and the
update command are in [the README](../README.md#aws-deployment).

## Datasets and imports

A fresh Generate screen offers **Load participant pack**, importing the 6,999
synthetic insights into PostgreSQL. Hosted deployment uploads that file to S3 and
sets `MEL_PACK_URI` for the API. Importing does not generate surveys or invoke models.
The initial Baseline configuration is seeded from the bundled YAML if there is no
active configuration.

Custom uploads currently have an authenticated JSON API, not a file-picker UI (the inner JSONL below is schematic):

```http
POST /api/datasets
Content-Type: application/json

{
  "name": "My dataset",
  "jsonl": "{\"insight_id\":\"example-1\", ...}\n..."
}
```

The `jsonl` string must contain actual JSON objects, one per line; the ellipses
above are placeholders. Follow the tracked
[insight schema](participant-pack/json/schemas/insight.schema.json) and use unique
insight IDs within the file. Invalid JSON or duplicate IDs rejects the import;
malformed/missing-ID objects can be retained as junk for rejection by the rules.
`POST /api/datasets/import-pack` imports the configured pack. Hosted API calls
require the console's sign-in cookie, not just AWS CLI credentials. Imported
datasets are shared in the deployment's database, not private to one login.

For local development, the API can read the tracked pack file directly when run
from the checkout. Import via CLI against your configured PostgreSQL:

```bash
uv run --locked mel-worker dataset import --input docs/participant-pack/data/insights.jsonl
uv run --locked mel-worker dataset list
```

With the Compose API, which omits that file from its image, a host CLI can import
into the same database (`MEL_DATABASE_PORT=55437 MEL_DATABASE_SSLMODE=disable` for
the example above). The hosted RDS database is private; the local CLI is not a
substitute for an authenticated hosted API import.

## Change a team-owned component

| Concern | Owner |
| --- | --- |
| Rule semantics and windows | `src/mel/decision/` |
| Flow and emitted stage evidence | `src/mel/graph.py` |
| Models, budgets, and revision settings | `config/agent.yaml` |
| Native SDK construction and catalog | `src/mel/agents.py`, `src/mel/bedrock_models/` |
| Grouped experiments and comparison analysis | `src/mel/experiments/` |
| Configuration versions, datasets and the job queue | `src/mel/configs.py`, `src/mel/datasets.py`, `src/mel/jobs.py` |
| Golden reference and holdout provenance | `eval/` |
| Database schema | New numbered files in `migrations/` |
| HTTP boundary and browser UI | `src/mel/explorer/` |

Migrations run once under a database lock. Applied migration files are immutable;
add a new migration for a schema change. Experiment runs retain model settings,
prompts, rules, source context, outcomes, and captured stage events. Events persist
when an insight finishes, so a process crash can lose its in-flight trace.

Human assessments use run, insight, and item IDs to keep repeated `q1` identifiers
separate. The API accepts explicitly supplied human ratings; the first UI displays
saved ratings and does not collect them. Additional ingestion UI and scheduler
integration can use these existing boundaries.

## Verify a change

```bash
uv run --locked pytest -q
node --test src/mel/explorer/static/*.test.mjs
MEL_TEST_DB_PORT=55437 uv run --locked pytest tests/test_postgres_integration.py -q
```

`scripts/verify_explorer.py` targets older prototype labels and composer controls;
it needs updating before it can verify the current console. Do not treat that
script as a current end-to-end check. For a manual browser check, use Runs/Compare
to select an insight and revision step, inspect evidence, and exercise the phone's
Start, required answers, Next, Back and Restart controls.

The PostgreSQL tests use rollback-only transactions. They check experiment/outbox
isolation, atomic result/handoff writes, and migration history. Live inference and
browser checks are separate from the offline suite.

## Build the standalone architecture viewer

From the repository root, with Python available:

```bash
uv run --locked python scripts/build_architecture_viewer.py /tmp/dg37-architecture
uv run --locked python -m http.server 8766 --directory /tmp/dg37-architecture
```

Open <http://127.0.0.1:8766>. Stop the server with Ctrl-C when finished.
The build copies the shared phone player, CSS and field photo into the output.
It derives a small candidate catalog from `src/mel/explorer/demo/recorded-run.json`.
The catalog retains OpenAI Luna and Anthropic Haiku outputs and Amazon Nova Lite's
recorded failure. It does not invoke models or call the explorer API.

`mountSurveyPlayer` in `src/mel/explorer/static/survey-player.mjs` is the seam
shared by the app and the standalone viewer. It owns intro, question navigation,
required answers, completion and restart. Answers live only in browser memory.
Changing the candidate resets them. App stage and reference inspection preserve
them for the same candidate and survey. Model explanations remain diagnostic
and do not establish independent question quality.

Run the browser check against a temporary build with an automatically stopped
server, or point it at an existing hosted viewer:

```bash
uv run --locked python scripts/verify_architecture_viewer.py
uv run --locked python scripts/verify_architecture_viewer.py --url http://127.0.0.1:8766
```


## Run costs and budgets

Persisted runs record each generation and review request in PostgreSQL, including
output retries and graph revisions. The ledger keeps the model, native API, AWS
region, insight, role, graph attempt, reported token usage, provider request ID and
the exact price version used. Prices are append-only; later changes do not reprice
old requests. Preview and model smoke checks have no persisted run ledger.

Generate offers a USD limit for the run. Compare and Configure tests offer a limit
**per candidate run**: two candidates with $5 each have a combined $10 limit. The UI
starts at $5. The API accepts `budget_usd` on batch and comparison submissions; an
omitted or null budget leaves the run unbounded for existing clients. CLI persisted
runs also accept `mel-worker run ... --budget-usd 5`.

Before each native request, a transaction locks that run's accounting row and
reserves the model's full documented input context bound, at the highest configured
input/cache rate, plus its enforced output-token limit. Reported usage settles the
reservation once, including cache reads/writes when separately priced. The output
count includes reasoning; it is not added again. SDK retries are disabled for agent
calls; Pydantic output retries are individually metered. The rough pre-run estimate
assumes 3,300 input and 200 output tokens per role per survey, before revisions.
It is separate from the conservative reservation used for admission.

A run stops when its next request cannot be covered. Completed checkpoints remain
available. In-flight requests may finish; cancellations, missing usage, unsupported
billing dimensions and overruns retain their exposure and block a budgeted run.
Resuming keeps the original limit, stop reason, spend and reservations. Nothing
expires automatically. Pending reservations from an interrupted process are retained.
These are configured-price estimates, not an AWS invoice or account-wide spending
cap; their accuracy depends on the stored rates and documented context bounds.

Migration `005_run_costs.sql` seeds verified Standard text rates for
`openai.gpt-6-luna` / Responses / `us-east-1` from the
[AWS Luna model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-luna.html),
including Mantle's regional premium, cached input and the long-context tier.
It also seeds `deepseek.v3.2` / Converse / `us-east-1` from
[AWS Bedrock pricing](https://aws.amazon.com/bedrock/pricing/), using the context bound
in its [model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-deepseek-deepseek-v3-2.html).
Both were checked on November 4, 2026. This is a dated snapshot, not automatic price
refresh. Other model/API/region combinations remain unpriced until verified; no
regional or model-name aliases are guessed. Budgeted launches require matching prices.

`GET /api/prices` lists current price versions. `POST /api/prices` appends a version
with `model_id`, `api`, `region`, and `pricing`: input/output USD per million tokens,
`max_input_tokens`, `source`, `checked_on`, optional cache rates and optional
long-context threshold/rates. Use provider documentation for the selected native
API, routing and region. Cache writes with ambiguous duration must remain unpriced.
The existing console sign-in also protects these routes.

`POST /api/jobs/batch/estimate` and `POST /api/experiments/estimate` calculate without
inference. Run pages and comparisons show spend, committed exposure and budget.
`GET /api/runs/{run_id}/costs?limit=100&offset=0` returns a page of requests with price
sources and full-run aggregates. Unknown totals stay unknown; known token and cost
subtotals remain visible. Recorded history is not retroactively priced.

Run `pytest` for offline checks. With a dedicated test PostgreSQL server, set
`MEL_TEST_DB_PORT` and run `pytest tests/test_costs_postgres.py` to verify settlement,
resumption, rate snapshots, request capture and concurrent connections. The concurrency
test creates and removes its own temporary schema on that test server.
