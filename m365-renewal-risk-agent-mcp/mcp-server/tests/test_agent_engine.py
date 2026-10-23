import asyncio
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock

from aiohttp import ClientResponseError, web
from aiohttp.test_utils import TestServer

from agent_engine import RemoteAgentError, RenewalEngine, conversation_key
RESOURCE = "projects/test-project/locations/us-central1/reasoningEngines/123456789"


class RemoteEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.sessions = {}
        self.counter = 0
        self.failure = None
        self.events = None
        self.invalid_session = False
        self.json_stream = False
        app = web.Application()
        app.router.add_post("/v1/{tail:.*}", self.remote_agent)
        self.server = TestServer(app)
        await self.server.start_server()
        self.engine = self.new_engine()

    def new_engine(self, resource=RESOURCE):
        engine = RenewalEngine(resource, AsyncMock(return_value="test-token"))
        engine.url = str(self.server.make_url("/v1/" + resource))
        return engine

    async def asyncTearDown(self):
        await self.engine.close()
        await self.server.close()

    def methods(self):
        return [call[1]["class_method"] for call in self.calls]

    async def remote_agent(self, request):
        body = await request.json()
        self.calls.append((request.path, body, request.headers.get("Authorization")))
        if self.failure:
            return web.json_response({"error": {"message": "remote failure"}}, status=self.failure)
        method = body["class_method"]
        inputs = body["input"]
        if method == "async_list_sessions":
            return web.json_response({"output": {"sessions": [
                {"id": sid, "userId": s["user"], "lastUpdateTime": s["updated"]}
                for sid, s in self.sessions.items() if s["user"] == inputs["user_id"]
            ]}})
        if method == "async_create_session":
            if self.invalid_session:
                return web.json_response({"output": {}})
            self.counter += 1
            session = f"remote-{self.counter}"
            self.sessions[session] = {"user": inputs["user_id"], "turns": 0, "updated": self.counter}
            return web.json_response({"output": {"id": session}})
        if method == "async_delete_session":
            del self.sessions[inputs["session_id"]]
            return web.json_response({})
        assert method == "async_stream_query"
        assert request.query["alt"] == "sse"
        session = self.sessions[inputs["session_id"]]
        assert session["user"] == inputs["user_id"]
        session["turns"] += 1
        events = self.events if self.events is not None else [{
            "author": "renewal_risk_agent", "content": {
                "role": "model", "parts": [{"text": f"remote turn {session['turns']}"}]
            }
        }]
        response = web.StreamResponse(headers={
            "Content-Type": "application/json" if self.json_stream else "text/event-stream"
        })
        await response.prepare(request)
        for event in events:
            encoded = json.dumps(event, ensure_ascii=False)
            frame = (encoded + "\n" if self.json_stream else "data: " + encoded + "\r\n\r\n").encode()
            for i in range(0, len(frame), 7):
                await response.write(frame[i:i + 7])
        await response.write_eof()
        return response

    async def test_remote_contract_and_reuse_survive_bridge_restart(self):
        assert await self.engine.reply("chat1", "Analiza Contoso") == "remote turn 1"
        assert [c[1] for c in self.calls[:2]] == [
            {"class_method": "async_list_sessions", "input": {"user_id": "chat1"}},
            {"class_method": "async_create_session", "input": {"user_id": "chat1"}},
        ]
        assert self.calls[2][1] == {
            "class_method": "async_stream_query",
            "input": {"user_id": "chat1", "session_id": "remote-1", "message": "Analiza Contoso"},
        }
        assert all(call[2] == "Bearer test-token" for call in self.calls)
        assert await self.engine.reply("chat1", "again") == "remote turn 2"
        assert self.methods()[-1] == "async_stream_query"
        assert self.methods().count("async_list_sessions") == 1
        await self.engine.close()
        self.engine = self.new_engine()
        assert await self.engine.reply("chat1", "approved") == "remote turn 3"
        assert self.counter == 1

    async def test_chats_get_separate_remote_sessions_and_scoped_reset(self):
        await self.engine.reply("chat1", "hello")
        await self.engine.reply("chat2", "hello")
        assert self.counter == 2
        await self.engine.reset("chat1")
        assert self.calls[-1][1] == {
            "class_method": "async_delete_session",
            "input": {"user_id": "chat1", "session_id": "remote-1"},
        }
        assert await self.engine.reply("chat1", "hello") == "remote turn 1"
        assert await self.engine.reply("chat2", "hello") == "remote turn 2"

    async def test_restart_picks_latest_remote_session(self):
        self.sessions["old"] = {"user": "chat1", "turns": 5, "updated": 1}
        self.sessions["new"] = {"user": "chat1", "turns": 1, "updated": 9}
        assert await self.engine.reply("chat1", "hello") == "remote turn 2"
        await self.engine.reset("chat1")
        assert not self.sessions

    async def test_simultaneous_turns_do_not_create_duplicate_sessions(self):
        results = await asyncio.gather(
            self.engine.reply("chat1", "one"),
            self.engine.reply("chat1", "two"),
        )
        assert results == ["remote turn 1", "remote turn 2"]
        assert self.counter == 1

    async def test_only_final_model_text_is_forwarded_without_changing_it(self):
        self.events = [
            {"content": {"role": "model", "parts": [{"text": "partial"}]}, "partial": True},
            {"content": {"role": "model", "parts": [{"text": "intermediate"}]}},
            {"content": {"role": "user", "parts": [{"text": "tool secret"}]}},
            {"content": {"role": "model", "parts": [{"functionCall": {"name": "crm"}}]}},
            {"content": {"role": "model", "parts": [
                {"text": "internal", "thought": True},
                {"text": "Riesgo: alto. Renovación pendiente.\n¿Apruebas el borrador?"},
            ]}},
        ]
        result = await self.engine.reply("chat1", "hello")
        assert result == "Riesgo: alto. Renovación pendiente.\n¿Apruebas el borrador?"

    async def test_remote_http_error_is_not_retried_or_replaced_by_answer(self):
        self.failure = 403
        with self.assertRaises(ClientResponseError):
            await self.engine.reply("chat1", "hello")
        assert len(self.calls) == 1

    async def test_actual_gcp_json_stream_handles_multiple_events_and_split_utf8(self):
        self.json_stream = True
        self.events = [
            {"content": {"role": "model", "parts": [{"text": "preparando"}]}},
            {"content": {"role": "model", "parts": [{"text": "Renovación: riesgo alto."}]}},
        ]
        assert await self.engine.reply("chat1", "hello") == "Renovación: riesgo alto."

    async def test_missing_remote_session_id_is_an_error(self):
        self.invalid_session = True
        with self.assertRaisesRegex(RemoteAgentError, "no session ID"):
            await self.engine.reply("chat1", "hello")

    async def test_empty_final_text_and_remote_invocation_error_are_errors(self):
        self.events = []
        with self.assertRaisesRegex(RemoteAgentError, "no final text"):
            await self.engine.reply("chat1", "hello")
        self.events = [{"error_code": "REMOTE_FAILURE"}]
        with self.assertRaisesRegex(RemoteAgentError, "invocation error"):
            await self.engine.reply("chat1", "hello")

    async def test_failed_remote_delete_keeps_mapping_for_explicit_retry(self):
        await self.engine.reply("chat1", "hello")
        self.failure = 503
        with self.assertRaises(ClientResponseError):
            await self.engine.reset("chat1")
        self.failure = None
        assert await self.engine.reply("chat1", "hello") == "remote turn 2"
        assert self.counter == 1


def test_bridge_does_not_load_adk_or_original_agent():
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c",
         "import agent_engine, sys; "
         "assert not any(n.startswith(('google.adk', 'renewal_risk_agent', 'google.genai')) "
         "for n in sys.modules)"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_identity_boundaries():
    base = ["tenant1", "msteams", "chat1", "user1"]
    key = conversation_key(*base)
    assert key == conversation_key(*base)
    for index in range(4):
        other = base.copy()
        other[index] += "-different"
        assert conversation_key(*other) != key
    with unittest.TestCase().assertRaises(ValueError):
        conversation_key("", "msteams", "chat", "user")
