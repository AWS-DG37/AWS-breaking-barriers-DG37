"""Scripted improver and judge models for improvement-loop tests."""

import json

from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from mel.improvement import improver_agents


def first_prompt(messages) -> dict:
    part = next(p for p in messages[0].parts if isinstance(p, UserPromptPart))
    return json.loads(part.content)


def improver_factory(proposals: list[dict], scores: list[float], seen: list | None = None):
    """Improver replies with `proposals` in order (including retries); the judge gives
    every comment the next score in `scores`, one score per judge call."""

    def improve(messages, info):
        if seen is not None:
            seen.append(first_prompt(messages))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, proposals.pop(0))])

    def judge(messages, info):
        score = scores.pop(0)
        judgements = [
            {"item_id": f["item_id"], "score": score, "addressed": score >= 0.75, "reason": "ok"}
            for f in first_prompt(messages)["feedback"]
        ]
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"judgements": judgements})]
        )

    def factory(model, region, project_id):
        return improver_agents(FunctionModel(improve), FunctionModel(judge), {})

    return factory


def edit(find: str, replace: str, summary: str = "Ask about the outcome first") -> dict:
    return {
        "summary": summary,
        "prompt_edits": [
            {"prompt": "generate", "find": find, "replace": replace, "reason": "comment 1"}
        ],
        "setting_changes": [],
    }
