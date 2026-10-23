import json
from pathlib import Path
import time

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client

from openapi_tools import tools_from_spec
from server import build_server, create_http_app, thread_key

SPECS = Path(__file__).resolve().parent.parent / "openapi"
TENANT = "11111111-1111-4111-8111-111111111111"
AUD = "api://weather-mcp-test"


class FakeAgent:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def reply(self, thread, text):
        self.calls.append(("reply", thread, text))
        if self.fail:
            raise RuntimeError("azure openai 429 internal detail")
        return f"echo: {text}"

    async def reset(self, thread):
        self.calls.append(("reset", thread))


class FakeHttp:
    """Stands in for aiohttp.ClientSession and records the outgoing GET."""
    seen = []

    def __init__(self, timeout):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, params, allow_redirects):
        FakeHttp.seen.append((url, params, allow_redirects))
        return self

    status = 200

    async def text(self):
        return json.dumps({"results": [{"name": "Sevilla", "latitude": 37.38, "longitude": -5.97}]})


def test_tools_mirror_the_copilot_studio_connectors():
    tools = {t.name: t for p in sorted(SPECS.glob("*.json")) for t in tools_from_spec(p)}
    assert set(tools) == {"OpenMeteo-Geocoding", "OpenMeteo-Weather"}
    assert "'Search location'" in tools["OpenMeteo-Geocoding"].description
    assert "'Get weather forecast'" in tools["OpenMeteo-Weather"].description
    assert set(tools["OpenMeteo-Weather"].args) == {"latitude", "longitude", "current_weather"}


async def test_tool_calls_only_the_spec_host_with_query_params():
    FakeHttp.seen.clear()
    tool = tools_from_spec(SPECS / "OpenMeteo-Weather.json", session_factory=FakeHttp)[0]
    await tool.ainvoke({"latitude": 37.38, "longitude": -5.97, "current_weather": True})
    url, params, redirects = FakeHttp.seen[0]
    assert url == "https://api.open-meteo.com/v1/forecast"
    assert params == {"latitude": 37.38, "longitude": -5.97, "current_weather": "true"}
    assert redirects is False


def test_unsupported_specs_are_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"swagger": "2.0", "info": {"title": "x"}, "host": "h", "schemes": ["http"],
                               "paths": {}}))
    with pytest.raises(ValueError, match="https"):
        tools_from_spec(bad)


async def test_slow_weather_api_is_retried_then_reported_to_the_model():
    import asyncio

    attempts = []

    class Stalled(FakeHttp):
        def get(self, url, params, allow_redirects):
            attempts.append(url)
            raise asyncio.TimeoutError()

    tool = tools_from_spec(SPECS / "OpenMeteo-Geocoding.json", session_factory=Stalled)[0]
    out = json.loads(await tool.ainvoke({"name": "Sevilla"}))
    assert len(attempts) == 2
    assert out["error"] == "weather service unavailable" and "TimeoutError" in out["detail"]


async def test_mcp_tools_forward_to_the_langchain_agent():
    agent = FakeAgent()
    async with Client(build_server({"MCP_LOCAL_DEVELOPMENT": "true"}, agent)) as client:
        assert {t.name for t in (await client.list_tools()).tools} == {
            "ask_weather_agent", "reset_conversation"}
        res = await client.call_tool("ask_weather_agent",
                                     {"message": "¿Qué tiempo hace en Sevilla?", "conversation_id": "c1"})
        assert res.content[0].text == "echo: ¿Qué tiempo hace en Sevilla?"
        await client.call_tool("reset_conversation", {"conversation_id": "c1"})
    key = thread_key("local", "c1")
    assert agent.calls == [("reply", key, "¿Qué tiempo hace en Sevilla?"), ("reset", key)]


async def test_agent_failure_hides_internal_detail():
    async with Client(build_server({"MCP_LOCAL_DEVELOPMENT": "true"}, FakeAgent(fail=True))) as client:
        res = await client.call_tool("ask_weather_agent", {"message": "hola", "conversation_id": "c"})
    assert res.is_error and "reference" in res.content[0].text and "429" not in res.content[0].text


async def test_public_endpoint_requires_entra_bearer():
    env = {"ENTRA_TENANT_ID": TENANT, "MCP_AUDIENCES": AUD,
           "MCP_RESOURCE_URL": "https://ca-weather-agent-mcp.example.io/mcp"}
    app = create_http_app(env, FakeAgent(), host="0.0.0.0")
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://t") as http:
        assert (await http.get("/healthz")).status_code == 200
        resp = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                               headers={"Accept": "application/json, text/event-stream"})
        assert resp.status_code == 401


async def test_token_without_agent_invoke_role_is_rejected():
    from entra_auth import EntraTokenVerifier

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = EntraTokenVerifier(TENANT, {AUD}, "Agent.Invoke")

    class Signing:
        pass

    Signing.key = key.public_key()
    verifier.jwks.get_signing_key_from_jwt = lambda _t: Signing
    now = int(time.time())
    base = {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "tid": TENANT, "aud": AUD,
            "azp": "bridge", "iat": now, "exp": now + 600}
    ok = jwt.encode({**base, "roles": ["Agent.Invoke"]}, key, algorithm="RS256")
    no_role = jwt.encode(base, key, algorithm="RS256")
    assert (await verifier.verify_token(ok)).client_id == "bridge"
    assert await verifier.verify_token(no_role) is None
