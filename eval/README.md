# Golden prompt examples and held-out cases

The v3 generator and reviewer prompts embed seven compact examples from Digital
Green's supplied golden set. Five positive examples cover direct action (potato),
monitoring (lentil), a plan (gram), holding off (cabbage), and expert escalation
(buffalo). Two negative examples cover general information (onion) and an
unfinished conversation (lentil). No mustard example is included in either prompt.

These are static examples embedded in both prompt files. The agents are instructed
to select the closest measurement pattern by resolution type before crop. There
is no new retrieval service or extra model call. English reference questions and
answer meanings are copied from the golden source; project-authored explanations
describe what to transfer. References are not complete production survey objects:
the target language, schema, applicability choices, and follow-up semantics still
come from the current request and contract.

`prompt_example_split.json` records source IDs, the original file's SHA-256,
excluded cases, and candidate evaluation IDs. The original 150-row source is
untouched. Seven prompt examples and five additional related cases are excluded
from scoring, leaving 138 candidate held-out cases, including 14 mustard cases.

Exclusions cover the example records, their farmers/conversations, and exact
advice duplicates after normalizing whitespace and replacing known crop/animal
names. This is a conservative starting split, not a semantic paraphrase detector.
Remove further cases from scored evaluation whenever they are used for tuning.
Repeated reference question templates do not by themselves constitute unseen-task
evidence; inspect performance across advice types and crops separately.

`golden_holdout.jsonl` retains the original golden row format with nested `insight`
and `expected`. It is evaluation data, not worker input: extract `insight` records
when constructing a generation input, and keep `expected` away from the agents.
Do not use exact question-string matching as the quality metric. Assess advice
grounding, correct survey decisions, outcome/adoption measurements, follow-up
labels, and agreement with human reviewers.

Do not score the prompt examples as independent evaluation cases. The golden
questions are reference evidence rather than an infallible rubric, and these
English examples do not replace Hindi-speaking review.

## Run the offline policy comparison

From the repository root, after `uv sync --locked`:

```bash
uv run --locked mel-worker eval policy
# Or select an explicit config and a new output file:
uv run --locked mel-worker eval policy --config config/agent.yaml \
  --output outputs/eval/policy-comparison.json
```

This command makes no Bedrock or database calls. It saves a JSON scorecard under
`outputs/eval/policy-<uuid>.json` by default and prints its path with the two
eligibility counts. An explicit output path must be new; existing files are never
overwritten. Evaluation is descriptive: a nonzero process exit indicates invalid
inputs or another execution failure, rather than a minimum score threshold.

`--golden` defaults to `eval/golden_holdout.jsonl`; `--split` defaults to
`eval/prompt_example_split.json`. The split's `source` path is resolved relative
to the project directory containing `eval/`. The scorer verifies the source
SHA-256, the complete holdout/exclusion partition, prompt exclusions, and exact
agreement between held-out records and their source records before scoring.
Use this with local project files; it does not download datasets or config.

The report records source, holdout, split, scorer and resolved bundle hashes;
case-level expected/predicted decisions, reasons and windows; a confusion matrix;
and per-rule eligibility counts. The bundle fingerprint uses `offline-policy` as
its evaluation context, rather than an AWS region. Scorer-only edits do not
change generation resume identity.

The primary score uses the worker's `DecisionEngine` (`src/mel/decision/`) on all
declared held-out cases. The separate `dg_agent_all_source` comparator reuses the
original `decide()` on the complete source, including prompt examples, and records
its implementation and actual reference-rules hashes. Its denominator is not
interchangeable with the primary score. The reference package is installed with
the worker; its pack assets still come from the repository. If those assets are
unavailable in another installation, the comparator is explicitly `not_assessed`.

## Policy evidence after the decision engine, 4 November 2026

With the checked-in rules, split and `decision.window_policy: golden_v1`:

| Dimension | Result | Interpretation |
| --- | --- | --- |
| Current worker eligibility | 138/138 | 88 true positives, 50 true negatives; every rule R1–R8 at 100% |
| Legacy eligibility | 145/150 (96.7%) | Reproduces the historical full-source regression |
| Reason agreement | 50/50 | The engine emits golden reason codes directly; no equivalence table |
| Exact send-window agreement | 88/88 | `golden_v1` windows; `pack` windows open at 07:00 and mostly differ |

These rules and the `golden_v1` window settings were written while reading all
150 golden cases, holdout included. The holdout therefore does not independently
measure the decision engine; treat 138/138 as a regression baseline, not a
generalization estimate. `mel-worker eval-rules` scores the same engine on all 150.

Before the engine (3 November 2026, `mel.policy`), the worker scored 132/138
eligibility, 0/88 exact windows, and could not express the critical-case and
harvested-crop reasons.

This slice assesses policy before inference. Relevance/grounding, actual language
quality, follow-up semantics, channel precedence and generation reliability are
separate `not_assessed` dimensions. It does not score generated questions or add
new runtime acceptance gates. The next evaluation slice can consume recorded
worker artifacts, reuse suitable deterministic survey checks, and add independent
MEL and Hindi review without introducing another inference loop.
