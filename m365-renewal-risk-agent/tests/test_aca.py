from pathlib import Path
import re

from scripts.deploy_aca import oauth_body, BOT_RESOURCE, BOT_TENANT, BOT_SUB, HOST_SUB, APP_RESOURCE
from scripts.deploy_federation import TENANT

ROOT = Path(__file__).resolve().parent.parent


def test_pilot_hosting_bot_and_federation_share_subscription_and_tenant():
    assert HOST_SUB == BOT_SUB
    assert BOT_TENANT == TENANT
    assert APP_RESOURCE.startswith(f"/subscriptions/{BOT_SUB}/")
    assert BOT_RESOURCE.startswith(f"/subscriptions/{BOT_SUB}/")


def test_no_environment_specific_values_in_templates():
    guid_default = re.compile(r"param \w+ string = '[^']*[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-")
    for name in ("containerapp.bicep", "aca-probe.bicep", "aca-foundation.bicep"):
        text = (ROOT / "infra" / name).read_text("utf-8")
        assert not guid_default.search(text), name
        assert not re.search(r"projects/\d+|reasoningEngines/\d+", text), name


def test_oauth_connection_is_tenant_specific_and_delegated():
    body = oauth_body("unit-test-secret")
    assert body["properties"]["scopes"] == "User.Read"
    assert body["properties"]["parameters"] == [{"key": "tenantId", "value": BOT_TENANT}]
    assert body["properties"]["clientSecret"] == "unit-test-secret"


def test_runtime_has_no_mounted_storage():
    for name in ("containerapp.bicep", "aca-probe.bicep", "aca-foundation.bicep"):
        text = (ROOT / "infra" / name).read_text("utf-8")
        assert "AzureFile" not in text and "SESSION_DB_PATH" not in text, name


def test_container_is_nonroot_and_copies_only_bridge_sources():
    dockerfile = (ROOT / "Dockerfile").read_text("utf-8")
    assert "USER 10001:10001" in dockerfile
    assert "COPY . " not in dockerfile
    assert "gcp_agent" not in dockerfile
    assert "COPY app.py config.py engine.py user_auth.py gcp_auth.py ./" in dockerfile
    assert ".env" not in dockerfile
