import asyncio
from typing import Literal

import boto3
from pydantic import Field

from mel.bedrock import create_mantle_client
from mel.config import StrictModel


class ModelCandidate(StrictModel):
    model_id: str = Field(min_length=1, pattern=r"\S")
    name: str = Field(min_length=1, pattern=r"\S")
    provider: str = Field(min_length=1, pattern=r"\S")
    api: Literal["responses", "converse"]
    status: Literal["discovered", "verified", "unavailable"] = "discovered"
    reason: str | None = None


class CatalogDiscoveryError(RuntimeError):

    def __init__(self, models: list[ModelCandidate], errors: list[str]):
        self.models = models
        self.errors = errors
        super().__init__("; ".join(errors))


def _converse_models(client, models: list[ModelCandidate], errors: list[str]) -> None:
    foundations = {}
    try:
        response = client.list_foundation_models()
        for model in response.get("modelSummaries", []):
            if "TEXT" not in model.get("inputModalities", []):
                continue
            if "TEXT" not in model.get("outputModalities", []):
                continue
            foundations[model["modelId"]] = model
            if "ON_DEMAND" not in model.get("inferenceTypesSupported", []):
                continue
            models.append(
                ModelCandidate(
                    model_id=model["modelId"],
                    name=model["modelName"],
                    provider=model["providerName"],
                    api="converse",
                    reason="Text model discovered; Converse and invocation access unverified.",
                )
            )
    except Exception as exc:
        errors.append(f"Bedrock foundation catalog: {type(exc).__name__}: {exc}")

    try:
        token = None
        while True:
            response = client.list_inference_profiles(**({"nextToken": token} if token else {}))
            for profile in response.get("inferenceProfileSummaries", []):
                references = [
                    foundations.get(model["modelArn"].split("foundation-model/")[-1])
                    for model in profile.get("models", [])
                ]
                if not references or any(model is None for model in references):
                    continue
                active = profile.get("status") == "ACTIVE"
                models.append(
                    ModelCandidate(
                        model_id=profile["inferenceProfileId"],
                        name=profile["inferenceProfileName"],
                        provider=references[0]["providerName"],
                        api="converse",
                        status="discovered" if active else "unavailable",
                        reason="Text profile discovered; Converse and invocation access unverified."
                        if active
                        else "Inference profile is not ACTIVE.",
                    )
                )
            token = response.get("nextToken")
            if not token:
                break
    except Exception as exc:
        errors.append(f"Bedrock inference profile catalog: {type(exc).__name__}: {exc}")


async def discover_models(
    region: str, *, bedrock_client=None, mantle_client=None
) -> list[ModelCandidate]:
    """List text candidates; discovery does not prove tool support or account access.

    Injected clients belong to the caller. Clients created here are closed here.
    A partial failure raises CatalogDiscoveryError with the successful entries.
    """
    models: list[ModelCandidate] = []
    errors: list[str] = []
    owned_bedrock = bedrock_client is None
    owned_mantle = mantle_client is None
    try:
        try:
            if owned_bedrock:
                bedrock_client = await asyncio.to_thread(
                    boto3.client, "bedrock", region_name=region
                )
            await asyncio.to_thread(_converse_models, bedrock_client, models, errors)
        except Exception as exc:
            errors.append(f"Bedrock catalog: {type(exc).__name__}: {exc}")
        try:
            if owned_mantle:
                mantle_client = create_mantle_client(region)
            page = await mantle_client.models.list()
            while True:
                for model in getattr(page, "data", page):
                    models.append(
                        ModelCandidate(
                            model_id=model.id,
                            name=model.id,
                            provider=model.id.split(".")[0],
                            api="responses",
                            reason="Responses model discovered; invocation access unverified.",
                        )
                    )
                if not hasattr(page, "has_next_page") or not page.has_next_page():
                    break
                page = await page.get_next_page()
        except Exception as exc:
            errors.append(f"Responses catalog: {type(exc).__name__}: {exc}")
    finally:
        if owned_bedrock and bedrock_client is not None:
            try:
                await asyncio.to_thread(bedrock_client.close)
            except Exception as exc:
                errors.append(f"Bedrock catalog close: {type(exc).__name__}: {exc}")
        if owned_mantle and mantle_client is not None:
            try:
                await mantle_client.close()
            except Exception as exc:
                errors.append(f"Responses catalog close: {type(exc).__name__}: {exc}")
    unique = {(model.api, model.model_id): model for model in models}
    result = sorted(unique.values(), key=lambda model: (model.api, model.provider, model.model_id))
    if errors:
        raise CatalogDiscoveryError(result, errors)
    return result
