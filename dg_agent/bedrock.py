"""Small, bounded text-chat connection to the workshop's Bedrock account."""

import os

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError, ClientError, NoCredentialsError, PartialCredentialsError,
    ProfileNotFound,
)

MODEL_ID = "amazon.nova-lite-v1:0"
REGION = "us-west-2"
PROFILE = os.environ.get("AWS_PROFILE", "digital-green")


class BedrockError(RuntimeError):
    """An AWS failure with a message safe to show in the app."""


def chat(messages, *, client=None):
    """Send up to ten exchanges; return reply text and actual token usage."""
    if not isinstance(messages, list) or not messages or len(messages) > 19:
        raise ValueError("Start a new chat after ten exchanges.")
    if len(messages) % 2 == 0:
        raise ValueError("The conversation must end with your question.")
    for index, message in enumerate(messages):
        expected_role = "user" if index % 2 == 0 else "assistant"
        if not isinstance(message, dict) or message.get("role") != expected_role:
            raise ValueError("Messages must alternate between you and the assistant.")
        text = message.get("content")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("Each message must contain 1–4,000 characters.")
    if sum(len(message["content"]) for message in messages) > 16000:
        raise ValueError("This conversation is getting long. Start a new chat.")

    try:
        if client is None:
            client = boto3.Session(profile_name=PROFILE).client(
                "bedrock-runtime", region_name=REGION,
                config=Config(connect_timeout=5, read_timeout=45,
                              retries={"total_max_attempts": 1}),
            )
        response = client.converse(
            modelId=MODEL_ID,
            messages=[{"role": message["role"], "content": [{"text": message["content"]}]}
                      for message in messages],
            inferenceConfig={"maxTokens": 512, "temperature": 0.3},
        )
    except (NoCredentialsError, PartialCredentialsError, ProfileNotFound) as error:
        raise BedrockError(
            f"Set up AWS profile '{PROFILE}' using Get AWS CLI credentials in the workshop."
        ) from error
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code in {"ExpiredToken", "ExpiredTokenException", "UnrecognizedClientException",
                    "InvalidClientTokenId", "InvalidSignatureException"}:
            message = "Refresh this Mac's AWS profile using Get AWS CLI credentials in the workshop."
        elif code == "AccessDeniedException":
            message = "This account cannot invoke Nova Lite. Ask your workshop organizer about Bedrock access."
        else:
            message = "Bedrock could not answer right now. Please try again in a moment."
        raise BedrockError(message) from error
    except BotoCoreError as error:
        raise BedrockError("Could not reach Bedrock. Check your connection and try again.") from error

    text = "".join(block.get("text", "") for block in response["output"]["message"]["content"])
    if not text.strip():
        raise BedrockError("Bedrock returned no text. Please try again.")
    return {"text": text, "input_tokens": response["usage"]["inputTokens"],
            "output_tokens": response["usage"]["outputTokens"]}
