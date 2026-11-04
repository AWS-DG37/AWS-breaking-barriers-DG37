"""Typed configuration. File paths are explicit; secrets stay in environment/Secrets Manager."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelConfig(StrictModel):
    model_id: str | None = None
    api: Literal["responses", "converse"] = "responses"
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


class GoldenWindowConfig(StrictModel):
    """Send windows as golden set v1 draws them; see GoldenWindowPolicy."""

    days: dict[str, tuple[int, int]]
    opens: str
    closes: str
    same_day_delay_hours: int = Field(ge=0)
    same_day_length_hours: int = Field(gt=0)


class DecisionPolicy(StrictModel):
    human_call_severities: list[str] = Field(min_length=1)
    window_policy: Literal["pack", "golden_v1"]
    golden_v1: GoldenWindowConfig | None = None

    @model_validator(mode="after")
    def golden_settings_present(self):
        if self.window_policy == "golden_v1" and self.golden_v1 is None:
            raise ValueError("window_policy golden_v1 requires decision.golden_v1 settings")
        return self


class PromptPaths(StrictModel):
    generate: Path
    review: Path


class AgentConfig(StrictModel):
    version: str
    generation: ModelConfig
    review: ModelConfig
    graph: GraphPolicy = Field(default_factory=GraphPolicy)
    batch: BatchPolicy = Field(default_factory=BatchPolicy)
    decision: DecisionPolicy
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
    # The participant pack's insights: a repository path locally, an s3:// URI when hosted
    # (the image omits datasets).
    pack_uri: str = "docs/participant-pack/data/insights.jsonl"
    # Hosted deployments start one Fargate task per queued job. Unset, jobs wait for a
    # polling runner (`mel-worker jobs`), as in local development.
    job_cluster: str | None = None
    job_task_definition: str | None = None
    job_container: str = "worker"
    job_subnets: list[str] = []
    job_security_groups: list[str] = []
    # Hosted sign-in (Cognito). All four together, or none for an open local console.
    auth_domain: str | None = None  # https://<prefix>.auth.<region>.amazoncognito.com
    auth_client_id: str | None = None
    auth_issuer: str | None = None  # https://cognito-idp.<region>.amazonaws.com/<pool id>
    console_url: str | None = None  # https://<distribution>.cloudfront.net, no trailing slash

    @model_validator(mode="after")
    def complete_sign_in(self):
        fields = (self.auth_domain, self.auth_client_id, self.auth_issuer, self.console_url)
        if any(fields) and not all(fields):
            raise ValueError("set all of auth_domain, auth_client_id, auth_issuer, console_url")
        return self


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
