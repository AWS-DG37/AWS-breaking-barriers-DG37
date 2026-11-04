# Field Echo architecture proposal

Status: historical proposal for Team 37 review, 3 November 2026.
This records the design at that date, not the current feature inventory. Use
[README](../README.md) and [console usage](explorer.md) for current architecture,
deployment and setup. Since this proposal, configuration versions, dataset imports,
persistent jobs, Converse support, run costs and the hosted console have landed.

Original branch scope: Work stays on
`ben/evals-and-checks`; this does not authorize a merge into `main`.

## Problem

Field Echo turns FarmerChat conversation insights into short follow-up surveys
about advice already given. The participant brief says, “Never give new advice
and never ask for personal information” (`participant-pack/index.html:72`). It
also excludes real delivery and real farmers (`:77`). The rough team diagram is
the intended wider system; its boxes are not all implemented services.

After bringing upstream `9ad0578` into this branch, a configurable generation
worker already exists. It uses Pydantic AI/Graph, Bedrock Mantle Responses,
PostgreSQL checkpoints and a scheduler outbox, with Docker/Fargate packaging.
The architectural task is to connect trustworthy evaluation and additional input
context to that worker, rather than build another generation pipeline.

## Usage, from the caller's view

These commands exist today:

```bash
# One or a few structured insights, local artifacts, real Bedrock calls.
AWS_PROFILE=workshop MEL_AWS_REGION=us-east-1 \
  uv run --locked mel-worker preview --input path/to/insights.jsonl

# The same graph, with PostgreSQL checkpoints and a transactional handoff.
AWS_PROFILE=workshop MEL_AWS_REGION=us-east-1 MEL_DATABASE_SSLMODE=disable \
  uv run --locked mel-worker run --input path/to/insights.jsonl \
  --output outputs/local-results.jsonl
```

Both commands now select the first 10 nonblank records by default; `--full` is explicit.
See [local testing](LOCAL_TESTING.md) for setup and the verified Docker path.

The first evaluation slice is implemented on this branch:

```bash
# Offline policy comparison; no model calls or deployment.
uv run --locked mel-worker eval policy
```

Its single programmatic entry point is:

```python
from pathlib import Path
from mel.bundle import Bundle
from mel.config import Settings
from mel.evaluation import score_policy

report = score_policy(
    bundle=Bundle.load(Settings()),
    golden_path=Path("eval/golden_holdout.jsonl"),
    split_path=Path("eval/prompt_example_split.json"),
)
```

A later model/prompt comparison scores recorded run artifacts. It does not build
another inference loop. A later simulator consumes accepted handoffs and writes
simulated response records, without changing generation results or dispatching
real messages.

## Shape and ownership

```mermaid
flowchart LR
    Input[Structured insight JSONL] --> Select[Schema, eligibility and timing]
    Select -->|eligible| Generate[Configured generator]
    Generate --> Review[Schema and configured model review]
    Review -->|revise within bound| Generate
    Review -->|accepted| Handoff[Survey and deterministic handoff]
    Select -->|skip| Results[Run outcomes]
    Review -->|reject or fail| Results
    Handoff --> Results
    Handoff --> Outbox[PostgreSQL scheduler outbox]
    Select -.-> Eval[Offline policy evaluation]
    Results -.-> FutureEval[Later survey artifact evaluation]
    Holdout[Held-out expectations] -.-> Eval
```

`preview` keeps artifacts locally and never enqueues its illustrative handoffs.
The persisted worker writes results and accepted handoffs in one transaction.
The SQL describes the outbox as “not a delivery queue implementation”
(`migrations/001_initial.sql:26`). Scheduler enforcement, delivery, response
simulation and reporting remain separate workstreams.

| Knowledge or state | Owner | Boundary |
| --- | --- | --- |
| Resolved model/prompt/rule/schema snapshot | `src/mel/bundle.py`, `config.py` | One configuration per run; secrets excluded |
| Survey-or-not rules (golden R1–R8) and send windows | `src/mel/decision/` | Deterministic `DecisionEngine`; no model calls |
| Priority, channel order and scheduler handoff | `src/mel/handoff.py` | Deterministic backend policy |
| Question content and rationale | `agents.py`, `graph.py`, `models.py` | Models do not assign identity or delivery metadata |
| Official JSON contracts | `src/mel/contracts.py`, tracked pack schemas | Full validation after generation |
| Execution, checkpoints and accepted handoffs | `worker.py`, `store.py` | Existing preview/run paths, no third generation loop |
| Policy evaluation and expected labels | `src/mel/evaluation/` | Offline scoring; labels never reach agents |
| Legacy regression comparator | `dg_agent`, existing tests | Historical baseline, not another production policy owner |

The public evaluation interface hides case loading, split validation, label
normalization, metric denominators and diagnostics. It exposes the resolved
`Bundle`, dataset references and report, because those determine what was tested.
It does not expose OpenAI, HTTPX, boto3 or graph-node objects to callers.

The generator and reviewer have separate model IDs, reasoning settings and token
budgets in `config/agent.yaml`. Null IDs use `MEL_BEDROCK_MODEL_ID`. An explicit
model override can affect both roles; comparisons must record both resolved IDs
and keep the reviewer model/prompt fixed when claiming a generator-only change.
Unsupported model/settings combinations fail visibly; no silent model switch.

Generation currently writes directly in `conversation_language`. A separate
English draft, translation service and translation cache are not required by the
brief. If translation is added, IDs, option values and follow-up meanings stay
stable, and the final localized artifact passes the same acceptance boundary.

## How the reference eval work folds in

The branch's original models, rules checks and tests were compared with
`~/src/bb-digital-green`; the core files matched before the integration. The
retained evaluator now reads the tracked `docs/participant-pack`, matching the
worker's assets. A fresh-checkout test verifies that ignored `data/pack` files
are not required. The original standalone copy remains untouched.

Use the existing checks as the reference foundation, with one production owner:

1. Keep `mel.decision` (the `DecisionEngine`) authoritative for runtime decisions. Keep the old
   `dg_agent.decide()` as a named regression comparator until differences are
   understood; do not introduce a third decision engine.
2. Adapt useful `check_survey()` assertions into shared configured checks. Expected
   timing/eligibility comes from `mel.decision`, not copied calculations. Structural
   validation uses `Contracts`, not a second independently maintained registry.
3. Initially run these assertions offline. Making them new runtime blocking
   gates changes acceptance behavior and needs its own reviewed step.
4. Mark the English regex and language-tag checks as limited evidence. They do
   not establish Hindi quality, absence of new advice, or an approved MEL rubric.

The existing **145/150** result is an all-150 eligibility regression. It does not
measure generated questions. The current prompts already contain seven golden
examples; the split excludes those and five related cases, leaving **138
candidate held-out records**. That split is not a semantic paraphrase detector.
The new evaluation must keep `expected` labels out of generation and report the
actual dataset hash and denominator for every score.

Always include all selected cases in overall eligibility/confusion counts,
including known policy misses. A missing reason mapping or an unimplemented rule
must not improve accuracy by removing difficult cases. Reason agreement is a
separate dimension: the engine emits the golden set's own reason codes, so raw
expected/predicted codes are compared directly and an unknown golden code counts
as a disagreement. Golden `start/end` windows
also need deliberate adaptation to contract `not_before/not_after` fields.

Report schema/policy compliance, relevance/grounding, actual language quality,
follow-up behavior and reliability separately. A model review is an observed
heuristic. Its own prompt says, “human review is still needed”
(`prompts/review.txt:57`). Missing human/rubric evidence is `not_assessed`, never
a pass. Missing usage or role-specific prices means cost is unavailable.

## Evaluation contract

The implemented Pydantic report types live in `src/mel/evaluation/`. Existing
`Bundle` and worker `Outcome` remain in use; no replacement insight/survey class
hierarchy is required. `score_policy(*, bundle, golden_path, split_path)` returns
a `PolicyReport`:

- `Assessed` carries a numerator and a positive denominator; impossible counts
  are rejected. No evidence or a zero denominator produces `NotAssessed` with a
  reason, rather than a numeric score.
- `CaseComparison` preserves each golden ID, rule, raw expected/predicted
  decision, reason and window, with separate reason/window assessments.
- `PolicyReport` carries dataset/split/scorer/bundle hashes, confusion counts,
  per-rule eligibility, reason agreement and all cases.
- `LegacyReport` records the independent full-source denominator, reference
  implementation/rules hashes and disagreement IDs.

Evaluation has its own fingerprint and output artifacts. Scorer edits must not
invalidate generation resume. The generation implementation hash scans every
`src/mel/**/*.py` except the packages listed in `OUTSIDE_GENERATION`
(`src/mel/bundle.py`: `evaluation`, `experiments`, `explorer`); a test covers this
boundary. New runtime code is included by default.
Once a checker affects acceptance, its version belongs in generation identity.

Malformed-row identity and provenance need a later change: preserve source line
and input hash even when no valid insight ID exists. Dependency failures remain
`failed`, not business `skipped` decisions. Infrastructure resume keeps a run ID;
a new stochastic experiment gets a new one.

## Synthesis decision

Two independent candidates compared a per-record `SurveyService` with extending
the existing run/artifact architecture. Both preferred the latter. The cross-judge
selected candidate B, 26/30 versus 21/30, for explicit assessment states,
reason-vocabulary handling and an offline policy-first slice.

Use B's run-oriented base, with A's narrow public entry point, explicit metric
denominators and row-provenance idea. Keep the worker graph, Docker/Fargate and
PostgreSQL outbox. Defer additional execution commands, configurable row-limit
changes and runtime acceptance changes. The existing first-three/explicit-full
guard stays in place.

The synthesis corrects two review overstatements: binary eligibility agreement
needs no reason crosswalk, and choosing a config file does not by itself replace
both model IDs. Reason aliases matter for reason diagnostics; model overrides or
shared null-ID fallbacks can change both resolved roles.

## Tradeoffs and alternatives

- Accept file-based scorecards to preserve local, offline iteration without a new
  database schema or service.
- Accept the legacy comparator temporarily to preserve a visible baseline;
  migrate consumers before removing it.
- Reject a new `SurveyService` now: it mostly wraps `process_insight()` while
  giving another caller responsibility for execution and provenance.
- Reject expected labels as a graph node and a second evaluator-owned inference
  loop. Evaluation observes generation; it does not influence the candidate.
- Defer a REST API, scheduler, simulator, separate translation/cache, multi-insight
  grouping and broad arbitrary-document ingestion until their workstream needs
  and contracts are agreed.

## Open decisions and risks

- Who confirms critical-case handling, crop-calendar exclusions, channel
  precedence and no-survey reason codes with Digital Green? Keep the current
  behavior and disagreements explicit until then.
- What rubric and human review establish question relevance, follow-up meaning
  and Hindi quality? Current model-review scores are insufficient evidence.
- Which input formats are actually needed beyond the current structured JSONL?
  Farmer profiles are not currently loaded; adding them requires explicit field
  precedence and provenance, rather than silently overriding insight language.
- Confirm the team's authority for tracked participant-pack distribution before
  any new publication. This proposal adds no copies or participant records.

## Implemented first slice and next boundary

The offline policy scorer uses the documented holdout, existing production policy
and reference comparator. Tests cover provenance, denominators, unresolved reasons,
offline execution and parity with the actual worker selection boundary. The first
run reports 132/138 holdout eligibility agreement and the separate historical
145/150 baseline; see the [evaluation guide](../eval/README.md) for coverage and
the unresolved policy/timing differences. Generation acceptance is unchanged.

The next slice can score recorded survey artifacts with shared deterministic
checks and independent MEL/language evidence. It should not add another inference
loop, silently turn English regex checks into a Hindi-quality gate, or deploy AWS
resources as part of offline evaluation.
