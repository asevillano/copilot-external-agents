from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parent
CONNECTION = "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__"


@dataclass(frozen=True)
class Settings:
    local: bool
    host: str
    port: int
    tenant_id: str
    timeout: float
    mcp_url: str
    m365_tenant_id: str
    oauth_connection: str
    allowed_user_ids: frozenset[str]
    mcp_token_resource: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        mode = env.get("LOCAL_DEVELOPMENT", "false").lower()
        if mode not in {"true", "false"}:
            raise ValueError("LOCAL_DEVELOPMENT must be true or false")
        local = mode == "true"
        host = env.get("HOST", "127.0.0.1")
        if local and host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Anonymous local mode must bind to loopback")

        tenant = env.get(CONNECTION + "TENANTID", "").strip()
        if not local:
            for key in ("CLIENTID", "CLIENTSECRET", "TENANTID"):
                if not env.get(CONNECTION + key, "").strip():
                    raise ValueError(f"Missing {CONNECTION}{key}")
            UUID(env[CONNECTION + "CLIENTID"])
            tenant = str(UUID(tenant))

        m365_tenant = env.get("M365_TENANT_ID", "").strip()
        oauth_connection = env.get("ENTRA_OAUTH_CONNECTION_NAME", "").strip()
        allowed_users = env.get("ENTRA_ALLOWED_USER_IDS", "").strip()
        if not local:
            for key, value in (
                ("M365_TENANT_ID", m365_tenant),
                ("ENTRA_OAUTH_CONNECTION_NAME", oauth_connection),
                ("ENTRA_ALLOWED_USER_IDS", allowed_users),
            ):
                if not value:
                    raise ValueError(f"Missing {key}")
        m365_tenant = str(UUID(m365_tenant)) if m365_tenant else ""
        user_ids = frozenset(str(UUID(value.strip())) for value in allowed_users.split(",")
                             if value.strip())
        if not local and not user_ids:
            raise ValueError("ENTRA_ALLOWED_USER_IDS must contain explicit Entra object IDs")

        port = int(env.get("PORT", "3978"))
        timeout = float(env.get("AGENT_TIMEOUT_SECONDS", "180"))
        if not 1 <= port <= 65535 or not 0 < timeout <= 600:
            raise ValueError("PORT must be 1..65535 and AGENT_TIMEOUT_SECONDS must be >0..600")
        mcp_url = env.get("MCP_SERVER_URL", "").strip()
        parsed = urlsplit(mcp_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username \
                or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("MCP_SERVER_URL must be the renewal-risk MCP endpoint, e.g. https://host/mcp")
        if not local and parsed.scheme != "https":
            raise ValueError("MCP_SERVER_URL must use https outside local development")
        token_resource = env.get("MCP_TOKEN_RESOURCE", "").strip()
        if not local and not token_resource.startswith("api://"):
            raise ValueError("MCP_TOKEN_RESOURCE must be the MCP API application ID URI (api://...)")
        return cls(local, host, port, tenant, timeout, mcp_url,
                   m365_tenant, oauth_connection, user_ids, token_resource)
