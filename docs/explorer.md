# Run the pipeline explorer locally

The explorer compares saved survey runs and explicitly starts bounded Bedrock
experiments. It shares the worker's graph, prompts, schemas, and PostgreSQL store.
The [architecture walkthrough](architecture-walkthrough.html) explains the flow.
Use the [agent-agnostic mac-mini handoff](handoffs/mac-mini-setup.md) to clone,
start, and verify the full app on mac-mini, with the agreed visual direction.

## Start the recorded demo with Docker

On **macdaddy-laptop**, from the repository root:

```bash
MEL_LOCAL_DATABASE_PORT=55437 MEL_EXPLORER_PORT=8877 \
  docker compose -p dg37-explorer up --build -d
```

Open <http://127.0.0.1:8877> and select **Import recorded run**. This imports an
actual Bedrock comparison on one synthetic mustard insight. OpenAI and Anthropic
produced accepted surveys. Amazon Nova's original execution failure remains visible.
This example demonstrates the inspection workflow; one scenario cannot establish
which model performs best overall.

Select a candidate, insight, pipeline stage, and question. Inspect the draft,
review findings, revision, and final ruling. The comparison includes failures and
missing cases. Unknown token usage remains unknown. Question explanations are
model-supplied diagnostics. Relevance is a survey-wide model judgment.

Choose **Saved execution rules** or **Current golden reference** to reanalyze the
saved outputs. This makes no model calls. Golden matching requires an identical
source insight, and the response identifies the current reference file's SHA-256.
Legacy participant-pack checks are diagnostic evidence with their own fixed rules;
their language check compares metadata and their PII/advice patterns cover English.
Independent MEL and Hindi review remain necessary for a quality claim.

The Docker explorer disables live execution by default and needs no AWS credentials.
Stop this instance while retaining the database volume:

```bash
docker compose -p dg37-explorer down
```

## Start a bounded live comparison

Start PostgreSQL with the same compose project, then run the API in the host's
authenticated AWS environment. Temporary workshop credentials must still be valid.

```bash
MEL_LOCAL_DATABASE_PORT=55437 docker compose -p dg37-explorer up -d postgres
uv sync --locked
AWS_PROFILE=digital-green AWS_EC2_METADATA_DISABLED=true \
  MEL_DATABASE_PORT=55437 MEL_DATABASE_SSLMODE=disable \
  uv run --locked mel-explorer --port 8765
```

Start a job runner in a second terminal, with the same environment. The API only
queues experiments; the runner executes them:

```bash
AWS_PROFILE=digital-green AWS_EC2_METADATA_DISABLED=true \
  MEL_DATABASE_PORT=55437 MEL_DATABASE_SSLMODE=disable \
  uv run --locked mel-worker jobs
```

Open <http://127.0.0.1:8765>, open **Compare** and select **New comparison**. Pick one
to six saved configuration versions and a dataset selection (at most fifty insights);
every version runs the same insights. Model discovery does not prove access or
structured-output compatibility. Unsupported calls remain failures; there is no
automatic provider or API substitution.

Only **Run comparison** invokes models and incurs AWS usage. Each runner executes one
job at a time; start more runners for more concurrency.

## Edit, test and save a configuration

**Configure** lists the saved versions (the active one first), test drafts, and
retired versions. A version's page shows what it changed from its parent, its rules
scored against the golden holdout (instant, no model calls), each pipeline step's
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

A definition must validate before it is saved: prompts cannot be empty, and hosted
versions may only use the models the worker may call (`MEL_ALLOWED_MODELS`). The stack
allows GPT-6 Luna through Mantle (`BedrockAllowedModelIds`) and the Converse models in
`ConverseModelIds`: Claude Haiku, Sonnet and Opus 5.5, Nova Pro, Nova 2 Lite, Mistral
Large 3, DeepSeek V3.2 and GPT 5.5. Each passed a tool-call check, which structured
survey output needs; Llama 4 does not support tools. Converse agents never force the
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
every result, and selecting one shows the agent's steps and a survey preview.
**Compare** is the model comparison described above. Configure, Schedule and Feedback
are shown greyed out until they exist. Hosted, `deploy.sh` copies the pack to the data
bucket, from which the API imports it (`MEL_PACK_URI`).

The worker's IAM policy authorizes Mantle inference for `BedrockAllowedModelIds` and
Converse inference for Bedrock models and inference profiles; the console limits which. 
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
saved ratings and does not collect them. Future dataset ingestion and scheduler
integration can use these existing boundaries.

## Verify a change

```bash
uv run --locked pytest -q
node --test src/mel/explorer/static/domain.test.mjs
MEL_TEST_DB_PORT=55437 uv run --locked pytest tests/test_postgres_integration.py -q
```

The PostgreSQL tests use rollback-only transactions. They check experiment/outbox
isolation, atomic result/handoff writes, and migration history. Live inference and
browser checks are separate from the offline suite.

## Build the standalone architecture viewer

On **macdaddy-laptop**, from the repository root:

```bash
python scripts/build_architecture_viewer.py /tmp/dg37-architecture
python -m http.server 8766 --directory /tmp/dg37-architecture
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
python scripts/verify_architecture_viewer.py
python scripts/verify_architecture_viewer.py --url http://127.0.0.1:8766
```
