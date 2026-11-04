import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.models.openai import OpenAIResponsesModel

from mel.agents import build_agents
from mel.bedrock_models import CatalogDiscoveryError, ModelCandidate, discover_models
from mel.bundle import Bundle
from mel.config import ModelConfig, Settings


def foundation(model_id="vendor.text-v1", **changes):
    return {
        "modelId": model_id,
        "modelName": "Text model",
        "providerName": "Vendor",
        "inputModalities": ["TEXT"],
        "outputModalities": ["TEXT"],
        "inferenceTypesSupported": ["ON_DEMAND"],
        "modelLifecycle": {"status": "ACTIVE"},
        **changes,
    }


def clients(models=None, profiles=None):
    bedrock = Mock()
    bedrock.list_foundation_models.return_value = {"modelSummaries": models or []}
    bedrock.list_inference_profiles.return_value = {"inferenceProfileSummaries": profiles or []}
    mantle = SimpleNamespace(models=SimpleNamespace(list=AsyncMock(return_value=[])))
    return bedrock, mantle


def test_catalog_preserves_ids_and_discovers_non_streaming_text_models():
    bedrock, mantle = clients(
        [foundation(), foundation("vendor.image", outputModalities=["IMAGE"])],
        [
            {
                "inferenceProfileId": "us.vendor.text-v1",
                "inferenceProfileName": "US Text",
                "status": "ACTIVE",
                "models": [{"modelArn": "arn:aws:bedrock:r::foundation-model/vendor.text-v1"}],
            }
        ],
    )
    mantle.models.list.return_value = [SimpleNamespace(id="openai.native-model")]
    models = asyncio.run(
        discover_models("test-region", bedrock_client=bedrock, mantle_client=mantle)
    )
    assert {(m.api, m.model_id) for m in models} == {
        ("converse", "vendor.text-v1"),
        ("converse", "us.vendor.text-v1"),
        ("responses", "openai.native-model"),
    }
    assert all(m.status == "discovered" and "verified" in m.reason for m in models)
    assert next(m for m in models if m.model_id == "vendor.text-v1").provider == "Vendor"


def test_profile_pagination_preserves_ids_and_ignores_non_text_profiles():
    bedrock, mantle = clients(
        [foundation(), foundation("vendor.image", outputModalities=["IMAGE"])]
    )
    bedrock.list_inference_profiles.side_effect = [
        {
            "inferenceProfileSummaries": [
                {
                    "inferenceProfileId": "us.vendor.text-v1",
                    "inferenceProfileName": "US text",
                    "status": "ACTIVE",
                    "models": [{"modelArn": "arn:aws:bedrock:r::foundation-model/vendor.text-v1"}],
                }
            ],
            "nextToken": "page2",
        },
        {
            "inferenceProfileSummaries": [
                {
                    "inferenceProfileId": "us.vendor.image",
                    "inferenceProfileName": "US image",
                    "status": "ACTIVE",
                    "models": [{"modelArn": "arn:aws:bedrock:r::foundation-model/vendor.image"}],
                }
            ]
        },
    ]
    models = asyncio.run(discover_models("r", bedrock_client=bedrock, mantle_client=mantle))
    assert {m.model_id for m in models} == {"vendor.text-v1", "us.vendor.text-v1"}
    assert bedrock.list_inference_profiles.call_args.kwargs == {"nextToken": "page2"}


def test_catalog_reports_partial_failures_without_hiding_discovered_models():
    bedrock, mantle = clients([foundation()])
    mantle.models.list.side_effect = RuntimeError("catalog unavailable")
    with pytest.raises(CatalogDiscoveryError, match="Responses") as failure:
        asyncio.run(discover_models("r", bedrock_client=bedrock, mantle_client=mantle))
    assert [m.model_id for m in failure.value.models] == ["vendor.text-v1"]
    assert failure.value.errors


def test_catalog_closes_only_owned_mantle_client(monkeypatch):
    bedrock, mantle = clients()
    mantle.close = AsyncMock()
    monkeypatch.setattr("mel.bedrock_models.boto3.client", Mock(return_value=bedrock))
    monkeypatch.setattr("mel.bedrock_models.create_mantle_client", Mock(return_value=mantle))
    asyncio.run(discover_models("r"))
    mantle.close.assert_awaited_once()
    mantle.close.reset_mock()
    asyncio.run(discover_models("r", bedrock_client=bedrock, mantle_client=mantle))
    mantle.close.assert_not_awaited()


def test_model_config_defaults_and_rejects_unknown_apis():
    assert ModelConfig().api == "responses"
    with pytest.raises(ValidationError):
        ModelConfig(api="custom")
    with pytest.raises(ValidationError):
        ModelCandidate(model_id=" ", name="x", provider="x", api="converse", status="verified")


def test_agents_select_native_converse_without_openai_settings(monkeypatch):
    bundle = Bundle.load(Settings())
    bundle.config.generation = ModelConfig(
        model_id="vendor.text-v1", api="converse", temperature=0.3
    )
    bundle.config.review = ModelConfig(model_id="vendor.text-v2", api="converse")
    native_provider = Mock()
    native_provider.client = Mock()
    native_provider.model_profile = None
    monkeypatch.setattr("mel.agents.BedrockProvider", Mock(return_value=native_provider))
    forbidden = Mock(side_effect=AssertionError("Responses client must not be constructed"))
    monkeypatch.setattr("mel.agents.create_mantle_client", forbidden)
    generator, reviewer = build_agents(bundle.config, "r", bundle.snapshot["prompts"])
    assert isinstance(generator.model, BedrockConverseModel)
    assert generator.model.model_name == "vendor.text-v1"
    assert generator.model_settings == {"max_tokens": 1800, "temperature": 0.3}
    assert reviewer.model_settings == {"max_tokens": 1800}


def test_mixed_agents_keep_native_responses_compatible(monkeypatch):
    bundle = Bundle.load(Settings())
    bundle.config.review = ModelConfig(model_id="vendor.text-v1", api="converse")
    client = Mock()
    monkeypatch.setattr("mel.agents.create_mantle_client", Mock(return_value=client))
    monkeypatch.setattr("mel.agents.OpenAIProvider", Mock())
    provider = Mock()
    provider.client = Mock()
    provider.model_profile = None
    monkeypatch.setattr("mel.agents.BedrockProvider", Mock(return_value=provider))
    generator, reviewer = build_agents(bundle.config, "r", bundle.snapshot["prompts"])
    assert isinstance(generator.model, OpenAIResponsesModel)
    assert isinstance(reviewer.model, BedrockConverseModel)
    assert generator.model_settings["openai_store"] is False
    assert generator.model_settings["openai_reasoning_effort"] == "low"


def test_native_converse_structured_output_uses_boto_runtime(monkeypatch):
    import boto3
    from botocore.stub import Stubber
    from pydantic_ai.providers.bedrock import BedrockProvider

    runtime = boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test-secret",
    )
    provider = BedrockProvider(bedrock_client=runtime)
    monkeypatch.setattr("mel.agents.BedrockProvider", Mock(return_value=provider))
    bundle = Bundle.load(Settings())
    bundle.config.generation = ModelConfig(
        model_id="anthropic.claude-3-haiku-20240307-v1:0", api="converse"
    )
    bundle.config.review = bundle.config.generation.model_copy()
    generator, _ = build_agents(bundle.config, "us-east-1", bundle.snapshot["prompts"])
    draft = {
        "items": [
            {
                "item_id": "q1",
                "type": "survey-confirm",
                "prompt": "Did you spray?",
                "options": [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}],
            }
        ],
        "rationale": "Follow up on advice.",
    }
    with Stubber(runtime) as stubber:
        stubber.add_response(
            "converse",
            {
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "toolUse": {
                                    "toolUseId": "call-1",
                                    "name": "final_result",
                                    "input": draft,
                                }
                            }
                        ],
                    }
                },
                "stopReason": "tool_use",
                "metrics": {"latencyMs": 1},
                "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
            },
        )
        result = asyncio.run(generator.run("Synthetic test context"))
        assert result.output.items[0].item_id == "q1"
        assert result.usage().input_tokens == 10
        stubber.assert_no_pending_responses()
    runtime.close()


def test_native_failure_does_not_switch_model_or_api(monkeypatch):
    import boto3
    from botocore.stub import Stubber
    from pydantic_ai.providers.bedrock import BedrockProvider

    runtime = boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test-secret",
    )
    provider = BedrockProvider(bedrock_client=runtime)
    monkeypatch.setattr("mel.agents.BedrockProvider", Mock(return_value=provider))
    responses = Mock(side_effect=AssertionError("No API fallback is allowed"))
    monkeypatch.setattr("mel.agents.create_mantle_client", responses)
    bundle = Bundle.load(Settings())
    bundle.config.generation = ModelConfig(model_id="vendor.denied", api="converse")
    bundle.config.review = bundle.config.generation.model_copy()
    generator, _ = build_agents(bundle.config, "us-east-1", bundle.snapshot["prompts"])
    with Stubber(runtime) as stubber:
        stubber.add_client_error(
            "converse",
            service_error_code="AccessDeniedException",
            service_message="Invocation denied",
            http_status_code=403,
        )
        with pytest.raises(Exception, match="Invocation denied"):
            asyncio.run(generator.run("Synthetic test context"))
        stubber.assert_no_pending_responses()
    assert generator.model.model_name == "vendor.denied"
    responses.assert_not_called()
    runtime.close()


def test_foundation_catalog_failure_keeps_responses_candidates():
    bedrock, mantle = clients()
    bedrock.list_foundation_models.side_effect = RuntimeError("No foundation listing access")
    mantle.models.list.return_value = [SimpleNamespace(id="openai.native-model")]
    with pytest.raises(CatalogDiscoveryError, match="foundation") as failure:
        asyncio.run(discover_models("r", bedrock_client=bedrock, mantle_client=mantle))
    assert [m.model_id for m in failure.value.models] == ["openai.native-model"]


def test_responses_pagination_failure_keeps_earlier_page():
    bedrock, mantle = clients()
    mantle.models.list.return_value = SimpleNamespace(
        data=[SimpleNamespace(id="openai.native-model")],
        has_next_page=lambda: True,
        get_next_page=AsyncMock(side_effect=RuntimeError("Second page unavailable")),
    )
    with pytest.raises(CatalogDiscoveryError, match="Second page") as failure:
        asyncio.run(discover_models("r", bedrock_client=bedrock, mantle_client=mantle))
    assert [m.model_id for m in failure.value.models] == ["openai.native-model"]
