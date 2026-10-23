"""MCP client used by the -mcp bridge instead of calling Agent Engine directly.

The bridge has no model or tool access of its own: it can only call the weather agent MCP,
authenticated with an Entra app token of its own managed identity.
"""
import asyncio
from collections.abc import Awaitable, Callable
import hashlib
from ipaddress import ip_address
import json
import logging
import os
import time
from typing import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

import aiohttp
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger(__name__)
ASK_TOOL = "ask_weather_agent"
RESET_TOOL = "reset_conversation"


def conversation_key(tenant: str, channel: str, conversation: str, user: str) -> str:
    """Opaque conversation_id sent to the MCP: one per tenant/channel/chat/user."""
    if not all((tenant, channel, conversation, user)):
        raise ValueError("Missing tenant, channel, conversation or sender identity")
    return hashlib.sha256(json.dumps([tenant, channel, conversation, user]).encode()).hexdigest()


class RemoteAgentError(RuntimeError):
    pass


class ManagedIdentityToken:
    """App token of the container's managed identity for the MCP API, cached until near expiry."""

    def __init__(self, env: Mapping[str, str], resource: str):
        endpoint = urlsplit(env.get("IDENTITY_ENDPOINT", ""))
        host = endpoint.hostname or ""
        try:
            local = host == "localhost" or ip_address(host).is_loopback or ip_address(host).is_link_local
        except ValueError:
            local = False
        if endpoint.scheme != "http" or not local or not env.get("IDENTITY_HEADER"):
            raise ValueError("Azure managed identity endpoint is not available")
        params = dict(parse_qsl(endpoint.query))
        params.update({"resource": resource, "api-version": "2019-08-01"})
        if env.get("AZURE_CLIENT_ID"):
            params["client_id"] = str(UUID(env["AZURE_CLIENT_ID"]))
        self.url = urlunsplit(endpoint._replace(query=urlencode(params)))
        self.header = env["IDENTITY_HEADER"]
        self._token = ""
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def __call__(self) -> str:
        async with self._lock:
            if self._token and time.time() < self._expires - 300:
                return self._token
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
                async with http.get(self.url, headers={"X-IDENTITY-HEADER": self.header},
                                    allow_redirects=False) as resp:
                    if resp.status != 200:
                        raise RemoteAgentError(f"Managed identity returned HTTP {resp.status}")
                    body = await resp.json(content_type=None)
            token = body.get("access_token") if isinstance(body, dict) else None
            if not isinstance(token, str) or not token:
                raise RemoteAgentError("Managed identity returned no access token")
            self._token, self._expires = token, float(body.get("expires_on") or time.time() + 600)
            return token


class McpAgentEngine:
    def __init__(self, url: str, token_provider: Callable[[], Awaitable[str]] | None,
                 timeout: float = 330.0):
        self.url = url
        self.token_provider = token_provider
        self.timeout = timeout

    async def _call(self, tool: str, args: dict, progress=None) -> str:
        headers = {}
        if self.token_provider is not None:
            headers["Authorization"] = f"Bearer {await self.token_provider()}"
        timeout = httpx2.Timeout(30, read=self.timeout)
        async with httpx2.AsyncClient(headers=headers, timeout=timeout) as http:
            transport = streamable_http_client(self.url, http_client=http)
            async with Client(transport, read_timeout_seconds=self.timeout) as client:
                result = await client.call_tool(tool, args, progress_callback=progress)
        text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text").strip()
        if result.is_error:
            # tool errors carry the server's message; never the caller's token or payload
            raise RemoteAgentError(f"MCP tool {tool} failed: {text[:300]}")
        if tool == ASK_TOOL and not text:
            raise RemoteAgentError("MCP tool returned no text")
        return text

    async def reply(self, key: str, text: str) -> str:
        async def progress(value, _total, message):
            logger.info("MCP progress key=%s %ss: %s", key[:12], value, message)
        return await self._call(ASK_TOOL, {"message": text, "conversation_id": key}, progress)

    async def reset(self, key: str) -> None:
        await self._call(RESET_TOOL, {"conversation_id": key})

    async def close(self) -> None:
        return None


def create_engine(url: str, token_resource: str, local: bool, timeout: float) -> McpAgentEngine:
    provider = None if local else ManagedIdentityToken(os.environ, token_resource)
    return McpAgentEngine(url, provider, timeout=timeout + 30)
