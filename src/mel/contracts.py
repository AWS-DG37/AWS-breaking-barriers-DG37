"""Validate against the participant pack, including its local schema references."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


class Contracts:
    def __init__(self, directory: Path | None = None, *, schemas: dict | None = None):
        if schemas is None:
            if directory is None:
                raise ValueError("directory or schemas is required")
            schemas = {p.name: json.loads(p.read_text()) for p in directory.glob("*.schema.json")}
        registry = Registry()
        for name, schema in schemas.items():
            Draft202012Validator.check_schema(schema)
            resource = Resource.from_contents(schema)
            registry = registry.with_resource(schema["$id"], resource)
        self.validators = {
            name.removesuffix(".schema.json"): Draft202012Validator(
                schema, registry=registry, format_checker=FormatChecker()
            )
            for name, schema in schemas.items()
        }

    def errors(self, name: str, value: dict) -> list[str]:
        return [e.message for e in self.validators[name].iter_errors(value)]

    def validate(self, name: str, value: dict) -> None:
        self.validators[name].validate(value)
