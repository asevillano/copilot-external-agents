import asyncio
from unittest.mock import AsyncMock

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import mcp_engine
from mcp_engine import McpAgentEngine, RemoteAgentError, conversation_key


def fake_mcp(record):
    srv = MCPServer(name="fake-weather-mcp")

    @srv.tool()
    async def ask_weather_agent(message: str, conversation_id: str) -> str:
        record.append(("ask", conversation_id, message))
        if message == "boom":
            raise ToolError("The GCP agent could not complete the request (reference abc).")
        return "Contoso: riesgo ALTO"

    @srv.tool()
    async def reset_conversation(conversation_id: str) -> str:
        record.append(("reset", conversation_id))
        return "Conversation reset."

    return srv


@pytest.fixture
def engine(monkeypatch):
    record = []
    server = fake_mcp(record)
    # in-process transport instead of HTTP; the bearer header path is covered below
    monkeypatch.setattr(mcp_engine, "streamable_http_client", lambda url, http_client: server)
    eng = McpAgentEngine("https://mcp.internal.test/mcp", AsyncMock(return_value="mi-token"))
    eng.record = record
    return eng


async def test_reply_and_reset_go_through_mcp_tools(engine):
    assert await engine.reply("k1", "Analiza Contoso") == "Contoso: riesgo ALTO"
    await engine.reset("k1")
    assert engine.record == [("ask", "k1", "Analiza Contoso"), ("reset", "k1")]
    assert engine.token_provider.await_count == 2


async def test_tool_error_becomes_remote_agent_error(engine):
    with pytest.raises(RemoteAgentError, match="reference abc"):
        await engine.reply("k1", "boom")


async def test_bearer_from_managed_identity_is_sent(monkeypatch):
    seen = {}

    def capture(url, http_client):
        seen["url"] = url
        seen["auth"] = http_client.headers.get("authorization")
        return fake_mcp([])

    monkeypatch.setattr(mcp_engine, "streamable_http_client", capture)
    eng = McpAgentEngine("https://mcp.internal.test/mcp", AsyncMock(return_value="mi-token"))
    await eng.reply("k", "hola")
    assert seen == {"url": "https://mcp.internal.test/mcp", "auth": "Bearer mi-token"}


def test_conversation_key_is_stable_and_scoped():
    base = ["tenant", "msteams:COPILOT", "19:abc@thread.v2", "user"]
    assert conversation_key(*base) == conversation_key(*base)
    assert conversation_key(*base) != conversation_key("tenant", "msteams", *base[2:])
    with pytest.raises(ValueError):
        conversation_key("", *base[1:])


async def test_managed_identity_token_is_cached(monkeypatch):
    calls = []

    class Resp:
        status = 200

        async def json(self, content_type=None):
            calls.append(1)
            return {"access_token": "tok", "expires_on": "9999999999"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Session:
        def __init__(self, *a, **k):
            pass

        def get(self, url, headers, allow_redirects):
            assert headers["X-IDENTITY-HEADER"] == "hdr" and allow_redirects is False
            assert "resource=api%3A%2F%2Frenewal-risk-mcp" in url
            return Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(mcp_engine.aiohttp, "ClientSession", Session)
    provider = mcp_engine.ManagedIdentityToken(
        {"IDENTITY_ENDPOINT": "http://localhost:42356/msi/token", "IDENTITY_HEADER": "hdr"},
        "api://renewal-risk-mcp",
    )
    assert await asyncio.gather(provider(), provider()) == ["tok", "tok"]
    assert len(calls) == 1


def test_managed_identity_endpoint_must_be_local():
    with pytest.raises(ValueError):
        mcp_engine.ManagedIdentityToken(
            {"IDENTITY_ENDPOINT": "http://evil.example/token", "IDENTITY_HEADER": "h"}, "api://x")
