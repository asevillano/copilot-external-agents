import pytest

from config import CONNECTION, Settings

TENANT = "11111111-1111-4111-8111-111111111111"
CLIENT = "22222222-2222-4222-8222-222222222222"
USER = "33333333-3333-4333-8333-333333333333"
MCP_URL = "https://ca-weather-agent-mcp.example.eastus.azurecontainerapps.io/mcp"
LOCAL = {"LOCAL_DEVELOPMENT": "true", "MCP_SERVER_URL": "http://127.0.0.1:8765/mcp"}
PRODUCTION = {
    CONNECTION + "CLIENTID": CLIENT,
    CONNECTION + "CLIENTSECRET": "unit-test-value",
    CONNECTION + "TENANTID": TENANT,
    "MCP_SERVER_URL": MCP_URL,
    "MCP_TOKEN_RESOURCE": "api://weather-mcp-test",
    "M365_TENANT_ID": TENANT,
    "ENTRA_OAUTH_CONNECTION_NAME": "corporate-entra",
    "ENTRA_ALLOWED_USER_IDS": USER,
}


def test_production_requires_credentials():
    with pytest.raises(ValueError, match="CLIENTID"):
        Settings.from_env({})
    assert not Settings.from_env(PRODUCTION).local


def test_local_is_explicit_and_loopback_only():
    assert Settings.from_env(LOCAL).local
    with pytest.raises(ValueError, match="loopback"):
        Settings.from_env({**LOCAL, "HOST": "0.0.0.0"})


@pytest.mark.parametrize("key", [
    "M365_TENANT_ID", "ENTRA_OAUTH_CONNECTION_NAME", "ENTRA_ALLOWED_USER_IDS",
])
def test_production_requires_user_authorization(key):
    with pytest.raises(ValueError, match=key):
        Settings.from_env({**PRODUCTION, key: ""})


@pytest.mark.parametrize("value", ["*", "someone@example.com", ",,"] )
def test_pilot_requires_explicit_object_ids(value):
    with pytest.raises(ValueError):
        Settings.from_env({**PRODUCTION, "ENTRA_ALLOWED_USER_IDS": value})


def test_channel_and_copilot_tenants_are_separate():
    cfg = Settings.from_env({**PRODUCTION, "M365_TENANT_ID": CLIENT})
    assert cfg.tenant_id == TENANT
    assert cfg.m365_tenant_id == CLIENT
    assert cfg.allowed_user_ids == frozenset({USER})


@pytest.mark.parametrize("env", [
    {"LOCAL_DEVELOPMENT": "yes"},
    {"LOCAL_DEVELOPMENT": "true", "PORT": "0"},
    {"LOCAL_DEVELOPMENT": "true", "AGENT_TIMEOUT_SECONDS": "nan"},
])
def test_invalid_config(env):
    with pytest.raises(ValueError):
        Settings.from_env(env)


@pytest.mark.parametrize("url", [
    "", "ftp://host/mcp", "https://user:pw@host/mcp", "https://host/mcp?x=1",
])
def test_mcp_endpoint_must_be_a_plain_http_url(url):
    with pytest.raises(ValueError, match="MCP_SERVER_URL"):
        Settings.from_env({**LOCAL, "MCP_SERVER_URL": url})


def test_production_requires_https_mcp_and_entra_audience():
    with pytest.raises(ValueError, match="https"):
        Settings.from_env({**PRODUCTION, "MCP_SERVER_URL": "http://ca-mcp/mcp"})
    with pytest.raises(ValueError, match="MCP_TOKEN_RESOURCE"):
        Settings.from_env({**PRODUCTION, "MCP_TOKEN_RESOURCE": ""})
    assert Settings.from_env(PRODUCTION).mcp_token_resource == "api://weather-mcp-test"
