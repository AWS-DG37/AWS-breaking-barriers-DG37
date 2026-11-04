"""Snapshot configuration and prompt text for reproducibility and optional S3 overrides."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import boto3

from mel.config import AgentConfig, Settings, load_config
from mel.contracts import Contracts
from mel.decision import CropCalendar, DecisionEngine, build_engine

# Subpackages that read or present runs but never change generation. Editing them
# must not invalidate a resumable run's fingerprint.
OUTSIDE_GENERATION = frozenset({"evaluation", "experiments", "explorer"})


def read_bytes(uri: str, region: str) -> bytes:
    if uri.startswith("s3://"):
        bucket, key = uri[5:].split("/", 1)
        return (
            boto3.client("s3", region_name=region).get_object(Bucket=bucket, Key=key)["Body"].read()
        )
    return Path(uri).read_bytes()


def write_bytes(uri: str, body: bytes, region: str) -> None:
    if uri.startswith("s3://"):
        bucket, key = uri[5:].split("/", 1)
        boto3.client("s3", region_name=region).put_object(
            Bucket=bucket, Key=key, Body=body, ContentType="application/x-ndjson"
        )
    else:
        path = Path(uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)


def local_bundle(config_path: Path, override: str | None = None) -> dict:
    config = load_config(config_path, override)
    with config.calendar_path.open() as source:
        calendar = list(csv.DictReader(source))
    return {
        "config": config.model_dump(mode="json"),
        "prompts": {
            "generate": config.prompts.generate.read_text(),
            "review": config.prompts.review.read_text(),
        },
        "rules": json.loads(config.rules_path.read_text()),
        "calendar": calendar,
        "schemas": {
            p.name: json.loads(p.read_text()) for p in config.schemas_path.glob("*.schema.json")
        },
    }


@dataclass
class Bundle:
    config: AgentConfig
    snapshot: dict
    contracts: Contracts

    @classmethod
    def load(cls, settings: Settings, uri: str | None = None) -> Bundle:
        snapshot = (
            json.loads(read_bytes(uri, settings.aws_region))
            if uri
            else local_bundle(settings.config_path)
        )
        config = AgentConfig.model_validate(snapshot["config"])
        for model in (config.generation, config.review):
            model.model_id = model.model_id or settings.bedrock_model_id
            if not model.model_id:
                raise ValueError("Set model_id in YAML or MEL_BEDROCK_MODEL_ID")
        snapshot["config"] = config.model_dump(mode="json")
        return cls(config, snapshot, Contracts(schemas=snapshot["schemas"]))

    def calendar(self) -> CropCalendar:
        return CropCalendar(self.snapshot["calendar"])

    def decision_engine(self) -> DecisionEngine:
        return build_engine(
            self.snapshot["rules"],
            self.config.decision,
            self.calendar(),
            lambda insight: self.contracts.errors("insight", insight),
        )

    def fingerprint(self, input_bytes: bytes, region: str) -> str:
        # Bundles contain their source paths for traceability, but execution uses the
        # embedded text/data. Moving an identical bundle between teammates is safe.
        snapshot = {**self.snapshot, "config": dict(self.snapshot["config"])}
        for key in ("prompts", "rules_path", "calendar_path", "schemas_path"):
            snapshot["config"].pop(key, None)
        package = Path(__file__).parent
        sources = [
            p
            for p in sorted(package.rglob("*.py"))
            if p.relative_to(package).parts[0] not in OUTSIDE_GENERATION
        ]
        manifest = {
            "bundle": snapshot,
            "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
            "region": region,
            "implementation_sha256": hashlib.sha256(
                b"".join(str(p.relative_to(package)).encode() + p.read_bytes() for p in sources)
            ).hexdigest(),
        }
        return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
