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

### Comparing prompt versions

Copy any of `generator_prompt.md`, `verifier_prompt.md` or `translator_prompt.md` into a folder, edit
it, and pass the folder. Files the folder lacks use the defaults in `skills/mel-survey-writer/references/`.

```bash
python sweep.py --golden ../eval/golden_dev.jsonl --models global.anthropic.claude-haiku-5-5 \
  --limit 75 --out runs/dev --prompt-dir prompts/shorter-labels --tag shorter-labels
```

Each run file records `tag`, `prompt_dir`, `prompt_sha` (hash of the three prompt files) and `golden`,
so the console's Agents tab can tell prompt versions apart. Only compare rows with the same golden file.

## Deployed on Bedrock AgentCore Runtime

`main.py` wraps `service.generate_survey()` (the same `sweep.run_pipeline` code the sweep scores) as an
AgentCore HTTP agent. `service.py` holds the model allowlist; the runtime's IAM role allows only those
models. The deployed generator runs without the AWS MCP documentation tools, like the sweep.

Main pushes automatically deploy the exact tested CodeZip through
[Field Echo delivery](../docs/fieldecho-deployment.md#agentcore-code-releases).
The existing runtime ARN, execution role and package bucket are recorded in
[`deploy/agentcore-target.json`](../deploy/agentcore-target.json). Runtime provisioning
and IAM changes remain operator-owned; ordinary updates go through the main workflow.

```bash
# local copy of the same entry point, from agents/
PORT=8081 .venv/bin/python main.py
```

Payload: `{"insight": {...}, "generator": "...", "verifier": "...", "pipeline": "direct"|"translate",
"max_retries": 0-2}`. Reply: `{"ok": true, "result": {...one sweep result...}}` or `{"ok": false, "error": ...}`.

If you add a model to `service.GENERATORS`, also add it to the `BedrockModelInvocation` statement of the
execution role recorded in `deploy/agentcore-target.json`, then release through main.

### MEL console

The console's **Agents** tab calls the deployed agent. Start it with:

```bash
# From the repository root; read the managed nonsecret target.
MEL_AGENT_RUNTIME_ARN="$(python3 -c 'import json; print(json.load(open("deploy/agentcore-target.json"))["runtime_arn"])')" \
  uv run mel-explorer --port 8765
```

Open `http://127.0.0.1:8765/#agents`, pick a golden insight and one or more generator models, and
the surveys come back side by side in the phone preview. The tab also lists every saved sweep in
`agents/runs/`. A call takes 15-60 seconds, so it works from a locally run console; the hosted console's
API Gateway and Lambda stop at 29 seconds.
