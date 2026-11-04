from dataclasses import replace

import boto3
from botocore.config import Config
from pydantic_ai import Agent
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers import bedrock as bedrock_profiles
from pydantic_ai.providers.bedrock import BedrockModelProfile, BedrockProvider
from pydantic_ai.providers.openai import OpenAIProvider

from mel.bedrock import create_mantle_client
from mel.config import AgentConfig, ModelConfig
from mel.models import Review, SurveyDraft


def build_agents(
    config: AgentConfig, region: str, prompts: dict[str, str], project_id: str = "default"
) -> tuple[Agent, Agent]:
    responses_provider = None
    converse_provider = None

    def agent(model: ModelConfig, output: type, prompt: str) -> Agent:
        nonlocal responses_provider, converse_provider
        if not model.model_id:
            raise ValueError("Resolve a Bedrock model ID before constructing an agent")
        settings = {"max_tokens": model.max_tokens}
        if model.temperature is not None:
            settings["temperature"] = model.temperature
        if model.api == "converse":
            if converse_provider is None:
                converse_provider = BedrockProvider(
                    bedrock_client=boto3.client(
                        "bedrock-runtime",
                        region_name=region,
                        config=Config(
                            read_timeout=300,
                            connect_timeout=60,
                            retries={"total_max_attempts": 1, "mode": "standard"},
                        ),
                    )
                )
            native_model = BedrockConverseModel(
                model.model_id, provider=converse_provider, profile=converse_profile(model.model_id)
            )
        else:
            if responses_provider is None:
                responses_provider = OpenAIProvider(
                    openai_client=create_mantle_client(region, project_id, max_retries=0)
                )
            settings.update(
                openai_store=False,
                openai_reasoning_effort=model.reasoning_effort,
                openai_send_reasoning_ids=False,
            )
            native_model = OpenAIResponsesModel(
                model.model_id,
                provider=responses_provider,
                profile=OpenAIModelProfile(
                    openai_supports_reasoning=True,
                    openai_supports_encrypted_reasoning_content=True,
                ),
            )
        return Agent(
            native_model,
            output_type=output,
            instructions=prompt,
            model_settings=settings,
            retries=config.graph.agent_output_retries,
        )

    return (
        agent(config.generation, SurveyDraft, prompts["generate"]),
        agent(config.review, Review, prompts["review"]),
    )


def converse_profile(model_id: str) -> BedrockModelProfile:
    """Pydantic AI's Bedrock profile, without a forced tool choice. Survey drafts arrive
    as an output tool call; forcing that call (toolChoice "any") is refused by some
    models, such as Claude Sonnet and Opus 5.5, while every model with tool support
    calls the output tool when offered it, and Pydantic AI retries a text-only reply."""
    profile = BedrockModelProfile.from_profile(
        bedrock_profiles.BedrockProvider.model_profile(model_id)
    )
    return replace(profile, bedrock_supports_tool_choice=False)
