"""Strands agent that replaces the raw Bedrock converse call.

Model: Amazon Bedrock via strands.models.BedrockModel
Tools: the AWS MCP server (SigV4-signed through mcp-proxy-for-aws)

Env (optional):
  AWS_REGION   default us-east-1
  MODEL_ID     default global.anthropic.claude-haiku-5-5
  TEMPERATURE  unset by default
  MCP_URL      default https://aws-mcp.<AWS_REGION>.api.aws/mcp
  MCP_SERVICE  default aws-mcp   (SigV4 signing name)
  USE_MCP      set to 0 to run the agent with no MCP tools
"""
import os

from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient


def _model():
    kwargs = {
        "model_id": os.environ.get("MODEL_ID", "global.anthropic.claude-haiku-5-5"),
        "region_name": os.environ.get("AWS_REGION", "us-east-1"),
        "max_tokens": 2000,
    }
    if os.environ.get("TEMPERATURE"):
        kwargs["temperature"] = float(os.environ["TEMPERATURE"])
    return BedrockModel(**kwargs)


def _mcp_client():
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
    region = os.environ.get("AWS_REGION", "us-east-1")
    url = os.environ.get("MCP_URL", f"https://aws-mcp.{region}.api.aws/mcp")
    service = os.environ.get("MCP_SERVICE", "aws-mcp")
    return MCPClient(lambda: aws_iam_streamablehttp_client(
        endpoint=url, aws_region=region, aws_service=service))


def run_agent(system, user):
    """Run the generator prompt through a Strands agent and return its text output."""
    if os.environ.get("USE_MCP", "1") == "0":
        agent = Agent(model=_model(), system_prompt=system, callback_handler=None)
        return str(agent(user))
    with _mcp_client() as mcp:
        agent = Agent(model=_model(), system_prompt=system,
                      tools=mcp.list_tools_sync(), callback_handler=None)
        return str(agent(user))
