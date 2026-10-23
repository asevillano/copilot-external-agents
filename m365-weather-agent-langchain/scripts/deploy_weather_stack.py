"""Deploy the weather version (LangChain agent + MCP, and its bridge) without touching the others.

First run (or --entra): interactive Entra sign-in in the browser to create the MCP API app
(role Agent.Invoke), assign that role to the bridge identity, and create the bot app and its
secret. The secret only lives in memory and in the Container App / Bot OAuth connection.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

import msal
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.local_env import load_env, require, setting  # noqa: E402

load_env()
SUB = setting("AZURE_SUBSCRIPTION_ID")
TENANT = setting("ENTRA_TENANT_ID")
RG = "rg-renewal-risk-m365-aca"
ADMIN_UPN = setting("ENTRA_ADMIN_UPN") or None
ARM = "https://management.azure.com"
GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_CLI_APP = "14d82eec-204b-4c2f-b7e8-296a70dab67e"  # Microsoft Graph Command Line Tools
GRAPH_SCOPES = ["Application.ReadWrite.All", "AppRoleAssignment.ReadWrite.All"]
MCP_API_NAME = "Weather Agent MCP - LangChain"
BOT_APP_NAME = "Weather Assistant M365 Bridge"
ROLE_VALUE = "Agent.Invoke"
OAUTH_NAME = setting("BOT_OAUTH_CONNECTION_NAME", "entra-weather")
REQUIRED = ("AZURE_SUBSCRIPTION_ID", "ENTRA_TENANT_ID", "PILOT_ALLOWED_USER_IDS")
STATE = ROOT / "data" / "deploy-weather.json"
RG_PATH = f"/subscriptions/{SUB}/resourceGroups/{RG}"


def save(state):
    STATE.write_text(json.dumps(state, indent=2), "utf-8")


def arm_token() -> str:
    az = shutil.which("az") or "az"
    out = subprocess.run([az, "account", "get-access-token", "--subscription", SUB,
                          "--query", "accessToken", "-o", "tsv"],
                         capture_output=True, text=True, check=False)
    if out.returncode or not out.stdout.strip():
        raise RuntimeError("Azure CLI ARM token failed: " + out.stderr.strip())
    return out.stdout.strip()


def call(method, url, token, ok=(200, 201, 202, 204), timeout=180, **kw):
    # ARM occasionally takes >60s on bot channel PUTs; PUT/GET here are idempotent
    for attempt in range(3):
        try:
            resp = requests.request(method, url, headers={"Authorization": f"Bearer {token}"},
                                    timeout=timeout, allow_redirects=False, **kw)
            break
        except requests.exceptions.ReadTimeout:
            if method not in {"PUT", "GET"} or attempt == 2:
                raise
            time.sleep(10)
    if resp.status_code not in ok:
        raise RuntimeError(f"{method} {url.split('?')[0]} -> {resp.status_code}: {resp.text[:400]}")
    return resp.json() if resp.content else {}


# --- Entra (Graph) --------------------------------------------------------------

def graph_token() -> str:
    app = msal.PublicClientApplication(GRAPH_CLI_APP,
                                       authority=f"https://login.microsoftonline.com/{TENANT}")
    res = app.acquire_token_interactive(GRAPH_SCOPES, login_hint=ADMIN_UPN,
                                        prompt="select_account")
    if "access_token" not in res:
        raise RuntimeError(f"Entra sign-in failed: {res.get('error_description', res)}")
    return res["access_token"]


def find_app(tok, name):
    found = call("GET", f"{GRAPH}/applications?$filter=displayName eq '{name}'", tok)["value"]
    if len(found) > 1:
        raise RuntimeError(f"More than one app named {name}; resolve manually")
    return found[0] if found else None


def ensure_sp(tok, app_id, **extra):
    found = call("GET", f"{GRAPH}/servicePrincipals?$filter=appId eq '{app_id}'", tok)["value"]
    if found:
        sp = found[0]
        if extra:
            call("PATCH", f"{GRAPH}/servicePrincipals/{sp['id']}", tok, json=extra)
        return sp
    for _ in range(10):  # the new application can take a few seconds to replicate
        try:
            return call("POST", f"{GRAPH}/servicePrincipals", tok, json={"appId": app_id, **extra})
        except RuntimeError as err:
            if "does not reference a valid application" not in str(err):
                raise
            time.sleep(5)
    raise RuntimeError("Service principal creation kept failing")


def setup_entra(state, bridge_principal: str) -> str:
    tok = graph_token()

    api = find_app(tok, MCP_API_NAME)
    if api is None:
        api = call("POST", f"{GRAPH}/applications", tok, json={
            "displayName": MCP_API_NAME, "signInAudience": "AzureADMyOrg",
            "api": {"requestedAccessTokenVersion": 2},
            "appRoles": [{
                "id": str(uuid.uuid4()), "allowedMemberTypes": ["Application"],
                "value": ROLE_VALUE, "displayName": "Invoke the weather agent via MCP",
                "description": "Call the weather agent MCP tools", "isEnabled": True,
            }],
        })
        call("PATCH", f"{GRAPH}/applications/{api['id']}", tok,
             json={"identifierUris": [f"api://{api['appId']}"]})
    role_id = next(r["id"] for r in api["appRoles"] if r["value"] == ROLE_VALUE)
    api_sp = ensure_sp(tok, api["appId"], appRoleAssignmentRequired=True)

    assigned = call("GET", f"{GRAPH}/servicePrincipals/{bridge_principal}/appRoleAssignments", tok)
    if not any(a["appRoleId"] == role_id and a["resourceId"] == api_sp["id"]
               for a in assigned["value"]):
        call("POST", f"{GRAPH}/servicePrincipals/{bridge_principal}/appRoleAssignments", tok,
             json={"principalId": bridge_principal, "resourceId": api_sp["id"], "appRoleId": role_id})

    bot = find_app(tok, BOT_APP_NAME)
    if bot is None:
        bot = call("POST", f"{GRAPH}/applications", tok, json={
            "displayName": BOT_APP_NAME, "signInAudience": "AzureADMyOrg",
            "web": {"redirectUris": ["https://token.botframework.com/.auth/web/redirect"]},
            "requiredResourceAccess": [{
                "resourceAppId": "00000003-0000-0000-c000-000000000000",
                "resourceAccess": [{"id": "e1fe6dd8-ba31-4d61-89e7-88639da4683d", "type": "Scope"}],
            }],
        })
    ensure_sp(tok, bot["appId"])

    expires = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
    # rotation: drop secrets this script issued before (e.g. lost in a failed run)
    current = call("GET", f"{GRAPH}/applications/{bot['id']}?$select=passwordCredentials", tok)
    for old in current.get("passwordCredentials", []):
        if old.get("displayName") == "weather-m365-bridge":
            call("POST", f"{GRAPH}/applications/{bot['id']}/removePassword", tok,
                 json={"keyId": old["keyId"]})
    cred = call("POST", f"{GRAPH}/applications/{bot['id']}/addPassword", tok, json={
        "passwordCredential": {"displayName": "weather-m365-bridge", "endDateTime": expires}})
    state.update({
        "mcpApiAppId": api["appId"], "mcpApiObjectId": api["id"], "mcpApiSpId": api_sp["id"],
        "agentInvokeRoleId": role_id, "botAppId": bot["appId"], "botAppObjectId": bot["id"],
        "botSecretKeyId": cred["keyId"], "botSecretExpires": expires,
    })
    save({**state, "status": "Entra ready; Azure deployment pending"})
    return cred["secretText"]


# --- Azure ----------------------------------------------------------------------

def deploy_template(tok, name, template_file, params):
    out = subprocess.run([shutil.which("az") or "az", "bicep", "build", "--file",
                          str(ROOT / "infra" / template_file), "--stdout"],
                         capture_output=True, text=True, check=False)
    if out.returncode:
        raise RuntimeError(out.stderr)
    url = f"{ARM}{RG_PATH}/providers/Microsoft.Resources/deployments/{name}?api-version=2022-09-01"
    body = {"properties": {
        "mode": "Incremental", "template": json.loads(out.stdout),
        "parameters": {k: {"value": v} for k, v in params.items()}}}
    try:
        call("PUT", url, tok, json=body, timeout=240)
    except requests.exceptions.ReadTimeout:
        # ARM may have accepted it anyway; the status poll below tells
        pass
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        resp = requests.get(url, headers={"Authorization": f"Bearer {tok}"}, timeout=60)
        if resp.status_code == 404:
            raise RuntimeError(f"Deployment {name} was not accepted by ARM; rerun")
        props = resp.json()["properties"]
        if props["provisioningState"] == "Succeeded":
            return props.get("outputs", {})
        if props["provisioningState"] in {"Failed", "Canceled"}:
            raise RuntimeError(f"Deployment {name} {props['provisioningState']}: "
                               f"{json.dumps(props.get('error', {}))[:600]}")
        time.sleep(10)
    raise TimeoutError(f"Deployment {name} still running")


def ensure_bot(tok, state, secret):
    name = state.setdefault("botName", setting("BOT_NAME", f"weather-m365-{state['botAppId'][:8]}"))
    bot_url = f"{ARM}{RG_PATH}/providers/Microsoft.BotService/botServices/{name}?api-version=2022-09-15"
    existing = requests.get(bot_url, headers={"Authorization": f"Bearer {tok}"}, timeout=60)
    if existing.status_code == 404:
        call("PUT", bot_url, tok, json={
            "location": "global", "sku": {"name": "F0"}, "kind": "azurebot",
            "tags": {"purpose": "m365-weather-agent-langchain"},
            "properties": {"displayName": "Weather Assistant (LangChain)", "endpoint": "",
                           "msaAppId": state["botAppId"], "msaAppType": "SingleTenant",
                           "msaAppTenantId": TENANT}})
    base = f"{ARM}{RG_PATH}/providers/Microsoft.BotService/botServices/{name}"
    for channel in ("MsTeamsChannel", "M365Extensions"):
        props = {"channelName": channel}
        if channel == "MsTeamsChannel":
            props["properties"] = {"isEnabled": True}
        call("PUT", f"{base}/channels/{channel}?api-version=2022-09-15", tok,
             json={"location": "global", "properties": props})
    if secret:
        call("PUT", f"{base}/connections/{OAUTH_NAME}?api-version=2022-09-15", tok, json={
            "location": "global", "properties": {
                "clientId": state["botAppId"], "clientSecret": secret,
                "serviceProviderId": "30dd229c-58e3-4a48-bdfd-91ec48eb906c",
                "scopes": "User.Read",
                "parameters": [{"key": "tenantId", "value": TENANT}]}})
    return bot_url


def existing_bridge_secret(tok):
    url = f"{ARM}{RG_PATH}/providers/Microsoft.App/containerApps/ca-weather-m365-bridge"
    if requests.get(url + "?api-version=2025-01-01", headers={"Authorization": f"Bearer {tok}"},
                    timeout=60).status_code == 404:
        return None
    secrets = call("POST", url + "/listSecrets?api-version=2025-01-01", tok, json={})["value"]
    found = [s["value"] for s in secrets if s["name"] == "bot-client-secret" and s.get("value")]
    return found[0] if len(found) == 1 else None


def deploy(mcp_image: str, bridge_image: str, entra: bool, min_replicas: int) -> dict:
    require(*REQUIRED)
    found = json.loads((ROOT / "data" / "foundation-weather.json").read_text("utf-8-sig"))
    f = {k: v["value"] for k, v in found.items()}
    for image in (mcp_image, bridge_image):
        if "@sha256:" not in image or not image.startswith(f["registryServer"] + "/"):
            raise ValueError("Use immutable image digests from the dedicated registry")
    state = json.loads(STATE.read_text("utf-8")) if STATE.exists() else {}

    tok = arm_token()
    secret = None
    if entra or "botAppId" not in state:
        secret = setup_entra(state, f["bridgePrincipalId"])
    else:
        secret = existing_bridge_secret(tok)
        if secret is None:
            raise RuntimeError("No bridge secret available; rerun with --entra to issue one")

    bot_url = ensure_bot(tok, state, secret)
    save({**state, "status": "Bot ready; apps pending"})

    mcp_out = deploy_template(tok, "weather-agent-mcp", "weather-agent.bicep", {
        "environmentId": f["agentEnvironmentId"], "defaultDomain": f["agentDefaultDomain"],
        "registryServer": f["registryServer"], "image": mcp_image,
        "identityId": f["agentIdentityId"], "identityClientId": f["agentClientId"],
        "mcpApiAppId": state["mcpApiAppId"], "allowedClientIds": f["bridgeClientId"],
        "tenantId": TENANT,
        "openAiEndpoint": f["openAiEndpoint"], "minReplicas": min_replicas})
    mcp_url = mcp_out["mcpUrl"]["value"]

    bridge_out = deploy_template(tok, "weather-m365-bridge", "bridge.bicep", {
        "environmentId": f["bridgeEnvironmentId"], "registryServer": f["registryServer"],
        "image": bridge_image, "identityId": f["bridgeIdentityId"],
        "identityClientId": f["bridgeClientId"], "botClientSecret": secret,
        "botClientId": state["botAppId"], "botTenantId": TENANT,
        "allowedUserIds": setting("PILOT_ALLOWED_USER_IDS"), "oauthConnectionName": OAUTH_NAME,
        "mcpServerUrl": mcp_url,
        "mcpApiAppId": state["mcpApiAppId"], "minReplicas": min_replicas})
    hostname = bridge_out["hostname"]["value"]

    bot = call("GET", bot_url, tok)
    bot["properties"]["endpoint"] = f"https://{hostname}/api/messages"
    call("PUT", bot_url, tok, json={k: bot[k] for k in ("location", "sku", "kind", "properties", "tags")
                                     if k in bot})
    state.update({"mcpServerUrl": mcp_url, "bridgeHostname": hostname,
                  "botEndpoint": bot["properties"]["endpoint"], "mcpImage": mcp_image,
                  "bridgeImage": bridge_image, "status": "Deployed; Copilot test pending"})
    save(state)
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-image", required=True)
    parser.add_argument("--bridge-image", required=True)
    parser.add_argument("--entra", action="store_true", help="(re)run the Entra setup and issue a new bot secret")
    parser.add_argument("--min-replicas", type=int, default=1, choices=[0, 1])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        parser.error("--apply is required for cloud changes")
    print(json.dumps(deploy(args.mcp_image, args.bridge_image, args.entra, args.min_replicas),
                     indent=2))
