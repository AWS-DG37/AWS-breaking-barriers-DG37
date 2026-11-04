# MEL survey agent starter

1. pip install -r requirements.txt
2. Copy insights.jsonl from the participant pack into data/. Keep golden_set.jsonl OUT of this folder.
3. Test the plumbing with no AWS: python run_one.py data/insights.jsonl --index 2 --dry-run
4. Set up AWS credentials (see the chat guide), then: python run_one.py data/insights.jsonl --index 2
5. Change the model with MODEL_ID, for example global.anthropic.claude-sonnet-5-5.

Dry-run uses a bundled example survey as fake model output, so it proves the gate, prompt building and code checks work. It does not test the model. The Bedrock call itself has not been tested by me, since I have no AWS account here.
