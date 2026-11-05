"""Amazon Bedrock AgentCore Runtime entry point for the MEL survey agents.

Request payload (JSON):
  {"insight": {...one insight row...},
   "generator": "<model id>", "verifier": "<model id>",      optional, allowlisted in service.py
   "pipeline": "direct" | "translate", "max_retries": 0-2,  optional
   "gate": "rules" | "classifier", "classifier": "<model id>"}  optional
  {"action": "options"}  returns the allowed models and defaults
  {"action": "decide", "insight": {...}, "gate": ..., "classifier": ...}  the gate alone

Response: {"ok": true, "result": {...}} or {"ok": false, "error": "..."}.

Local run:   PORT=8081 python main.py
             curl -X POST localhost:8081/invocations -H 'content-type: application/json' \
                  -d '{"action":"options"}'
"""

import logging
import os

# AgentCore's filesystem outside /tmp is not for app writes; keep the translation cache there.
os.environ.setdefault("MEL_CACHE_DIR", "/tmp/mel-cache")

import service  # noqa: E402
from bedrock_agentcore.runtime import BedrockAgentCoreApp  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("mel.agent")
app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload, context=None):
    payload = payload or {}
    if payload.get("action") == "options":
        return {"ok": True, "result": service.options()}
    if payload.get("action") == "decide":  # the gate alone: rules, or one classifier call
        try:
            result = service.decide_only(
                payload.get("insight"), payload.get("gate", "rules"), payload.get("classifier")
            )
            return {"ok": True, "result": result}
        except (service.BadRequest, service.DecisionFailed) as error:
            return {"ok": False, "error": str(error)}
    try:
        result = service.generate_survey(
            payload.get("insight"),
            payload.get("generator"),
            payload.get("verifier"),
            payload.get("pipeline", "direct"),
            payload.get("translator"),
            payload.get("max_retries", 1),
            payload.get("gate", "rules"),
            payload.get("classifier"),
        )
    except service.BadRequest as error:
        return {"ok": False, "error": str(error)}
    # Log ids and outcomes only; insight text and surveys stay out of the logs.
    log.info(
        "insight=%s gate=%s status=%s attempts=%s generator=%s seconds=%s",
        result.get("insight_id"),
        result.get("gate"),
        result.get("status"),
        result.get("attempts"),
        result["models"]["generator"],
        result.get("elapsed_seconds"),
    )
    return {"ok": True, "result": result}


if __name__ == "__main__":
    app.run(
        port=int(os.environ.get("PORT", "8080"))
    )  # AgentCore requires 8080; PORT is for local runs
