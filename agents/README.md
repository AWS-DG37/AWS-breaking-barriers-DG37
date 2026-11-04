# MEL survey agents

1. pip install -r requirements.txt
2. Copy insights.jsonl from the participant pack into data/. Keep golden_set.jsonl OUT of this folder.
3. Test the plumbing with no AWS: python run_one.py data/insights.jsonl --index 2 --dry-run
4. Set up AWS credentials (see the chat guide), then: python run_one.py data/insights.jsonl --index 2
5. Change the model with MODEL_ID, for example global.anthropic.claude-sonnet-5-5.

Dry-run uses a bundled example survey as fake model output, so it proves the gate, prompt building and code checks work. It does not test the model.

## Pipeline

gate (`decide.py`, code only) -> survey cache (`cache.py`) -> generator -> code checks (`check_survey.py`) -> verifier.

- The agents decide whether an insight gets a survey and write it. They do not decide when it is sent: no send window, and no rules that need one (R6 season end, R7 crop harvested). A separate timing script owns that and adds the send window. Questions and answers may still use timing words such as "on time" or "in the last few days"; exact dates are not allowed.
- Every farmer sees every question. There is no skip logic, so the verifier does not ask for it.
- Surveys have 1 to 3 questions and at most 4 answers per question. Answer labels are natural short phrases, up to 20 characters (WhatsApp limit).
- The verifier tags each issue `blocking` or `minor`. A survey passes when nothing is blocking (`effective_verdict` in `run_one.py`), and only blocking issues go back to the generator.
- Rule R2 (critical severity goes to a human call) is removed; critical insights follow the normal rules.

## Comparing models: sweep.py

Runs the first N golden-set insights through the pipeline for each generator model and scores them. Results go to `runs/` (git-ignored) and a comparison table is printed.

```
python sweep.py --golden ../docs/participant-pack/data/golden_set.jsonl \
    --models global.anthropic.claude-haiku-5-5 us.openai.gpt-5.5 \
    --limit 10 --max-retries 2
```

Options: `--verifier-model` (default Sonnet 5.5; keep it fixed and different from the generators), `--pipeline direct|translate`, `--translator` (default `google.gemma-3-27b-it`), `--no-translation-cache`.

- `direct`: the generator writes the survey in the farmer's language.
- `translate`: the generator writes English, then a translator model writes the farmer's language (`translate.py`, `translator_prompt.md`). Code keeps ids, answer values and follow_up codes from the English survey and takes only the text from the translator. Verified translations are cached in `cache/translations/`.

Each run file records per insight: decision, verdict, relevance, attempts, tokens and seconds per stage, every verifier review, and the final survey.
