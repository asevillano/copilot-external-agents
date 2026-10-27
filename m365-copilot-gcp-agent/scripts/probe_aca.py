import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def identity_claims() -> dict:
    from google.auth.transport.requests import Request
    from gcp_auth import AzureSubjectToken

    token = AzureSubjectToken(os.environ, os.environ["GCP_AZURE_TOKEN_RESOURCE"]).get_subject_token(
        None, Request()
    )
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    # Metadata for configuring trust, not a local authorization decision.
    return {key: claims[key] for key in ("iss", "aud", "oid", "tid")}


async def backend_probe() -> dict:
    from engine import conversation_key, create_engine

    engine = create_engine(os.environ["GCP_AGENT_RESOURCE"])
    key = conversation_key(os.environ["M365_TENANT_ID"], "deployment-probe", uuid4().hex, "operator")
    try:
        answer = await engine.reply(
            key, "Responde solo con la palabra conectado. No invoques herramientas."
        )
        if "conectado" not in answer.casefold():
            raise RuntimeError("The remote agent did not return the requested smoke response")
        return {"backend_query": True}
    finally:
        try:
            await engine.reset(key)
        finally:
            await engine.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Operator-only deployment checks; never prints tokens")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--identity-claims", action="store_true")
    modes.add_argument("--backend", action="store_true")
    parser.add_argument("--hold-seconds", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.hold_seconds <= 3600:
        parser.error("--hold-seconds must be between 0 and 3600")
    result = identity_claims() if args.identity_claims else asyncio.run(backend_probe())
    print(json.dumps(result), flush=True)
    if args.hold_seconds:
        time.sleep(args.hold_seconds)
