# Golden dev/test split, prompt examples and judges

The current baseline (`luna-golden-patterns-v9`) retains seven compact examples
from Digital Green’s supplied golden set, introduced in the v3 generator and
reviewer prompts. Five positive examples cover direct action (potato),
monitoring (lentil), a plan (gram), holding off (cabbage), and expert escalation
(goat). Two negative examples cover general information (onion) and an
unfinished conversation (maize). No mustard example is included in either prompt.
All seven come from the dev half.

These are static examples embedded in both prompt files. The agents are instructed
to select the closest measurement pattern by resolution type before crop. There
is no new retrieval service or extra model call. English reference questions and
answer meanings are copied from the golden source; project-authored explanations
describe what to transfer. References are not complete production survey objects:
the target language, schema, applicability choices, and follow-up semantics still
come from the current request and contract.

## The split (`golden-dev-test-v2`)

The 150 golden rows hold only 86 distinct advice texts, and only 95 rows expect a
survey. A random row split therefore puts copies of the same advice in both halves.
The split is grouped and stratified instead:

- Rows that share a normalized advice text, a farmer or a conversation form one
  group. Normalization lowercases the text, collapses whitespace and replaces the
  source's crop and animal names. A whole group goes to one half.
- Groups are assigned to balance expected rule (R1 to R8), `resolution_type`,
  `conversation_language`, the survey decision and their joint stratum.
- The build is deterministic: seed 37, 2,000 trials, best balance wins. Rebuild
  with `uv run --locked python -m mel.evaluation.split`.

The grouping yields 47 groups. One group holds 70 rows, because farmers link
many advice texts. That group decides much of the balance, so some strata are
uneven. Read every rate with its count.

| Count | Dev | Test |
| --- | --- | --- |
| Rows | 75 | 75 |
| Expect a survey | 48 | 47 |
| R1 / R2 / R3 / R4 | 4 / 4 / 9 / 6 | 10 / 2 / 3 / 4 |
| R5 / R6 / R7 / R8 | 2 / 1 / 1 / 48 | 8 / 1 / 0 / 47 |
| direct_action / plan_created | 27 / 9 | 27 / 15 |
| do_not_act / wait_and_monitor | 6 / 10 | 6 / 2 |
| escalate_to_expert / needs_more_context | 3 / 8 | 8 / 5 |
| generic_information | 12 | 12 |
| hi-IN / en-IN | 68 / 7 | 73 / 2 |

The only R7 row is in dev, so the test half cannot score R7.

Files:

- `prompt_example_split.json` records the source SHA-256, the method and seed,
  `dev_ids`, `holdout_ids` (the test half), the counts above, the prompt examples,
  the prompt example swaps and the tuning exposures.
- `golden_dev.jsonl` and `golden_holdout.jsonl` hold the dev and test rows in the
  original golden format, with nested `insight` and `expected`. They are
  evaluation data, not worker input. Extract `insight` records with
  `mel-worker golden-insights`, and keep `expected` away from the agents.

Two of the original v3 prompt examples fell in the test half. The builder swapped
each for the first dev row with the same resolution type, survey decision and rule:

| Replaced (now test) | Replacement (dev) | Resolution type |
| --- | --- | --- |
| G-0045 (buffalo) | G-0113 (goat) | escalate_to_expert |
| G-0018 (lentil) | G-0092 (maize) | needs_more_context |

The replacements have the same reference questions, so only the advice context
changed in both prompt files. The config version moved to `luna-golden-patterns-v9`.

Use dev, and only dev, to tune prompts and calibrate judges. Score test, and do not
tune on it. If a test row is used for tuning, add it to `TUNING_EXPOSURES` in
`src/mel/evaluation/split.py` with the reason and rebuild. The builder moves that
row's whole group to dev, records it under `tuning_exposures`, and the verifier
refuses a manifest with an exposed row in test.

Version history: `golden-dev-test-v1` had 74 dev and 76 test rows. In v2, test row
G-0073 moved to dev, because its test judgment prompted relevance rubric v4. It is
a group of one, so nothing else moved.

Grouping catches exact normalized advice duplicates, not paraphrases.
Do not use exact question-string matching as the quality metric. Assess advice
grounding, correct survey decisions, outcome/adoption measurements, follow-up
labels, and agreement with human reviewers.

Do not score the prompt examples as independent evaluation cases. The golden
questions are reference evidence rather than an infallible rubric, and these
English examples do not replace Hindi-speaking review.

## Timing sources and console checks

The golden examples and participant pack’s explicit `rules.json` disagree on
send-window timing. Configure labels these choices **Timing inferred from golden
examples** (`golden_v1`) and **Timing from participant pack’s written rules**
(`pack`). The former is our inferred policy, not an additional supplied requirement.
Source precedence remains unconfirmed by Digital Green. Changing this setting
changes runtime windows; it does not select the input dataset or scoring reference.

The console's **Agreement with golden examples** check runs this configuration's
deterministic decision engine on the 75 test-half cases, without model calls. It
uses `score_decisions`, rather than the CLI's provenance-rich `score_policy` report.
A lower exact-window score under `pack` can reflect compliance with the written
rules. Neither score is an independent generalization estimate: the rules were
developed using all 150 source cases, as described below.

## Run the rules regression baseline

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
to the project directory containing `eval/`. Before scoring, the verifier checks:

- the source SHA-256;
- that dev and test partition the source, with the prompt examples in dev;
- that no normalized advice, farmer or conversation is in both halves;
- that the recorded seed and counts match the rows;
- that each held-out record equals its source record.

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

## Rules regression baseline, 4 November 2026

The rules and the `golden_v1` window settings were written while reading all 150
golden rows, test half included. The scores below are a regression baseline, not a
generalization estimate. Every report carries a `label` field that says so.

With the checked-in rules, split and `decision.window_policy: golden_v1`:

| Dimension | Test half | Dev half (`eval-rules --golden eval/golden_dev.jsonl`) |
| --- | --- | --- |
| Eligibility | 75/75 (47 true positives, 28 true negatives) | 75/75 |
| Deciding rule | every test rule R1–R6, R8 correct (R7: 0 rows) | 75/75 |
| Reason agreement | 28/28 | not reported by `eval-rules` |
| Exact send-window agreement | 47/47 | 48/48 |
| Legacy eligibility, all 150 rows | 145/150 | (same source) |

Before the grouped split, the same engine scored 138/138 on the earlier 138-row
holdout. Before the engine (3 November 2026, `mel.policy`), the worker scored
132/138 eligibility and 0/88 exact windows on that holdout.

The policy score assesses decisions before inference. It does not score generated
questions. Language quality, channel precedence and generation reliability stay
`not_assessed`. The relevance judge below scores generated questions.

## Privacy calibration (calls Bedrock)

```bash
uv run --locked mel-worker eval calibrate-privacy --max-cases 8
```

Omit `--max-cases` for all 47 dev survey rows (188 reviewer calls before retries).
Each row has clean and personal-question controls in English and Hindi; clean
Hindi controls rotate three benign keyword traps. The judge is the production
reviewer, using the config's review prompt and model. `--split`, `--config`,
`--model-id`, `--api`, and `--output` override their defaults.

Reports default to `outputs/eval/privacy-calibration-<uuid>.json` and never
overwrite an existing file. They include prompt, split and scorer hashes, token
usage including failed calls, FP/FN numerator/denominator counts by language,
false positives by Hindi trap, quotes for disagreements, and failed cases.
Failed calls are excluded from FP/FN denominators and independently fail the gate.
`gate.build_narrow_check` is true if any false negative occurs, false positives
exceed the fixed limit of 2, or any reviewer call fails; `passed` is its inverse.
The gate recommends the separate narrow check; calibration does not build it.

### Result (2026-11-04)

`privacy_calibration_dev.json`: review model `openai.gpt-6-luna` in `us-east-1`, all 47
dev survey rows, 188 controls, 0 failed calls, 189 requests, 506,100 input and 46,405
output tokens.

| | English | Hindi | All |
|---|---|---|---|
| Missed personal questions (false negatives) | 0/47 | 0/47 | 0/94 |
| Flagged clean surveys (false positives) | 0/47 | 0/47 | 0/94 |

Hindi traps: पता नहीं 0/16, आधारित 0/16, फ़ोन किया 0/15 false positives. The gate does
not call for the narrow check. The controls are synthetic and the eight personal
questions are explicit. A perfect score shows that the reviewer applies the definition
to clear cases. It does not show how it handles subtle or indirect wording, and it does
not replace Hindi-speaking MEL review. A test fails if the review prompt, split or
scorer changes without a new report.

## Relevance judge (calls Bedrock)

The judge reads saved worker results. It does not run the worker and adds no
inference loop to it. These two commands and privacy calibration call a model.
The judge model is the config's review model unless you set `--model-id` and `--api`.

The rubric is `eval/relevance_rubric.txt`. For each generated survey, the judge
marks six criteria as met, not met or not applicable:

| Criterion | Met when |
| --- | --- |
| `advice_grounding` | every question is about this advice, crop or animal and problem |
| `measures_adoption` | a question asks whether the farmer followed the advice |
| `measures_outcome` | a question asks what happened since |
| `follow_up_routing` | follow-up codes sit only on answers that show the named problem |
| `no_new_advice` | no question or option gives new advice |
| `no_personal_info` | no question asks for identifying information |

A survey passes (`core_pass`) when the first, second, fifth and sixth criteria are
met. The golden reference questions go to the judge as English evidence of what a
good survey measures. The judge does not match strings, and the survey can be in
Hindi. The judge message leaves out farmer, conversation, location and insight IDs.

### 1. Calibrate on dev

```bash
uv run --locked mel-worker eval calibrate-relevance --max-cases 10
```

Calibration builds known-answer controls from dev rows only:

- the row's own reference questions (grounded, measures adoption, no new advice,
  no personal information, and measures outcome when the reference asks
  Better/Same/Worse, whether the plan or the expert's advice worked, or "Have you
  seen ...");
- a reference from an unrelated dev row (not grounded);
- a generic app question (not grounded, no adoption, no outcome);
- the own reference plus an injected new-advice question, or plus an injected
  personal-information question;
- the own reference plus the Hindi "Do you need more help?" question (grounded, no
  new advice);
- `crop_or_animal_damaged` on "Worse" and `could_not_reach_expert` on "Could not
  reach" (correct routing, as the generator prompt requires);
  `advice_did_not_work` on "Worse", or any code on "Better" or "All steps" (wrong
  routing).

The report gives agreement as counts per criterion and lists each disagreement
with the judge's rationale. Calibration passes when every criterion reaches 90%
agreement and no call fails. If it fails, edit the rubric and calibrate again on
dev. Omit `--max-cases` to use all 48 dev survey rows (up to eleven controls each).

### Calibration result and improvement iteration, 4 November 2026

Judge `openai.gpt-6-luna` (the config's review model) in `us-east-1`, dev half only.
Each cell is agreement with the known-answer controls.

Columns up to v3 + scales used split v1 (47 dev survey rows); the v4 column uses
split v2 (48 dev survey rows, G-0073 included).

| Criterion | v1, 10 rows | v2, 10 rows | v2, 47 rows | v3, 47 rows | v3 + scales, 47 rows | v4, 48 rows |
| --- | --- | --- | --- | --- | --- | --- |
| advice_grounding | 30/30 | 30/30 | 141/141 | 141/141 | 141/141 | 192/192 |
| measures_adoption | **18/20** | 20/20 | 94/94 | 94/94 | 94/94 | 96/96 |
| measures_outcome | 20/20 | 20/20 | 94/94 | 94/94 | 94/94 | 96/96 |
| follow_up_routing | 18/18 | 17/18 | **87/90** | 90/90 | 142/142 | 141/142 |
| no_new_advice | 20/20 | 20/20 | 94/94 | 94/94 | 94/94 | 144/144 |
| no_personal_info | 20/20 | 20/20 | 94/94 | 94/94 | 94/94 | 96/96 |
| Calls (failed) | 68 (0) | 68 (0) | 325 (0) | 325 (0) | 377 (0) | 430 (0) |

- v1 to v2: the judge said that "Have you seen new drying patches? Yes / No / Did
  not check" does not show the farmer checked (G-0013, G-0015). The rubric now says
  that a separate "Did not check" answer makes the question measure adoption.
- v2 to v3: on three rows the judge answered `not_applicable` although one survey
  option carried a correct code (G-0021, G-0042, G-0123). The rubric now limits
  `not_applicable` to surveys with no `follow_up` key and tells the judge to ignore
  the reference questions for this criterion.
- v3 + scales: scale questions route through `follow_up_at_or_below`, so the judge
  now sees each scale as numbered options, and the steps at or below the threshold
  carry its `follow_up_reason`. Two new controls turn each Better/Same/Worse
  question into a 1-5 scale: routed at 1-2 (correct) and at 1-5 (wrong). The rubric
  did not change.
- v3 + scales to v4: on the first test-half score, the judge called the Hindi "Do you
  need more help?" question generic (G-0073), although the golden wait-and-monitor
  references ask it. Because a test row prompted the change, G-0073 moved to dev
  (split v2), and the fix was reproduced and checked on dev surveys (see below). The
  grounding criterion now says that this question counts, and a new control appends
  it in Hindi. The one v4 miss is debatable: on G-0114 the judge said that step 2 of a
  1-5 scale is not clearly worse.

The controls are synthetic. Perfect agreement shows that the judge applies the
rubric to clear cases, not that it agrees with human MEL reviewers on real surveys.

### Real dev surveys

The synthetic controls are English references, so they missed a judgment that only
real Hindi surveys showed. Real generated surveys for the dev rows (same config,
same model) give a second dev check:

| Dev surveys | v3 rubric, split v1 (47) | v4 rubric, split v1 (47) | v4 rubric, split v2 (48) |
| --- | --- | --- | --- |
| Core pass | 45/47 | 47/47 | 47/48 |
| advice_grounding | 45/47 | 47/47 | 48/48 |
| measures_adoption | 47/47 | 47/47 | 47/48 |

In v3, 12 dev surveys asked "क्या आपको और मदद चाहिए?" and the judge marked 2 of
them not grounded (G-0071, G-0124). In v4 all of them are grounded. The split v2
miss (G-0066) is judge variance; see below.

### Test-half result

Config `luna-golden-patterns-v9`. The worker's decisions match the golden set on
every test row: 47 surveys (38 accepted, 9 rejected by the reviewer) and 28 skips
on the 75 split-v2 test rows.

| Test surveys | v3 + scales, split v1 (48) | v4, split v1 (48) | **v4, split v2 (47)** |
| --- | --- | --- | --- |
| Status | held-out, first score | post-inspection | **held-out** |
| Core pass | 47/48 | 48/48 | **46/47** |
| advice_grounding | 47/48 | 48/48 | 47/47 |
| measures_adoption | 48/48 | 48/48 | 46/47 |
| measures_outcome | 48/48 | 48/48 | 47/47 |
| follow_up_routing | 42/42 | 42/42 | 41/41 |
| no_new_advice | 48/48 | 48/48 | 47/47 |
| no_personal_info | 48/48 | 48/48 | 47/47 |

The held-out number is the last column. The v4 split-v1 score included G-0073, the
row that prompted v4, so it is a post-inspection result only.

**Judge variance.** The judge does not return the same verdict on every run. G-0056
(test) and G-0066 (dev) passed adoption in earlier runs and failed it in the split-v2
runs, with the same survey and rubric. Both advise two actions and the survey asks
about the main one, as the generator prompt requires. Expect about one flip per 47
surveys between runs. These cases were not used for tuning. A dev-side fix is to
run the judge several times per survey and take the majority verdict.

The judge also passes all 9 drafts that the reviewer rejected, so the two models
apply different bars. Human MEL review of those 9 and of a sample of the rest is
the next check (issue #9).

`relevance_calibration_dev.json` is the v4 report. It records the rubric,
split and judge code hashes. A test fails if any of them changes without a new
calibration, and `--part test` refuses a stale report.

### 2. Score saved results

```bash
uv run --locked mel-worker golden-insights --golden eval/golden_holdout.jsonl \
  --output outputs/eval/test-insights.jsonl
# Generate surveys from those insights (preview, run or the console), then:
uv run --locked mel-worker eval relevance --part test \
  --results outputs/local/<run-id>/results.jsonl \
  --calibration eval/relevance_calibration_dev.json
```

`--part test` refuses to call the model unless the calibration report passed and
matches the same rubric hash, judge model and split hash. `--part dev` runs
without a calibration, to support rubric iteration. Results must all come from the
named half.

The report gives each rate as a numerator and denominator. It also counts results,
judged surveys, failed calls, rows where the golden set expects a survey but the
worker made none, and the reverse. Only rows where both have a survey are judged.
Each report records the split, rubric, results hashes and token usage. Reports
are never overwritten.

An LLM judge does not replace Hindi-speaking MEL review.
