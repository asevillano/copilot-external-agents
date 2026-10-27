import argparse
import json
from pathlib import Path
import sys
import time

import google.auth
from google.auth.transport.requests import AuthorizedSession

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.local_env import load_env, require, setting  # noqa: E402

load_env()
NUMBER = setting("GCP_PROJECT_NUMBER")
RESOURCE = setting("GCP_AGENT_RESOURCE")
POOL = f"projects/{NUMBER}/locations/global/workloadIdentityPools/renewal-m365-azure-mcp"
PROVIDER_ID = setting("GCP_WIF_PROVIDER_ID", "azure-mcp")
TENANT = setting("ENTRA_TENANT_ID")
ROLE = "roles/aiplatform.user"
# aud observed on the ACA managed identity token requested for api://AzureADTokenExchange
TOKEN_EXCHANGE_AUDIENCE = "fb60f99c-7a34-4190-8149-302f77469936"


def provider_config(principal: str, issuer: str, audience: str) -> dict:
    if issuer != f"https://login.microsoftonline.com/{TENANT}/v2.0":
        raise ValueError("Unexpected hosting tenant issuer")
    if audience != TOKEN_EXCHANGE_AUDIENCE:
        raise ValueError("Unexpected Azure token exchange audience")
    from uuid import UUID
    principal = str(UUID(principal))
    return {
        "displayName": "Azure MCP adapter identity",
        "oidc": {"issuerUri": issuer, "allowedAudiences": [audience]},
        "attributeMapping": {"google.subject": "assertion.oid"},
        "attributeCondition": (
            f"assertion.tid == '{TENANT}' && assertion.oid == '{principal}'"
        ),
    }


def add_member(policy: dict, role: str, member: str) -> bool:
    for binding in policy.setdefault("bindings", []):
        if binding["role"] == role and not binding.get("condition"):
            if member in binding["members"]:
                return False
            binding["members"].append(member)
            return True
    policy["bindings"].append({"role": role, "members": [member]})
    return True


def deploy(principal: str, issuer: str, audience: str) -> dict:
    require("GCP_PROJECT_NUMBER", "GCP_AGENT_RESOURCE", "ENTRA_TENANT_ID")
    config = provider_config(principal, issuer, audience)
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    with AuthorizedSession(credentials) as http:
        def call(method, url, **kwargs):
            response = http.request(method, url, timeout=60, **kwargs)
            if not response.ok:
                raise RuntimeError(f"GCP {method} {url} -> {response.status_code}: {response.text[:500]}")
            result = response.json()
            if "/operations/" in result.get("name", ""):
                deadline = time.monotonic() + 120
                while not result.get("done"):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("GCP federation provisioning operation did not finish")
                    time.sleep(2)
                    response = http.get(
                        "https://iam.googleapis.com/v1/" + result["name"], timeout=30,
                    )
                    response.raise_for_status()
                    result = response.json()
                if result.get("error"):
                    raise RuntimeError(f"GCP provisioning failed: {result['error']}")
            return result

        iam = "https://iam.googleapis.com/v1/"
        pool_url = iam + POOL

        def listed(url, key):
            # GET on a missing pool/provider answers 403 "or it may not exist", so list instead
            resp = http.get(url, params={"showDeleted": "true"}, timeout=30)
            resp.raise_for_status()
            return {item["name"]: item for item in resp.json().get(key, [])}

        pools = listed(iam + f"projects/{NUMBER}/locations/global/workloadIdentityPools",
                       "workloadIdentityPools")
        if POOL not in pools:
            call("POST", iam + f"projects/{NUMBER}/locations/global/workloadIdentityPools",
                 params={"workloadIdentityPoolId": "renewal-m365-azure-mcp"},
                 json={"displayName": "Renewal M365 MCP federation"})
        elif pools[POOL].get("state") != "ACTIVE":
            raise RuntimeError("Workload identity pool exists but is not active")

        provider = POOL + "/providers/" + PROVIDER_ID
        providers = listed(pool_url + "/providers", "workloadIdentityPoolProviders")
        if provider not in providers:
            call("POST", pool_url + "/providers", params={"workloadIdentityPoolProviderId": PROVIDER_ID},
                 json=config)
        else:
            current = providers[provider]
            if current.get("state") != "ACTIVE" or any(current.get(key) != config[key] for key in (
                "oidc", "attributeMapping", "attributeCondition",
            )):
                raise RuntimeError("Existing federation provider differs; refusing to change it")

        # predefined role bound on the engine resource only; custom roles need iam.roles.create
        agent_url = "https://us-central1-aiplatform.googleapis.com/v1/" + RESOURCE
        policy = call("POST", agent_url + ":getIamPolicy",
                      json={"options": {"requestedPolicyVersion": 3}})
        member = f"principal://iam.googleapis.com/{POOL}/subject/{principal}"
        if add_member(policy, ROLE, member):
            call("POST", agent_url + ":setIamPolicy", json={"policy": policy})
        verified = call("POST", agent_url + ":getIamPolicy",
                        json={"options": {"requestedPolicyVersion": 3}})
        if not any(
            binding.get("role") == ROLE and member in binding.get("members", [])
            for binding in verified.get("bindings", [])
        ):
            raise RuntimeError("Resource-scoped invocation permission was not retained")
    return {"provider": provider, "principal": principal, "resource": RESOURCE,
            "role": ROLE, "scope": "Agent Engine resource only"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add narrowly scoped Azure MI federation to GCP")
    parser.add_argument("--principal", required=True)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        parser.error("--apply is required for cloud changes")
    state = deploy(args.principal, args.issuer, args.audience)
    (ROOT / "data" / "gcp-federation-mcp.json").write_text(json.dumps(state, indent=2), "utf-8")
    print(json.dumps(state, indent=2))
