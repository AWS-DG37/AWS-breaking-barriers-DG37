from dataclasses import replace

import boto3
from botocore.config import Config
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers import bedrock as bedrock_profiles
from pydantic_ai.providers.bedrock import BedrockModelProfile, BedrockProvider
from pydantic_ai.providers.openai import OpenAIProvider

from mel.bedrock import create_mantle_client
from mel.config import AgentConfig, ModelConfig
from mel.models import Review, SurveyDraft


class Providers:
    """Bedrock clients shared by every agent built from them, created on first use."""

    def __init__(self, region: str, project_id: str = "default"):
        self.region, self.project_id = region, project_id
        self.responses = None
        self.converse = None

    def model(self, model: ModelConfig) -> tuple[Model, dict]:
        """The native model and its request settings."""
        if not model.model_id:
            raise ValueError("Resolve a Bedrock model ID before constructing an agent")
        settings = {"max_tokens": model.max_tokens}
        if model.temperature is not None:
            settings["temperature"] = model.temperature
        if model.api == "converse":
            if self.converse is None:
                self.converse = BedrockProvider(
                    bedrock_client=boto3.client(
                        "bedrock-runtime",
                        region_name=self.region,
                        config=Config(
                            read_timeout=300,
                            connect_timeout=60,
                            retries={"total_max_attempts": 1, "mode": "standard"},
                        ),
                    )
                )
            return BedrockConverseModel(
                model.model_id, provider=self.converse, profile=converse_profile(model.model_id)
            ), settings
        if self.responses is None:
            self.responses = OpenAIProvider(
                openai_client=create_mantle_client(self.region, self.project_id, max_retries=0)
            )
        settings.update(
            openai_store=False,
            openai_reasoning_effort=model.reasoning_effort,
            openai_send_reasoning_ids=False,
        )
        return OpenAIResponsesModel(
            model.model_id,
            provider=self.responses,
            profile=OpenAIModelProfile(
                openai_supports_reasoning=True,
                openai_supports_encrypted_reasoning_content=True,
            ),
        ), settings


def build_agent(
    providers: Providers, model: ModelConfig, output: type, prompt: str, retries: int
) -> Agent:
    native, settings = providers.model(model)
    return Agent(
        native, output_type=output, instructions=prompt, model_settings=settings, retries=retries
    )


def build_agents(
    config: AgentConfig, region: str, prompts: dict[str, str], project_id: str = "default"
) -> tuple[Agent, Agent]:
    providers = Providers(region, project_id)
    retries = config.graph.agent_output_retries
    return (
        build_agent(providers, config.generation, SurveyDraft, prompts["generate"], retries),
        build_agent(providers, config.review, Review, prompts["review"], retries),
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
