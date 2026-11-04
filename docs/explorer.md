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

Open <http://127.0.0.1:8765> and select **New live experiment**. Supply normalized
insight JSONL with no golden expectations. Choose candidate models and native APIs,
a shared reviewer, and a configured ruleset. Model discovery does not prove access
or structured-output compatibility. Unsupported calls remain failures; there is no
automatic provider or API substitution.

Only **Run live experiment** invokes models and incurs AWS usage. A request allows
at most six candidates and fifty insights, defaults to three insight rows, and uses
the existing revision and timeout limits. At most two experiments run concurrently
in one API process. Comparison results never enter the scheduler handoff queue.

The API defaults to loopback and has no authentication. Keep it local. The existing
Fargate stack runs the batch worker; it does not deploy this web API. Its IAM policy
currently authorizes Mantle inference only. Configure appropriate native Converse
permissions before using Converse candidates in a deployed worker.

## Change a team-owned component

| Concern | Owner |
| --- | --- |
| Registered rulesets | `config/rulesets.yaml`, with paths relative to that file |
| Rule semantics and windows | `src/mel/decision/` |
| Flow and emitted stage evidence | `src/mel/graph.py` |
| Models, budgets, and revision settings | `config/agent.yaml` |
| Native SDK construction and catalog | `src/mel/agents.py`, `src/mel/bedrock_models/` |
| Grouped experiments and comparison analysis | `src/mel/experiments/` |
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
