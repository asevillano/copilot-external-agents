import argparse
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile

import google.auth
from google.auth.transport.requests import AuthorizedSession

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.local_env import load_env, require, setting  # noqa: E402

load_env()
PROJECT = setting("GCP_PROJECT_ID")
LOCATION = "us-central1"
DISPLAY_NAME = "renewal-risk-analyst-m365"
SERVICE_ACCOUNT_ID = "renewal-risk-m365-agent"
SOURCE_BUCKET = setting("GCP_DOCS_BUCKET")
# read by gcp_agent/renewal_risk_agent/agent.py inside Agent Engine
AGENT_ENV = ("GCP_PROJECT_NUMBER", "GCP_DOCS_BUCKET", "SALESFORCE_MCP_URL", "SERVICENOW_MCP_URL")
STATE_FILE = ROOT / "data" / "gcp-deployment.json"


def operation_schemas() -> list[dict]:
    schemas = []
    for name, mode, fields in (
        ("async_create_session", "async", ["user_id"]),
        ("async_delete_session", "async", ["user_id", "session_id"]),
        ("async_stream_query", "async_stream", ["user_id", "session_id", "message"]),
    ):
        schemas.append({
            "name": name,
            "api_mode": mode,
            "parameters": {
                "type": "object",
                "properties": {field: {"type": "string"} for field in fields},
                "required": fields,
            },
        })
    return schemas


@contextmanager
def staged_source():
    """Package only the original source, never local credentials, caches or pickles."""
    original = ROOT / "gcp_agent" / "renewal_risk_agent"
    with tempfile.TemporaryDirectory(prefix="renewal-m365-") as directory:
        stage = Path(directory)
        package = stage / "renewal_risk_agent"
        package.mkdir()
        hashes = {}
        for name in ("__init__.py", "agent.py"):
            source = original / name
            target = package / name
            shutil.copyfile(source, target)
            hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
            if hashlib.sha256(target.read_bytes()).hexdigest() != hashes[name]:
                raise RuntimeError(f"Source packaging changed {name}")
        shutil.copyfile(ROOT / "scripts" / "gcp_entrypoint.py", stage / "gcp_entrypoint.py")
        shutil.copyfile(ROOT / "requirements-deploy.txt", stage / "requirements.txt")
        yield stage, hashes


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def add_member(policy: dict, role: str, member: str) -> bool:
    for binding in policy.setdefault("bindings", []):
        if binding["role"] == role and not binding.get("condition"):
            if member in binding["members"]:
                return False
            binding["members"].append(member)
            return True
    policy["bindings"].append({"role": role, "members": [member]})
    return True


def provision_identity(session: AuthorizedSession, state: dict) -> str:
    email = f"{SERVICE_ACCOUNT_ID}@{PROJECT}.iam.gserviceaccount.com"
    account_url = f"https://iam.googleapis.com/v1/projects/{PROJECT}/serviceAccounts/{email}"
    response = session.get(account_url, timeout=30)
    if response.status_code == 404:
        response = session.post(
            f"https://iam.googleapis.com/v1/projects/{PROJECT}/serviceAccounts",
            json={
                "accountId": SERVICE_ACCOUNT_ID,
                "serviceAccount": {
                    "displayName": "Renewal Risk Analyst - M365 runtime",
                    "description": "Dedicated identity for the isolated renewal-risk-analyst-m365 deployment.",
                },
            },
            timeout=30,
        )
        response.raise_for_status()
        state["service_account"] = email
        save_state(state)
    else:
        response.raise_for_status()
        if state.get("service_account") != email:
            raise RuntimeError("Service account already exists but is not owned by this deployment")
    member = f"serviceAccount:{email}"
    policy_url = f"https://cloudresourcemanager.googleapis.com/v1/projects/{PROJECT}"
    response = session.post(
        policy_url + ":getIamPolicy", json={"options": {"requestedPolicyVersion": 3}}, timeout=30
    )
    response.raise_for_status()
    policy = response.json()
    changed = False
    for role in ("roles/aiplatform.user", "roles/discoveryengine.viewer", "roles/logging.logWriter"):
        changed = add_member(policy, role, member) or changed
    if changed:
        response = session.post(policy_url + ":setIamPolicy", json={"policy": policy}, timeout=30)
        response.raise_for_status()
    bucket_iam = f"https://storage.googleapis.com/storage/v1/b/{SOURCE_BUCKET}/iam"
    response = session.get(
        bucket_iam, params={"optionsRequestedPolicyVersion": 3}, timeout=30
    )
    response.raise_for_status()
    policy = response.json()
    if add_member(policy, "roles/storage.objectViewer", member):
        response = session.put(bucket_iam, json=policy, timeout=30)
        response.raise_for_status()
    state["granted_roles"] = [
        "roles/aiplatform.user", "roles/discoveryengine.viewer", "roles/logging.logWriter",
        f"roles/storage.objectViewer on {SOURCE_BUCKET}",
    ]
    save_state(state)
    return email


def deploy() -> str:
    import vertexai

    require("GCP_PROJECT_ID", *AGENT_ENV)
    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {
        "project": PROJECT, "location": LOCATION, "display_name": DISPLAY_NAME,
    }
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(credentials)
    endpoint = f"https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/{LOCATION}"
    response = session.get(
        endpoint + "/reasoningEngines", params={"pageSize": 100}, timeout=30
    )
    response.raise_for_status()
    listing = response.json()
    if listing.get("nextPageToken"):
        raise RuntimeError("More than 100 agents returned; paginate before checking deployment ownership")
    matching = [
        agent for agent in listing.get("reasoningEngines", [])
        if agent.get("displayName") == DISPLAY_NAME
    ]
    if matching:
        if len(matching) == 1 and state.get("resource") == matching[0]["name"]:
            print("Existing deployment owned by this integration:", state["resource"])
            return state["resource"]
        raise RuntimeError(
            "A deployment with this name already exists; inspect it instead of creating a duplicate: "
            + ", ".join(agent["name"] for agent in matching)
        )
    account = provision_identity(session, state)
    client = vertexai.Client(project=PROJECT, location=LOCATION, credentials=credentials)
    with staged_source() as (directory, hashes):
        state["source_sha256"] = hashes
        save_state(state)
        previous = Path.cwd()
        os.chdir(directory)
        try:
            remote = client.agent_engines.create(config={
                "source_packages": ["renewal_risk_agent", "gcp_entrypoint.py", "requirements.txt"],
                "entrypoint_module": "gcp_entrypoint",
                "entrypoint_object": "app",
                "requirements_file": "requirements.txt",
                "agent_framework": "google-adk",
                "python_version": "3.12",
                "class_methods": operation_schemas(),
                "display_name": DISPLAY_NAME,
                "description": "Original GCP Renewal Risk Analyst, exposed remotely for M365 Copilot.",
                "labels": {"integration": "m365-renewal-risk", "source": "existing-adk-agent"},
                "service_account": account,
                "env_vars": {name: setting(name) for name in AGENT_ENV},
                "min_instances": 0,
                "max_instances": 1,
                "resource_limits": {"cpu": "1", "memory": "2Gi"},
                "container_concurrency": 3,
            })
        finally:
            os.chdir(previous)
    if not remote.api_resource or not remote.api_resource.name:
        raise RuntimeError("Agent Engine creation returned no resource")
    state["resource"] = remote.api_resource.name
    save_state(state)
    print("Created isolated deployment:", state["resource"])
    return state["resource"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deploy the unchanged original renewal agent")
    parser.add_argument(
        "--apply", action="store_true",
        help="Create a new GCP identity, grants and Agent Engine; may incur cloud costs",
    )
    args = parser.parse_args()
    if not args.apply:
        parser.error("--apply is required; this command creates cloud resources")
    logging.basicConfig(level=logging.INFO)
    deploy()
