"""Verify the actual signed Responses wire format with HTTPX's local mock transport."""

import json
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import httpx
from botocore.credentials import Credentials

from mel.agents import build_agents
from mel.bedrock import MantleSigV4Auth, create_mantle_client
from mel.bundle import Bundle
from mel.config import Settings
from mel.graph import State, check_usage
from mel.policy import IST


class BedrockTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        session = Mock()
        session.get_credentials.return_value = Credentials(
            "TESTACCESS", "test-secret", "test-token"
        )
        return session

    async def test_agents_use_model_name_and_signed_responses_on_default_project(self):
        await self.run_signed_agents(usage_reported=True)

    async def test_missing_provider_usage_is_marked_incomplete(self):
        await self.run_signed_agents(usage_reported=False)

    async def run_signed_agents(self, usage_reported):
        requests = []
        draft = {
            "items": [
                {
                    "item_id": "q1",
                    "type": "survey-confirm",
                    "prompt": "Did you spray?",
                    "options": [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}],
                }
            ],
            "rationale": "Follow up on spraying advice.",
        }

        def handle(request):
            requests.append(request)
            payload = json.loads(request.content)
            output = (
                draft
                if len(requests) == 1
                else {
                    "relevance_score": 0.9,
                    "language_correct": True,
                    "gives_new_advice": False,
                    "asks_personal_info": False,
                    "issues": [],
                }
            )
            return httpx.Response(
                200,
                json={
                    "id": f"resp_{len(requests)}",
                    "object": "response",
                    "created_at": 1,
                    "model": payload["model"],
                    "status": "completed",
                    "output": [
                        {
                            "id": "fc_1",
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": payload["tools"][0]["name"],
                            "arguments": json.dumps(output),
                            "status": "completed",
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
                    if usage_reported
                    else None,
                },
            )

        with patch("mel.bedrock.boto3.Session", return_value=self.session()):
            async with create_mantle_client(
                "us-east-1", transport=httpx.MockTransport(handle)
            ) as client:
                bundle = Bundle.load(Settings())
                with patch("mel.agents.create_mantle_client", return_value=client):
                    generator, reviewer = build_agents(
                        bundle.config, "us-east-1", bundle.snapshot["prompts"]
                    )
                generated = await generator.run("Synthetic test context")
                self.assertEqual(generated.output.items[0].item_id, "q1")
                self.assertEqual(generated.usage().input_tokens, 10 if usage_reported else 0)
                self.assertEqual(generated.usage().output_tokens, 5 if usage_reported else 0)
                state = State({}, "S-0000000001", datetime.now(IST))
                check_usage(state, generated)
                self.assertEqual(state.usage_complete, usage_reported)
                self.assertEqual(
                    (await reviewer.run("Synthetic test survey")).output.relevance_score, 0.9
                )
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertEqual(
                str(request.url), "https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses"
            )
            self.assertEqual(request.headers["OpenAI-Project"], "default")
            self.assertIn(
                "/us-east-1/bedrock-mantle/aws4_request", request.headers["Authorization"]
            )
            self.assertEqual(request.headers["X-Amz-Security-Token"], "test-token")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "openai.gpt-6-luna")
            self.assertFalse(body["store"])
            self.assertEqual(body["reasoning"]["effort"], "low")
            self.assertNotIn("temperature", body)

    def test_signing_refreshes_credentials_and_replaces_placeholder_token(self):
        session = self.session()
        auth = MantleSigV4Auth("us-east-1", session=session)
        request = httpx.Request(
            "POST",
            "https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses",
            headers={"Authorization": "Bearer placeholder"},
            content=b"{}",
        )
        auth.sign(request)
        first = request.headers["Authorization"]
        session.get_credentials.return_value = Credentials("NEWACCESS", "new-secret", "new-token")
        auth.sign(request)
        self.assertNotEqual(request.headers["Authorization"], first)
        self.assertIn("NEWACCESS/", request.headers["Authorization"])
        self.assertEqual(request.headers["X-Amz-Security-Token"], "new-token")
        self.assertNotIn("Bearer", request.headers["Authorization"])


if __name__ == "__main__":
    unittest.main()
