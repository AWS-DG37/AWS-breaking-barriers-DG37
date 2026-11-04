"""Strands agents for the MEL pipeline: a generator (writes the survey) and a verifier (reviews it).

Model: Amazon Bedrock via strands.models.BedrockModel
Generator tools: the AWS MCP server (SigV4-signed through mcp-proxy-for-aws)
Verifier tools: none, and a different model from the generator

Env (optional):
  AWS_REGION            default us-east-1
  MODEL_ID              generator model, default global.anthropic.claude-haiku-5-5
  TEMPERATURE           generator temperature, unset by default
  MAX_TOKENS            generator output cap, default 4000 (Hindi JSON used up to ~1900, 2000 was too tight)
  VERIFIER_MODEL_ID     default global.anthropic.claude-sonnet-5-5
  VERIFIER_TEMPERATURE  unset by default (Sonnet 5.5 does not accept it)
  VERIFIER_MAX_TOKENS   default 4000
  MCP_URL               default https://aws-mcp.<AWS_REGION>.api.aws/mcp
  MCP_SERVICE           default aws-mcp   (SigV4 signing name)
  USE_MCP               set to 0 to run the generator with no MCP tools
"""
import os

from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient


def _model():
    kwargs = {
        "model_id": os.environ.get("MODEL_ID", "global.anthropic.claude-haiku-5-5"),
        "region_name": os.environ.get("AWS_REGION", "us-east-1"),
        "max_tokens": int(os.environ.get("MAX_TOKENS", "8000")),
    }
    if os.environ.get("TEMPERATURE"):
        kwargs["temperature"] = float(os.environ["TEMPERATURE"])
    return BedrockModel(**kwargs)


def _verifier_model():
    kwargs = {
        "model_id": os.environ.get("VERIFIER_MODEL_ID", "global.anthropic.claude-sonnet-5-5"),
        "region_name": os.environ.get("AWS_REGION", "us-east-1"),
        "max_tokens": int(os.environ.get("VERIFIER_MAX_TOKENS", "8000")),
    }
    # Sonnet 5.5 rejects temperature ("deprecated for this model"), so it is opt-in.
    if os.environ.get("VERIFIER_TEMPERATURE"):
        kwargs["temperature"] = float(os.environ["VERIFIER_TEMPERATURE"])
    return BedrockModel(**kwargs)


def _mcp_client():
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
    region = os.environ.get("AWS_REGION", "us-east-1")
    url = os.environ.get("MCP_URL", f"https://aws-mcp.{region}.api.aws/mcp")
    service = os.environ.get("MCP_SERVICE", "aws-mcp")
    return MCPClient(lambda: aws_iam_streamablehttp_client(
        endpoint=url, aws_region=region, aws_service=service))


def _invoke(agent, user, label):
    """Call the agent and print token usage, so limit problems show up with real numbers."""
    try:
        result = agent(user)
    except Exception as e:
        print(f"[{label}] FAILED: {type(e).__name__}: {e}")
        raise
    m = result.metrics
    print(f"[{label}] stop_reason={result.stop_reason} usage={m.accumulated_usage} "
          f"model_calls={len(m.agent_invocations[-1].cycles) if m.agent_invocations else '?'} "
          f"tool_calls={list(m.tool_metrics)}")
    return str(result)


def run_agent(system, user):
    """GENERATOR: run the generator prompt through a Strands agent and return its text output."""
    if os.environ.get("USE_MCP", "1") == "0":
        agent = Agent(model=_model(), system_prompt=system, callback_handler=None)
        return _invoke(agent, user, "generator")
    with _mcp_client() as mcp:
        agent = Agent(model=_model(), system_prompt=system,
                      tools=mcp.list_tools_sync(), callback_handler=None)
        return _invoke(agent, user, "generator")


def run_verifier(system, user):
    """VERIFIER: a different model from the generator, no tools. Returns its text output."""
    agent = Agent(model=_verifier_model(), system_prompt=system, callback_handler=None)
    return _invoke(agent, user, "verifier")
