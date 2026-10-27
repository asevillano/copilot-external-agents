from dataclasses import dataclass
from pathlib import Path
import re
from typing import Mapping
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
    resource: str
    m365_tenant_id: str
    oauth_connection: str
    allowed_user_ids: frozenset[str]

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
        resource = env.get("GCP_AGENT_RESOURCE", "").strip()
        if not re.fullmatch(
            r"projects/[a-zA-Z0-9-]+/locations/[a-z][a-z0-9-]+/reasoningEngines/[0-9]+",
            resource,
        ):
            raise ValueError(
                "GCP_AGENT_RESOURCE must identify the deployed renewal agent: "
                "projects/PROJECT/locations/REGION/reasoningEngines/ID"
            )
        return cls(local, host, port, tenant, timeout, resource,
                   m365_tenant, oauth_connection, user_ids)
