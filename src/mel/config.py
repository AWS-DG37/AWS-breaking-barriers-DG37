"""Typed configuration. File paths are explicit; secrets stay in environment/Secrets Manager."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelConfig(StrictModel):
    model_id: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=1)
    reasoning_effort: Literal["low", "medium", "high"] = "low"
    max_tokens: int = Field(default=1800, gt=0)


class GraphPolicy(StrictModel):
    max_revisions: int = Field(default=1, ge=0, le=5)
    relevance_threshold: float = Field(default=0.8, ge=0, le=1)
    agent_output_retries: int = Field(default=2, ge=0, le=5)
    max_requests_per_call: int = Field(default=4, ge=1)
    insight_timeout_seconds: int = Field(default=180, ge=1)


class BatchPolicy(StrictModel):
    concurrency: int = Field(default=3, ge=1, le=100)
    input_cost_per_million: float | None = Field(default=None, ge=0)
    output_cost_per_million: float | None = Field(default=None, ge=0)


class PromptPaths(StrictModel):
    generate: Path
    review: Path


class AgentConfig(StrictModel):
    version: str
    generation: ModelConfig
    review: ModelConfig
    graph: GraphPolicy = Field(default_factory=GraphPolicy)
    batch: BatchPolicy = Field(default_factory=BatchPolicy)
    prompts: PromptPaths
    rules_path: Path
    calendar_path: Path
    schemas_path: Path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEL_", extra="ignore")
    aws_region: str = "us-east-1"
    config_path: Path = Path("config/agent.yaml")
    bedrock_model_id: str | None = "openai.gpt-6-luna"  # Fallback for null YAML model IDs.
    bedrock_project_id: str = "default"
    database_host: str = "localhost"
    database_port: int = 5432
    database_name: str = "farmerchat"
    database_user: str = "mel"
    database_password: str = "mel-local-only"
    database_secret_arn: str | None = None
    database_sslmode: Literal["disable", "require", "verify-full"] = "verify-full"
    database_sslrootcert: str | None = None


def load_config(path: Path, model_override: str | None = None) -> AgentConfig:
    config = AgentConfig.model_validate(yaml.safe_load(path.read_text()))
    base = path.resolve().parent
    for field in ("rules_path", "calendar_path", "schemas_path"):
        setattr(config, field, (base / getattr(config, field)).resolve())
    for field in ("generate", "review"):
        setattr(config.prompts, field, (base / getattr(config.prompts, field)).resolve())
    if model_override:
        config.generation.model_id = model_override
        config.review.model_id = model_override
    return config
