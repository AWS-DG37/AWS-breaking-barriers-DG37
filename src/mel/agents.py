"""Only this adapter knows about Bedrock. Tests can inject Pydantic AI TestModel."""

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

from mel.bedrock import create_mantle_client
from mel.config import AgentConfig, ModelConfig
from mel.models import Review, SurveyDraft


def build_agents(
    config: AgentConfig, region: str, prompts: dict[str, str], project_id: str = "default"
) -> tuple[Agent, Agent]:
    client = create_mantle_client(region, project_id)
    provider = OpenAIProvider(openai_client=client)

    def agent(model: ModelConfig, output: type, prompt: str) -> Agent:
        if not model.model_id:
            raise ValueError("Resolve a Bedrock model ID before constructing an agent")
        settings = {
            "max_tokens": model.max_tokens,
            "openai_store": False,
            "openai_reasoning_effort": model.reasoning_effort,
            "openai_send_reasoning_ids": False,
        }
        if model.temperature is not None:
            settings["temperature"] = model.temperature
        return Agent(
            OpenAIResponsesModel(
                model.model_id,
                provider=provider,
                profile=OpenAIModelProfile(
                    openai_supports_reasoning=True,
                    openai_supports_encrypted_reasoning_content=True,
                ),
            ),
            output_type=output,
            instructions=prompt,
            model_settings=settings,
            retries=config.graph.agent_output_retries,
        )

    return (
        agent(config.generation, SurveyDraft, prompts["generate"]),
        agent(config.review, Review, prompts["review"]),
    )
