"""Show, from GCP's side, what M365 Copilot sent to the remote agent and what it answered.

Reads Agent Engine sessions/events with the caller's ADC. Read-only.
"""
import argparse
import json
from pathlib import Path
import sys

import google.auth
from google.auth.transport.requests import AuthorizedSession

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.local_env import load_env, setting  # noqa: E402

load_env()
RESOURCE = setting("GCP_AGENT_RESOURCE")


def _pages(http, url, key, params=None):
    params = dict(params or {}, pageSize=100)
    while True:
        resp = http.get(url, params=params, timeout=60)
        resp.raise_for_status()
        body = resp.json()
        yield from body.get(key, [])
        if not body.get("nextPageToken"):
            return
        params["pageToken"] = body["nextPageToken"]


def _short(value, n):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n] + " ..."


def show(resource: str, session: str | None, key: str | None, last: int, width: int) -> None:
    api = f"https://{resource.split('/')[3]}-aiplatform.googleapis.com/v1beta1/"
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    http = AuthorizedSession(creds)

    sessions = sorted(_pages(http, api + resource + "/sessions", "sessions"),
                      key=lambda s: s.get("updateTime", ""), reverse=True)
    if key:
        sessions = [s for s in sessions if s.get("userId", "").startswith(key)]
    if session:
        sessions = [s for s in sessions if s["name"].endswith("/" + session)]
    if not sessions:
        sys.exit("No matching sessions in Agent Engine")

    print(f"Agent Engine: {resource}")
    print(f"{'updated':27} {'session':22} user_id (bridge key)")
    for s in sessions[:last]:
        print(f"{s.get('updateTime', ''):27} {s['name'].rsplit('/', 1)[-1]:22} {s.get('userId', '')[:12]}...")

    target = sessions[0]
    print(f"\n=== Events of session {target['name'].rsplit('/', 1)[-1]} ===")
    for ev in _pages(http, api + target["name"] + "/events", "sessionEvents"):
        who = ev.get("author", "?")
        when = ev.get("timestamp", "")[11:19]
        for part in (ev.get("content") or {}).get("parts", []):
            if part.get("thought"):
                continue
            if "functionCall" in part:
                call = part["functionCall"]
                print(f"{when} {who:22} TOOL CALL  {call.get('name')}({_short(call.get('args', {}), width)})")
            elif "functionResponse" in part:
                res = part["functionResponse"]
                print(f"{when} {who:22} TOOL RESULT {res.get('name')}: {_short(res.get('response', {}), width)}")
            elif part.get("text"):
                label = "M365 USER  " if who == "user" else "AGENT REPLY"
                print(f"{when} {who:22} {label} {_short(part['text'], width)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", default=RESOURCE)
    parser.add_argument("--session", help="Agent Engine session ID (from the bridge log)")
    parser.add_argument("--key", help="first characters of the bridge key (from the bridge log)")
    parser.add_argument("--last", type=int, default=5, help="sessions to list")
    parser.add_argument("--width", type=int, default=160, help="characters per event")
    args = parser.parse_args()
    if not args.resource:
        parser.error("set GCP_AGENT_RESOURCE in .env or pass --resource")
    show(args.resource, args.session, args.key, args.last, args.width)
