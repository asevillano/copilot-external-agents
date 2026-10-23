"""Entra ID bearer validation for MCP callers (app tokens from managed identities)."""
import asyncio
import logging
from uuid import UUID

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken

logger = logging.getLogger(__name__)


class EntraTokenVerifier:
    def __init__(self, tenant_id: str, audiences: set[str], role: str,
                 allowed_clients: frozenset[str] = frozenset()):
        self.tenant_id = str(UUID(tenant_id))
        self.audiences = sorted(a for a in audiences if a)
        if not self.audiences:
            raise ValueError("At least one MCP audience is required")
        self.role = role
        self.allowed_clients = allowed_clients
        # managed identity tokens for a custom API are v1 unless the API asks for v2
        self.issuers = {
            f"https://login.microsoftonline.com/{self.tenant_id}/v2.0",
            f"https://sts.windows.net/{self.tenant_id}/",
        }
        self.jwks = PyJWKClient(
            f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys",
            cache_keys=True, lifespan=3600,
        )

    def _decode(self, token: str) -> dict:
        key = self.jwks.get_signing_key_from_jwt(token)
        return jwt.decode(
            token, key.key, algorithms=["RS256"], audience=self.audiences,
            options={"require": ["exp", "iat", "aud", "iss"]}, leeway=60,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await asyncio.to_thread(self._decode, token)
        except (jwt.PyJWTError, ValueError) as err:
            logger.warning("Rejected MCP bearer: %s", type(err).__name__)
            return None
        if claims.get("iss") not in self.issuers or claims.get("tid") != self.tenant_id:
            logger.warning("Rejected MCP bearer from another tenant or issuer")
            return None
        roles = claims.get("roles") or []
        if self.role not in roles:
            logger.warning("Rejected MCP bearer without role %s", self.role)
            return None
        client = claims.get("azp") or claims.get("appid") or ""
        if self.allowed_clients and client not in self.allowed_clients:
            logger.warning("Rejected MCP bearer from client %s", client)
            return None
        return AccessToken(
            token=token, client_id=client, scopes=list(roles), expires_at=claims["exp"],
            subject=claims.get("oid"), claims={"iss": claims["iss"], "tid": claims["tid"]},
        )
