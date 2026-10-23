"""MCP endpoint of the LangChain weather agent (one MCP per agent).

Published with external ingress because the bridge lives in another ACA environment;
every call needs an Entra app token with the Agent.Invoke role.
"""
import asyncio
import hashlib
import json
import logging
import os
import time
from typing import Mapping
from uuid import UUID, uuid4

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from entra_auth import EntraTokenVerifier

logger = logging.getLogger("weather_agent_mcp")
ROLE = "Agent.Invoke"
PROGRESS_SECONDS = 15


def thread_key(caller: str, conversation_id: str) -> str:
    return "mcp:" + hashlib.sha256(json.dumps([caller, conversation_id]).encode()).hexdigest()


def _caller() -> str:
    token = get_access_token()
    return token.client_id if token else "local"


def _conversation(conversation_id: str) -> str:
    conversation_id = conversation_id.strip()
    if not 1 <= len(conversation_id) <= 256 or not conversation_id.isprintable():
        raise ToolError("conversation_id must be 1-256 printable characters")
    return conversation_id


def build_server(env: Mapping[str, str], agent) -> MCPServer:
    local = env.get("MCP_LOCAL_DEVELOPMENT", "false").lower() == "true"
    verifier = auth = None
    if not local:
        tenant = str(UUID(env["ENTRA_TENANT_ID"]))
        audiences = {a.strip() for a in env["MCP_AUDIENCES"].split(",") if a.strip()}
        clients = frozenset(c.strip() for c in env.get("MCP_ALLOWED_CLIENT_IDS", "").split(",")
                            if c.strip())
        verifier = EntraTokenVerifier(tenant, audiences, ROLE, clients)
        auth = AuthSettings(issuer_url=f"https://login.microsoftonline.com/{tenant}/v2.0",
                            resource_server_url=env["MCP_RESOURCE_URL"],
                            validate_token_resource=False, required_scopes=[ROLE])

    mcp = MCPServer(
        name="weather-agent-langchain", title="Weather Assistant (LangChain)",
        instructions="Weather assistant. Send the user's message as-is; reuse conversation_id.",
        version="1.0.0", token_verifier=verifier, auth=auth,
    )

    @mcp.tool(
        title="Ask the weather assistant",
        description=(
            "Sends a message to the LangChain weather assistant and returns its answer in the "
            "user's language. It geocodes the city and returns the current weather (OpenMeteo). "
            "Reuse conversation_id to continue the same conversation."
        ),
    )
    async def ask_weather_agent(message: str, conversation_id: str, ctx: Context) -> str:
        message = message.strip()
        if not message or len(message) > 8000:
            raise ToolError("message must contain 1-8000 characters")
        caller = _caller()
        key = thread_key(caller, _conversation(conversation_id))
        started = time.monotonic()
        logger.info("tools/call ask caller=%s thread=%s", caller, key[4:16])
        task = asyncio.create_task(agent.reply(key, message))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=PROGRESS_SECONDS)
                if done:
                    break
                try:
                    await ctx.report_progress(round(time.monotonic() - started), None,
                                              "The weather assistant is still working")
                except Exception:
                    logger.debug("progress notification not delivered", exc_info=True)
            answer = task.result()
        except ToolError:
            raise
        except Exception:
            ref = uuid4().hex[:12]
            logger.exception("Weather agent failed; reference=%s", ref)
            raise ToolError(f"The weather assistant could not answer (reference {ref}).") from None
        finally:
            if not task.done():
                task.cancel()
        logger.info("tools/call ask thread=%s answered in %.1fs (%d chars)",
                    key[4:16], time.monotonic() - started, len(answer))
        if not answer:
            raise ToolError("The weather assistant returned an empty answer.")
        return answer

    @mcp.tool(title="Reset conversation", description="Forgets this conversation_id's history.")
    async def reset_conversation(conversation_id: str) -> str:
        caller = _caller()
        key = thread_key(caller, _conversation(conversation_id))
        await agent.reset(key)
        logger.info("tools/call reset caller=%s thread=%s", caller, key[4:16])
        return "Conversation reset."

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return mcp


def create_http_app(env: Mapping[str, str], agent, host: str):
    mcp = build_server(env, agent)
    allowed = [h.strip() for h in env.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=allowed) if allowed else None
    return mcp.streamable_http_app(stateless_http=True, host=host, transport_security=security)


if __name__ == "__main__":
    import uvicorn
    from dotenv import load_dotenv

    from agent import create_weather_agent

    load_dotenv(override=False)
    logging.basicConfig(level=logging.INFO)
    env = dict(os.environ)
    host = env.get("HOST", "127.0.0.1")
    if env.get("MCP_LOCAL_DEVELOPMENT", "false").lower() == "true" and host not in {
        "127.0.0.1", "localhost", "::1"
    }:
        raise SystemExit("Unauthenticated local MCP must bind to loopback")
    uvicorn.run(create_http_app(env, create_weather_agent(env), host), host=host,
                port=int(env.get("PORT", "8080")), log_level="info")
