"""MCP adapter for the Renewal Risk Analyst running in Vertex AI Agent Engine.

One MCP server per remote agent. It does not reimplement the agent: every tool call
goes to the deployed Agent Engine resource, which keeps its model, tools and history.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
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

from agent_engine import RenewalEngine, create_engine
from entra_auth import EntraTokenVerifier

logger = logging.getLogger("renewal_risk_mcp")
ROLE = "Agent.Invoke"
PROGRESS_SECONDS = 15
RESOURCE_RE = r"projects/[a-zA-Z0-9-]+/locations/[a-z][a-z0-9-]+/reasoningEngines/[0-9]+"


def session_key(caller: str, conversation_id: str) -> str:
    """Agent Engine user_id: callers never share sessions even if they reuse conversation ids."""
    digest = hashlib.sha256(json.dumps([caller, conversation_id]).encode()).hexdigest()
    return "mcp:" + digest


def _caller() -> str:
    token = get_access_token()
    return token.client_id if token else "local"


def _check_conversation(conversation_id: str) -> str:
    conversation_id = conversation_id.strip()
    if not 1 <= len(conversation_id) <= 256 or not conversation_id.isprintable():
        raise ToolError("conversation_id must be 1-256 printable characters")
    return conversation_id


async def _remote(call, what: str):
    try:
        return await call
    except Exception:
        ref = uuid4().hex[:12]
        logger.exception("Agent Engine %s failed; reference=%s", what, ref)
        raise ToolError(f"The GCP agent could not complete the request (reference {ref}).") from None


def build_server(env: Mapping[str, str], engine: RenewalEngine) -> MCPServer:
    local = env.get("MCP_LOCAL_DEVELOPMENT", "false").lower() == "true"
    verifier = auth = None
    if not local:
        tenant = str(UUID(env["ENTRA_TENANT_ID"]))
        audiences = {a.strip() for a in env["MCP_AUDIENCES"].split(",") if a.strip()}
        clients = frozenset(c.strip() for c in env.get("MCP_ALLOWED_CLIENT_IDS", "").split(",")
                            if c.strip())
        verifier = EntraTokenVerifier(tenant, audiences, ROLE, clients)
        auth = AuthSettings(
            issuer_url=f"https://login.microsoftonline.com/{tenant}/v2.0",
            resource_server_url=env["MCP_RESOURCE_URL"],
            validate_token_resource=False,  # EntraTokenVerifier checks aud itself
            required_scopes=[ROLE],
        )

    mcp = MCPServer(
        name="renewal-risk-analyst-gcp",
        title="Renewal Risk Analyst (GCP Agent Engine)",
        instructions=(
            "Gateway to the Renewal Risk Analyst agent deployed in Google Cloud. "
            "Send the user's request as-is; keep the same conversation_id for follow-ups."
        ),
        version="1.0.0",
        token_verifier=verifier,
        auth=auth,
    )

    @mcp.tool(
        title="Ask the Renewal Risk Analyst",
        description=(
            "Sends a message to the Renewal Risk Analyst agent in GCP and returns its final answer. "
            "It analyses CRM, support, product usage and contracts, scores renewal risk and drafts "
            "customer emails. Reuse conversation_id to continue the same conversation. "
            "Calls can take 20 seconds to 3 minutes."
        ),
    )
    async def ask_renewal_risk_analyst(message: str, conversation_id: str, ctx: Context) -> str:
        message = message.strip()
        if not message or len(message) > 16000:
            raise ToolError("message must contain 1-16000 characters")
        caller = _caller()
        key = session_key(caller, _check_conversation(conversation_id))
        started = time.monotonic()
        logger.info("tools/call ask caller=%s key=%s", caller, key[4:16])
        task = asyncio.create_task(_remote(engine.reply(key, message), "query"))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=PROGRESS_SECONDS)
                if done:
                    break
                try:
                    await ctx.report_progress(round(time.monotonic() - started), None,
                                              "El Renewal Risk Analyst sigue trabajando en GCP")
                except Exception:  # client without progress token or gone; keep waiting
                    logger.debug("progress notification not delivered", exc_info=True)
            answer = task.result()
        finally:
            if not task.done():
                task.cancel()
        logger.info("tools/call ask key=%s answered in %.1fs (%d chars)",
                    key[4:16], time.monotonic() - started, len(answer))
        return answer

    @mcp.tool(
        title="Reset conversation",
        description="Deletes the remote Agent Engine session for this conversation_id.",
    )
    async def reset_conversation(conversation_id: str) -> str:
        caller = _caller()
        key = session_key(caller, _check_conversation(conversation_id))
        await _remote(engine.reset(key), "reset")
        logger.info("tools/call reset caller=%s key=%s", caller, key[4:16])
        return "Conversation reset."

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return mcp


def create_http_app(env: Mapping[str, str], engine: RenewalEngine, host: str):
    mcp = build_server(env, engine)
    allowed = [h.strip() for h in env.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = None
    if allowed:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                             allowed_hosts=allowed)
    return mcp.streamable_http_app(stateless_http=True, host=host, transport_security=security)


if __name__ == "__main__":
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv(override=False)
    logging.basicConfig(level=logging.INFO)
    env = dict(os.environ)
    resource = env.get("GCP_AGENT_RESOURCE", "").strip()
    if not re.fullmatch(RESOURCE_RE, resource):
        raise SystemExit("GCP_AGENT_RESOURCE must be projects/P/locations/L/reasoningEngines/ID")
    host = env.get("HOST", "127.0.0.1")
    if env.get("MCP_LOCAL_DEVELOPMENT", "false").lower() == "true" and host not in {
        "127.0.0.1", "localhost", "::1"
    }:
        raise SystemExit("Unauthenticated local MCP must bind to loopback")
    uvicorn.run(create_http_app(env, create_engine(resource), host), host=host,
                port=int(env.get("PORT", "8080")), log_level="info")
