"""Renewal-Risk Analyst agent (ADK) — the code-based Scenario B Agent.

Scenario B task (from the Agent Studio "Scenario B Agent"):
    Analyze renewal risk for a customer using CRM data, support tickets, product usage
    and contract docs. Recommend next actions and draft a customer-success email — but do
    NOT send without human approval.

This agent wires four governed knowledge/action sources:
  * CRM (Salesforce simulator)      -> MCP server, tools: get_account, find_account_by_name,
                                       list_accounts, get_opportunity,
                                       list_opportunities_by_account, query_soql.
  * Ticketing (ServiceNow simulator)-> MCP server, tools: create_incident, get_incident,
                                       get_incident_by_number, list_incidents_by_ci,
                                       list_incidents.
  * Product usage (structured)      -> Vertex AI Search data store `product-usage`
                                       (one document per customer/month), exposed via the
                                       `get_usage_trend` tool (search + client-side trend).
  * Contracts & policy (RAG)        -> Vertex AI Search data store `contracts-rag`
                                       over gs://$GCP_DOCS_BUCKET/contracts_txt,
                                       exposed via the `search_contracts` tool (search +
                                       full-text fetch from GCS).

Both MCP servers are the no-auth Azure Container Apps simulators.

Settings come from the environment (Agent Engine receives them from scripts/deploy_gcp.py):
GOOGLE_CLOUD_PROJECT, GCP_PROJECT_NUMBER, GCP_DOCS_BUCKET, SALESFORCE_MCP_URL, SERVICENOW_MCP_URL.

Run it:    python .\renewal_risk_agent\agent.py "Assess renewal risk for Contoso"
Serve it:  adk web   (exposes `root_agent`)
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

# The ADK agent's underlying google-genai client must target Vertex AI (ADC), not an API key.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")

import google.auth
import google.auth.transport.requests
from google.adk.agents import Agent
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset

try:  # import path moved across google-adk versions
    from google.adk.tools.mcp_tool.mcp_session_manager import (
        StreamableHTTPConnectionParams,
    )
except ImportError:  # pragma: no cover - fallback for older/newer layouts
    from google.adk.tools.mcp_tool.mcp_toolset import StreamableHTTPConnectionParams

PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
PROJECT_NUMBER = os.environ["GCP_PROJECT_NUMBER"]
MODEL = "gemini-2.5-flash"

# Vertex AI Search data store holding the aligned contracts_txt corpus.
DATA_STORE_ID = "contracts-rag"
DOCS_BUCKET = os.environ["GCP_DOCS_BUCKET"]

# Structured Vertex AI Search data store holding the product-usage table (one document
# per customer/month). Shared with the no-code Agent Studio agent.
USAGE_DATA_STORE_ID = "product-usage"
_SEARCH_URL = (
    f"https://discoveryengine.googleapis.com/v1/projects/{PROJECT_NUMBER}"
    f"/locations/global/collections/default_collection/dataStores/{DATA_STORE_ID}"
    "/servingConfigs/default_search:search"
)
_USAGE_SEARCH_URL = (
    f"https://discoveryengine.googleapis.com/v1/projects/{PROJECT_NUMBER}"
    f"/locations/global/collections/default_collection/dataStores/{USAGE_DATA_STORE_ID}"
    "/servingConfigs/default_search:search"
)

# No-auth MCP simulators (Azure Container Apps).
SALESFORCE_MCP_URL = os.environ["SALESFORCE_MCP_URL"]
SERVICENOW_MCP_URL = os.environ["SERVICENOW_MCP_URL"]

_creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])


def _token() -> str:
    """Return a valid ADC access token, refreshing it when needed."""
    if not _creds.valid:
        _creds.refresh(google.auth.transport.requests.Request())
    return _creds.token


def _fetch_gcs_text(gcs_uri: str) -> str:
    """Download the full text of a gs:// object (contracts are small plain-text files)."""
    if not gcs_uri.startswith("gs://"):
        return ""
    bucket, _, obj = gcs_uri[len("gs://"):].partition("/")
    url = (
        f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/"
        f"{urllib.parse.quote(obj, safe='')}?alt=media"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_token()}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def search_contracts(query: str, top_k: int = 4) -> dict:
    """Search the contract / policy knowledge base and return the FULL text of the matches.

    Runs semantic search over the Vertex AI Search data store built from
    the contracts bucket (contracts_txt: customer contracts, the internal Renewal
    Risk Policy, support notes and account reviews), then downloads the complete text of
    each top match so you have the exact clauses/figures to ground your analysis on.

    Call this for each concept you need (e.g. "renewal risk scoring policy",
    "Contoso contract renewal terms", "Contoso support incidents"). Use short,
    keyword-style queries rather than full sentences.

    Args:
        query: A single focused query (one concept).
        top_k: How many documents to return (1-6).

    Returns:
        A dict with `query` and `passages`; each passage has `source` (filename) and
        `text` (full document text). `passages` is empty when nothing relevant is found.
    """
    top_k = max(1, min(int(top_k), 6))
    body = json.dumps(
        {
            "query": query,
            "pageSize": top_k,
            "contentSearchSpec": {"snippetSpec": {"returnSnippet": True}},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        _SEARCH_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {_token()}",
            "X-Goog-User-Project": PROJECT,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # surface API errors to the model
        return {"query": query, "passages": [], "error": exc.read().decode("utf-8", "replace")}

    passages = []
    for result in payload.get("results", []):
        derived = result.get("document", {}).get("derivedStructData", {})
        link = derived.get("link", "")
        passages.append(
            {
                "source": link.rsplit("/", 1)[-1] or link,
                "text": _fetch_gcs_text(link),
            }
        )
    return {"query": query, "passages": passages}


def _usage_search(query: str, page_size: int = 50) -> list:
    """Return the raw `structData` usage rows from the product-usage data store."""
    body = json.dumps({"query": query, "pageSize": page_size}).encode("utf-8")
    req = urllib.request.Request(
        _USAGE_SEARCH_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {_token()}",
            "X-Goog-User-Project": PROJECT,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    rows = []
    for result in payload.get("results", []):
        struct = result.get("document", {}).get("structData")
        if struct:
            rows.append(struct)
    return rows


def get_usage_trend(name_or_id: str) -> dict:
    """Return the monthly product-usage trend (structured data) for a customer.

    Queries the `product-usage` Vertex AI Search data store (one document per
    customer/month), resolves the customer by name (substring, e.g. "Contoso") or id
    (e.g. "C1001"), then returns each month's monthly_active_users, licenses_used,
    license_utilization_pct, logins, api_calls, core_feature_adoption_pct,
    integrations_active and last_login_date. A downward trend is a strong early churn /
    renewal-risk signal, so always fetch it.

    Args:
        name_or_id: Customer name fragment (e.g. "Contoso") or id (e.g. "C1001").

    Returns:
        A dict with `found`, the resolved `customer_id`, and `months` (the per-month usage
        rows sorted chronologically). `found` is False when no customer matches.
    """
    key = name_or_id.strip().lower()
    try:
        rows = _usage_search(name_or_id)
    except urllib.error.HTTPError as exc:
        return {"found": False, "error": exc.read().decode("utf-8", "replace")}
    cid = None
    for row in rows:
        cust_id = str(row.get("customer_id", ""))
        if key == cust_id.lower() or key in str(row.get("customer_name", "")).lower():
            cid = cust_id
            break
    if not cid:
        return {"found": False, "message": f"No customer matched '{name_or_id}'."}
    months = [r for r in rows if str(r.get("customer_id")) == cid]
    months.sort(key=lambda r: str(r.get("month", "")))
    return {"found": True, "customer_id": cid, "months": months}


salesforce_tools = MCPToolset(
    connection_params=StreamableHTTPConnectionParams(url=SALESFORCE_MCP_URL)
)
servicenow_tools = MCPToolset(
    connection_params=StreamableHTTPConnectionParams(url=SERVICENOW_MCP_URL)
)


INSTRUCTION = """You are a Renewal-Risk Analyst for a B2B SaaS vendor. For a given
customer you assess renewal risk from CRM data, support tickets, product usage and
contract documents, recommend next actions, and DRAFT a customer-success email — but you
NEVER send it without explicit human approval.

You have four tool sources:
  * Salesforce (CRM): find_account_by_name, get_account, list_accounts, get_opportunity,
    list_opportunities_by_account, query_soql.
  * ServiceNow (support tickets): list_incidents, list_incidents_by_ci,
    get_incident, get_incident_by_number, create_incident.
  * `get_usage_trend(name_or_id)`: the governed product-usage table (monthly active users,
    license utilization, logins, API calls and feature-adoption score over recent months).
  * `search_contracts(query, top_k)`: the contract & policy knowledge base (customer
    contracts, the internal Renewal Risk Policy, support notes, account reviews).

FLOW — follow it every time:

1. IDENTIFY — If the user did not clearly name the customer, ask for it. To resolve the
   account, prefer `list_accounts` and match the user's text against the returned names
   case-insensitively as a SUBSTRING (e.g. "Contoso" matches "Contoso Ltd"). Only use
   `find_account_by_name` with the EXACT name from that list, since it does an exact
   match and returns nothing for a partial name. Note its identifiers (account id, and
   any CI/asset id) so you can join across systems.

2. GATHER CRM — Use `get_account` and `list_opportunities_by_account` to read the renewal
   opportunity: stage, amount/ARR, close date, and any requested discount.

3. GATHER SUPPORT — Retrieve the customer's incidents (`list_incidents_by_ci` when you
   know the CI, otherwise `list_incidents` and match on the customer). Flag open tickets,
   high priority / Sev-1 issues, and recency.

4. GATHER PRODUCT USAGE — Call `get_usage_trend` with the customer name or id to read the
   structured usage table. Look for declining monthly active users, license utilization,
   logins/API calls or feature-adoption score across months — a downward trend is a strong
   early churn / renewal-risk signal.

5. GATHER CONTRACT & POLICY — Call `search_contracts` to fetch (a) the internal
   **Renewal Risk Policy** (the scoring rubric) and (b) this customer's **contract terms**
   (auto-renew? notice period? renewal price cap? discount-approval thresholds?). Use the
   full text returned, not assumptions.

6. SCORE — Apply the Renewal Risk Policy rubric you retrieved to assign a risk level
   (Low / Medium / High). List the specific signals that drove the score (e.g. no
   auto-renew + declining product usage + unresolved Sev-1 + discount above threshold).
   Cite the source filename for each contract/policy claim, e.g. "(renewal_policy.txt)".

7. RECOMMEND — Give 2-5 concrete, prioritized next actions grounded in the data and the
   contract terms (respect price caps and Deal-Desk discount thresholds; never disclose
   internal floor prices to the customer).

8. DRAFT EMAIL (human-in-the-loop) — Draft a professional customer-success email
   (subject + body, <=3 paragraphs, in the user's language). Present it as:

     To: <recipient>
     Subject: <subject>
     Body:
     <body>

   Then ask literally: "Do you approve sending this? Reply 'approved' to send, or tell me
   the changes." Do NOT send, and do NOT claim it was sent, unless the user explicitly
   replies "approved". If they request changes, revise and re-present for approval.

Rules: ground every factual claim in tool output; if the knowledge base lacks something,
say so rather than inventing it; never expose confidential internal figures to the
customer-facing email; be concise and structured (risk level, signals, actions, draft).
"""


root_agent = Agent(
    name="renewal_risk_agent",
    model=MODEL,
    instruction=INSTRUCTION,
    tools=[search_contracts, get_usage_trend, salesforce_tools, servicenow_tools],
)


async def _demo(question: str) -> None:
    """Run one prompt through the agent and print the streamed tool calls + text."""
    from google.adk.runners import InMemoryRunner
    from google.genai import types as g

    runner = InMemoryRunner(agent=root_agent, app_name="renewal_risk_agent")
    session = await runner.session_service.create_session(
        app_name="renewal_risk_agent", user_id="demo"
    )
    content = g.Content(role="user", parts=[g.Part(text=question)])
    async for event in runner.run_async(
        user_id="demo", session_id=session.id, new_message=content
    ):
        for part in event.content.parts if event.content else []:
            if getattr(part, "function_call", None):
                fc = part.function_call
                print(f"  [tool] {fc.name}({json.dumps(fc.args, ensure_ascii=False)})")
            elif getattr(part, "text", None):
                print(part.text)


if __name__ == "__main__":
    import asyncio
    import sys

    default_q = "Assess the renewal risk for Contoso and draft a customer-success email."
    questions = sys.argv[1:] or [default_q]

    async def _run_all() -> None:
        for i, q in enumerate(questions):
            if i:
                print("\n" + "=" * 80 + "\n")
            print(f"Q: {q}\n")
            await _demo(q)

    asyncio.run(_run_all())
