"""Construct native Pydantic AI models for the selected Bedrock API."""

from pydantic_ai import Agent
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.bedrock import BedrockProvider
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
                converse_provider = BedrockProvider(region_name=region)
            native_model = BedrockConverseModel(model.model_id, provider=converse_provider)
        else:
            if responses_provider is None:
                responses_provider = OpenAIProvider(
                    openai_client=create_mantle_client(region, project_id)
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
