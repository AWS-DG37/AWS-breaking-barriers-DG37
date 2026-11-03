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
