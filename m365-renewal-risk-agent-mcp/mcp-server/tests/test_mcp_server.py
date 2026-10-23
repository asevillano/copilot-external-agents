import asyncio
import time

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client

from entra_auth import EntraTokenVerifier
from server import build_server, create_http_app, session_key

TENANT = "11111111-1111-4111-8111-111111111111"
AUD = "api://renewal-risk-mcp-test"
BRIDGE = "22222222-2222-4222-8222-222222222222"


class FakeEngine:
    def __init__(self, delay=0.0):
        self.calls = []
        self.delay = delay

    async def reply(self, key, text):
        self.calls.append(("reply", key, text))
        await asyncio.sleep(self.delay)
        return f"riesgo ALTO para: {text}"

    async def reset(self, key):
        self.calls.append(("reset", key))


def local_server(engine):
    return build_server({"MCP_LOCAL_DEVELOPMENT": "true"}, engine)


async def test_tools_are_listed_and_forward_to_agent_engine():
    engine = FakeEngine()
    async with Client(local_server(engine)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert names == {"ask_renewal_risk_analyst", "reset_conversation"}
        res = await client.call_tool("ask_renewal_risk_analyst",
                                     {"message": " Analiza Contoso ", "conversation_id": "chat-1"})
        assert not res.is_error
        assert res.content[0].text == "riesgo ALTO para: Analiza Contoso"
        await client.call_tool("reset_conversation", {"conversation_id": "chat-1"})
    key = session_key("local", "chat-1")
    assert engine.calls == [("reply", key, "Analiza Contoso"), ("reset", key)]
    assert key.startswith("mcp:")


def test_session_keys_never_collide_between_callers():
    assert session_key("bridge", "c1") != session_key("langchain-agent", "c1")
    assert session_key("bridge", "c1") == session_key("bridge", "c1")


async def test_bad_input_is_a_tool_error_and_never_reaches_gcp():
    engine = FakeEngine()
    async with Client(local_server(engine)) as client:
        for args in ({"message": "", "conversation_id": "c"},
                     {"message": "x" * 16001, "conversation_id": "c"},
                     {"message": "hola", "conversation_id": " "}):
            assert (await client.call_tool("ask_renewal_risk_analyst", args)).is_error
    assert engine.calls == []


async def test_agent_engine_failure_is_reported_without_internal_detail():
    class Broken(FakeEngine):
        async def reply(self, key, text):
            raise RuntimeError("403 on projects/123456789012 secret-ish detail")

    async with Client(local_server(Broken())) as client:
        res = await client.call_tool("ask_renewal_risk_analyst",
                                     {"message": "hola", "conversation_id": "c"})
    assert res.is_error
    assert "reference" in res.content[0].text and "123456789012" not in res.content[0].text


async def test_long_calls_report_progress(monkeypatch):
    monkeypatch.setattr("server.PROGRESS_SECONDS", 0.05)
    seen = []

    async def progress(value, total, message):
        seen.append(message)

    async with Client(local_server(FakeEngine(delay=0.2))) as client:
        res = await client.call_tool("ask_renewal_risk_analyst",
                                     {"message": "hola", "conversation_id": "c"},
                                     progress_callback=progress)
    assert not res.is_error
    assert seen and "GCP" in seen[0]


# --- Entra validation -------------------------------------------------------

@pytest.fixture(scope="module")
def keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_token(key, **overrides):
    now = int(time.time())
    claims = {"iss": f"https://sts.windows.net/{TENANT}/", "tid": TENANT, "aud": AUD,
              "appid": BRIDGE, "oid": "33333333-3333-4333-8333-333333333333",
              "roles": ["Agent.Invoke"], "iat": now, "nbf": now, "exp": now + 600}
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def verifier(keypair, monkeypatch):
    v = EntraTokenVerifier(TENANT, {AUD}, "Agent.Invoke", frozenset({BRIDGE}))

    class Signing:
        key = keypair.public_key()

    monkeypatch.setattr(v.jwks, "get_signing_key_from_jwt", lambda _t: Signing)
    return v


async def test_managed_identity_app_token_is_accepted(keypair, verifier):
    tok = await verifier.verify_token(make_token(keypair))
    assert tok.client_id == BRIDGE and "Agent.Invoke" in tok.scopes


@pytest.mark.parametrize("overrides", [
    {"roles": []},
    {"aud": "api://something-else"},
    {"tid": "44444444-4444-4444-8444-444444444444"},
    {"iss": "https://login.microsoftonline.com/44444444-4444-4444-8444-444444444444/v2.0"},
    {"appid": "55555555-5555-4555-8555-555555555555"},
    {"exp": int(time.time()) - 3600},
])
async def test_tokens_outside_the_contract_are_rejected(keypair, verifier, overrides):
    assert await verifier.verify_token(make_token(keypair, **overrides)) is None


async def test_unsigned_garbage_is_rejected(verifier):
    assert await verifier.verify_token("not-a-jwt") is None


async def test_http_endpoint_requires_bearer_but_health_is_open():
    env = {"ENTRA_TENANT_ID": TENANT, "MCP_AUDIENCES": AUD,
           "MCP_RESOURCE_URL": "https://mcp.internal.example/mcp"}
    app = create_http_app(env, FakeEngine(), host="0.0.0.0")
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                  base_url="http://test") as http:
        assert (await http.get("/healthz")).status_code == 200
        resp = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                               headers={"Accept": "application/json, text/event-stream"})
        assert resp.status_code == 401
        assert "Bearer" in resp.headers.get("www-authenticate", "")
