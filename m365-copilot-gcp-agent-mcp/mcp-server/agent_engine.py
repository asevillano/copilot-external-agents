import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import codecs
import hashlib
import json
import logging
from weakref import WeakValueDictionary

import aiohttp
from google.auth.transport.requests import Request

from gcp_auth import gcp_credentials

logger = logging.getLogger(__name__)


def conversation_key(tenant: str, channel: str, conversation: str, user: str) -> str:
    """Remote Agent Engine user_id: one per tenant/channel/chat/user, so lookups never mix chats."""
    if not all((tenant, channel, conversation, user)):
        raise ValueError("Missing tenant, channel, conversation or sender identity")
    return hashlib.sha256(json.dumps([tenant, channel, conversation, user]).encode()).hexdigest()


class RemoteAgentError(RuntimeError):
    pass


async def stream_events(
    content: aiohttp.StreamReader, content_type: str = "text/event-stream",
) -> AsyncIterator[dict]:
    """Decode SSE frames independently of HTTP chunk boundaries."""
    if content_type == "application/json":
        async for event in json_stream_events(content):
            yield event
        return
    data = []
    async for raw in content:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif not line and data:
            payload = "\n".join(data)
            data = []
            if payload == "[DONE]":
                return
            event = json.loads(payload)
            if not isinstance(event, dict):
                raise RemoteAgentError("GCP returned a non-object event")
            yield event
        elif line and not line.startswith((":", "event:", "id:", "retry:")):
            raise RemoteAgentError("GCP returned an invalid SSE stream")
    if data:
        payload = "\n".join(data)
        if payload != "[DONE]":
            event = json.loads(payload)
            if not isinstance(event, dict):
                raise RemoteAgentError("GCP returned a non-object event")
            yield event


async def json_stream_events(content: aiohttp.StreamReader) -> AsyncIterator[dict]:
    decoder = json.JSONDecoder()
    utf8 = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    async for chunk in content.iter_chunked(65536):
        buffer += utf8.decode(chunk)
        while buffer.strip():
            buffer = buffer.lstrip()
            try:
                event, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                break
            if not isinstance(event, dict):
                raise RemoteAgentError("GCP returned a non-object JSON event")
            yield event
            buffer = buffer[end:]
        if len(buffer) > 1024 * 1024:
            raise RemoteAgentError("GCP JSON event exceeded the transport limit")
    buffer += utf8.decode(b"", final=True)
    if buffer.strip():
        raise RemoteAgentError("GCP returned an incomplete or malformed JSON stream")


class RenewalEngine:
    """Transport bridge only: no prompts, model calls, local ADK runner or tools."""

    def __init__(self, resource: str, token_provider: Callable[[], Awaitable[str]]):
        self.resource = resource
        location = resource.split("/")[3]
        self.url = f"https://{location}-aiplatform.googleapis.com/v1/{resource}"
        self.token_provider = token_provider
        self._http: aiohttp.ClientSession | None = None
        self._init_lock = asyncio.Lock()
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        # cache only; Agent Engine is the source of truth after a restart
        self._known: dict[str, str] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def _connect(self) -> None:
        async with self._init_lock:
            if self._http is None:
                self._http = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=600, sock_connect=15, sock_read=180),
                    read_bufsize=1024 * 1024,
                )

    async def _headers(self) -> dict[str, str]:
        token = await self.token_provider()
        if not token:
            raise RemoteAgentError("GCP credentials did not provide an access token")
        return {"Authorization": f"Bearer {token}"}

    async def _query(self, method: str, inputs: dict) -> object:
        await self._connect()
        assert self._http is not None
        async with self._http.post(
            self.url + ":query", headers=await self._headers(),
            json={"class_method": method, "input": inputs},
        ) as response:
            response.raise_for_status()
            payload = await response.json()
            if method == "async_delete_session" and payload == {}:
                return None
            if not isinstance(payload, dict) or "output" not in payload:
                raise RemoteAgentError(f"GCP {method} returned no output")
            return payload["output"]

    async def _remote_sessions(self, key: str) -> list[dict]:
        listed = await self._query("async_list_sessions", {"user_id": key})
        sessions = listed.get("sessions") if isinstance(listed, dict) else None
        if not isinstance(sessions, list):
            raise RemoteAgentError("GCP session listing returned an unexpected payload")
        return [s for s in sessions if isinstance(s, dict) and isinstance(s.get("id"), str) and s["id"]]

    async def reply(self, key: str, text: str) -> str:
        async with self._lock(key):
            remote_id = self._known.get(key)
            if remote_id is None:
                existing = await self._remote_sessions(key)
                if existing:
                    remote_id = max(existing, key=lambda s: s.get("lastUpdateTime") or 0)["id"]
                else:
                    created = await self._query("async_create_session", {"user_id": key})
                    if not isinstance(created, dict) or not isinstance(created.get("id"), str):
                        raise RemoteAgentError("GCP session creation returned no session ID")
                    remote_id = created["id"]
                    if not remote_id:
                        raise RemoteAgentError("GCP session creation returned an empty session ID")
                self._known[key] = remote_id
                logger.info("Agent Engine session=%s key=%s (%s)", remote_id, key[:12],
                            "reused" if existing else "created")
            await self._connect()
            assert self._http is not None
            answer = ""
            async with self._http.post(
                self.url + ":streamQuery", params={"alt": "sse"}, headers=await self._headers(),
                json={
                    "class_method": "async_stream_query",
                    "input": {"user_id": key, "session_id": remote_id, "message": text},
                },
            ) as response:
                response.raise_for_status()
                if response.content_type not in {"text/event-stream", "application/json"}:
                    raise RemoteAgentError("GCP returned an unsupported streaming content type")
                async for event in stream_events(response.content, response.content_type):
                    if event.get("error") or event.get("error_code") or event.get("errorCode"):
                        raise RemoteAgentError("The remote GCP agent reported an invocation error")
                    content = event.get("content") or {}
                    parts = content.get("parts") or []
                    tool_event = any(
                        any(key in part for key in (
                            "function_call", "functionCall", "function_response", "functionResponse"
                        ))
                        for part in parts
                    )
                    if content.get("role") == "model" and not event.get("partial") and not tool_event:
                        final = "\n".join(
                            part["text"] for part in parts
                            if isinstance(part.get("text"), str) and not part.get("thought")
                        ).strip()
                        if final:
                            answer = final
            if not answer:
                raise RemoteAgentError("GCP returned no final text response")
            return answer

    async def reset(self, key: str) -> None:
        async with self._lock(key):
            self._known.pop(key, None)
            for session in await self._remote_sessions(key):
                await self._query(
                    "async_delete_session", {"user_id": key, "session_id": session["id"]}
                )

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None


def create_engine(resource: str) -> RenewalEngine:
    credentials = gcp_credentials()
    refresh_lock = asyncio.Lock()

    async def access_token() -> str:
        async with refresh_lock:
            if not credentials.valid:
                await asyncio.to_thread(credentials.refresh, Request())
            if not credentials.token:
                raise RemoteAgentError("GCP ADC returned no access token")
            return credentials.token

    return RenewalEngine(resource, access_token)
