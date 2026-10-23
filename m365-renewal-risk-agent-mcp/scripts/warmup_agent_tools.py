"""Wake the GCP agent's own tool servers before a demo (CRM and ServiceNow simulators).

Both MCP servers and the simulators behind them scale to zero. If the agent runs while
they are cold, it either loads no CRM tools ("Tool 'list_accounts' not found") or gets
"simulator_unreachable". Read-only list calls only; nothing is created or changed.
"""
import asyncio
from pathlib import Path
import sys
import time

from mcp import Client

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.local_env import load_env, require, setting  # noqa: E402

load_env()
SERVERS = {
    "CRM (Salesforce)": (setting("SALESFORCE_MCP_URL"), "list_accounts"),
    "ServiceNow": (setting("SERVICENOW_MCP_URL"), "list_incidents"),
}


async def warm(name: str, url: str, tool: str) -> bool:
    async with Client(url, read_timeout_seconds=120) as client:
        for attempt in range(1, 6):
            started = time.monotonic()
            res = await client.call_tool(tool, {})
            text = res.content[0].text if res.content else ""
            if not res.is_error and "simulator_unreachable" not in text:
                print(f"{name}: OK in {time.monotonic() - started:.1f}s (attempt {attempt})")
                return True
            print(f"{name}: still cold (attempt {attempt}), retrying...")
            await asyncio.sleep(10)
    print(f"{name}: NOT ready")
    return False


async def main() -> int:
    require("SALESFORCE_MCP_URL", "SERVICENOW_MCP_URL")
    results = await asyncio.gather(*(warm(n, u, t) for n, (u, t) in SERVERS.items()))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
