"""Bedrock Mantle Responses transport using refreshable AWS role credentials.

The pinned Pydantic AI version predates its native Mantle provider. This small
HTTPX auth adapter signs each finalized request (including retries) with botocore.
No OpenAI or Bedrock bearer API key is required.
"""

import asyncio

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import NoCredentialsError
from openai import AsyncOpenAI


class MantleSigV4Auth(httpx.Auth):
    requires_request_body = True

    def __init__(self, region: str, session=None):
        self.region = region
        self.session = session if session is not None else boto3.Session(region_name=region)

    def sign(self, request: httpx.Request) -> None:
        credentials = self.session.get_credentials()
        if credentials is None:
            raise NoCredentialsError()
        # The OpenAI-compatible SDK requires an api_key argument. Replace its
        # placeholder Authorization header; it must never leave as a bearer token.
        request.headers.pop("Authorization", None)
        request.headers.pop("X-Amz-Date", None)
        request.headers.pop("X-Amz-Security-Token", None)
        signed = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers=dict(request.headers),
        )
        SigV4Auth(credentials.get_frozen_credentials(), "bedrock-mantle", self.region).add_auth(
            signed
        )
        request.headers.update(dict(signed.headers))

    def auth_flow(self, request):
        self.sign(request)
        yield request

    async def async_auth_flow(self, request):
        # ECS/profile credential refresh may perform blocking I/O.
        await asyncio.to_thread(self.sign, request)
        yield request


def create_mantle_client(
    region: str, project_id: str = "default", *, transport=None
) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=f"https://bedrock-mantle.{region}.api.aws/openai/v1",
        api_key="aws-sigv4-placeholder",
        project=project_id,
        max_retries=4,
        http_client=httpx.AsyncClient(
            auth=MantleSigV4Auth(region),
            timeout=httpx.Timeout(120, connect=10),
            transport=transport,
        ),
    )
