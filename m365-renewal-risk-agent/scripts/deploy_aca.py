import argparse
from getpass import getpass
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.local_env import load_env, require, setting  # noqa: E402

load_env()
BOT_SUB = setting("AZURE_SUBSCRIPTION_ID")
HOST_SUB = BOT_SUB
BOT_TENANT = setting("ENTRA_TENANT_ID")
APP_ID = setting("BOT_APP_ID")
BOT_NAME = setting("BOT_NAME", f"renewal-risk-m365-{APP_ID[:8]}")
OAUTH_CONNECTION = setting("BOT_OAUTH_CONNECTION_NAME", "entra-user-read")
WIF_PROVIDER = setting("GCP_WIF_PROVIDER_ID", "azure")
APP_RESOURCE = (
    f"/subscriptions/{HOST_SUB}/resourceGroups/rg-renewal-risk-m365-aca/"
    "providers/Microsoft.App/containerApps/ca-renewal-risk-m365"
)
BOT_RESOURCE = (
    f"/subscriptions/{BOT_SUB}/resourceGroups/rg-renewal-risk-m365-test/"
    f"providers/Microsoft.BotService/botServices/{BOT_NAME}"
)
REQUIRED = ("AZURE_SUBSCRIPTION_ID", "ENTRA_TENANT_ID", "BOT_APP_ID", "PILOT_ALLOWED_USER_IDS",
            "GCP_PROJECT_NUMBER", "GCP_AGENT_RESOURCE")
ARM = "https://management.azure.com"
STATE_PATH = ROOT / "data" / "aca-deployment.json"
FOUNDATION_PATH = ROOT / "data" / "aca-bridge-foundation.json"


def azure_token(*args: str) -> str:
    az = shutil.which("az")
    if az is None:
        raise RuntimeError("Azure CLI is required")
    result = subprocess.run(
        [az, "account", "get-access-token", *args, "--query", "accessToken", "--output", "tsv"],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if result.returncode:
        raise RuntimeError("Azure authentication failed: " + result.stderr.strip())
    if not result.stdout.strip():
        raise RuntimeError("Azure CLI returned no token")
    return result.stdout.strip()


def request(method: str, url: str, token: str, **kwargs):
    response = requests.request(
        method, url, headers={"Authorization": f"Bearer {token}"},
        timeout=60, allow_redirects=False, **kwargs,
    )
    if response.status_code != 404:
        response.raise_for_status()
    return response


def oauth_body(secret: str) -> dict:
    return {"location": "global", "properties": {
        "clientId": APP_ID, "clientSecret": secret,
        "serviceProviderId": "30dd229c-58e3-4a48-bdfd-91ec48eb906c",
        "scopes": "User.Read", "parameters": [{"key": "tenantId", "value": BOT_TENANT}],
    }}


def save(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), "utf-8")


def deploy(image: str, rotate: bool = False) -> dict:
    require(*REQUIRED)
    foundation = json.loads(FOUNDATION_PATH.read_text("utf-8-sig"))
    template = json.loads((ROOT / "data" / "containerapp.compiled.json").read_text("utf-8-sig"))
    if not image.startswith(foundation["registryServer"]["value"] + "/renewal-risk-m365@sha256:"):
        raise ValueError("Use the immutable bridge image digest from the dedicated registry")
    token = azure_token("--subscription", HOST_SUB)

    previous = request("GET", ARM + APP_RESOURCE + "?api-version=2025-01-01", token)
    state = {"botResource": BOT_RESOURCE, "appResource": APP_RESOURCE, "image": image}
    runtime = previous.status_code == 200 and previous.json().get("tags", {}).get("phase") == "runtime"
    secret = None
    if runtime and not rotate:
        secrets = request("POST", ARM + APP_RESOURCE + "/listSecrets?api-version=2025-01-01",
                          token, json={})
        secrets.raise_for_status()
        candidates = [item["value"] for item in secrets.json()["value"]
                      if item["name"] == "bot-client-secret" and item.get("value")]
        if len(candidates) != 1:
            raise RuntimeError("Existing app has no unique bot secret; rerun with --rotate")
        secret = candidates[0]
    else:
        # created by the admin in the Entra portal; Graph from Azure CLI is blocked by CAE here
        secret = getpass(f"Client secret for app {APP_ID} (input hidden): ").strip()
        if not secret:
            raise ValueError("Empty client secret")

    connection_url = ARM + BOT_RESOURCE + f"/connections/{OAUTH_CONNECTION}?api-version=2022-09-15"
    connection = request("PUT", connection_url, token, json=oauth_body(secret))
    connection.raise_for_status()
    save({**state, "status": "OAuth configured; hosting pending"})
    values = {
        "environmentId": foundation["environmentId"]["value"],
        "registryServer": foundation["registryServer"]["value"],
        "image": image, "identityId": foundation["identityId"]["value"],
        "identityClientId": foundation["clientId"]["value"],
        "botClientSecret": secret,
        "botClientId": APP_ID, "botTenantId": BOT_TENANT,
        "allowedUserIds": setting("PILOT_ALLOWED_USER_IDS"),
        "oauthConnectionName": OAUTH_CONNECTION,
        "gcpAgentResource": setting("GCP_AGENT_RESOURCE"),
        "gcpWorkloadIdentityAudience": (
            f"//iam.googleapis.com/projects/{setting('GCP_PROJECT_NUMBER')}/locations/global/"
            f"workloadIdentityPools/renewal-m365-azure/providers/{WIF_PROVIDER}"
        ),
        "minReplicas": 1,
    }
    deployment_url = (
        ARM + f"/subscriptions/{HOST_SUB}/resourceGroups/rg-renewal-risk-m365-aca/"
        "providers/Microsoft.Resources/deployments/renewal-aca-app?api-version=2022-09-01"
    )
    deployment = request("PUT", deployment_url, token, json={"properties": {
        "mode": "Incremental", "template": template,
        "parameters": {key: {"value": value} for key, value in values.items()},
    }})
    deployment.raise_for_status()
    deadline = time.monotonic() + 900
    while True:
        status = request("GET", deployment_url, token)
        status.raise_for_status()
        properties = status.json()["properties"]
        phase = properties["provisioningState"]
        if phase == "Succeeded":
            break
        if phase in {"Failed", "Canceled"}:
            save({**state, "status": f"Azure deployment {phase}"})
            raise RuntimeError(f"Azure deployment {phase}; inspect deployment operations in Azure")
        if time.monotonic() >= deadline:
            raise TimeoutError("Azure deployment still running; inspect before retrying")
        time.sleep(10)
    hostname = properties["outputs"]["hostname"]["value"]
    bot_url = ARM + BOT_RESOURCE + "?api-version=2022-09-15"
    bot = request("GET", bot_url, token)
    bot.raise_for_status()
    bot = bot.json()
    bot["properties"]["endpoint"] = f"https://{hostname}/api/messages"
    request("PUT", bot_url, token, json={key: bot[key] for key in (
        "location", "sku", "kind", "properties", "tags") if key in bot}).raise_for_status()
    state.update({"hostname": hostname, "botEndpoint": bot["properties"]["endpoint"],
                  "status": "Hosted and bot endpoint configured; Copilot test pending"})
    save(state)
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deploy the dedicated bridge without secret files")
    parser.add_argument("--image", required=True)
    parser.add_argument("--rotate", action="store_true", help="prompt for a new portal-created secret")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        parser.error("--apply is required for cloud changes")
    print(json.dumps(deploy(args.image, args.rotate), indent=2))
