import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer
from microsoft_agents.activity import Activity, TokenResponse
from microsoft_agents.hosting.core import AgentApplication, TurnContext

from app import create_app, message_chunks, plain_text
from config import Settings
from test_bridge_config import LOCAL, PRODUCTION, TENANT, USER
from user_auth import UserAuthorizationError


def activity(text="Analiza Contoso", **updates):
    payload = {
        "type": "message",
        "id": "test-activity",
        "channelId": "msteams",
        "serviceUrl": "https://smba.trafficmanager.net/amer/",
        "deliveryMode": "expectReplies",
        "from": {"id": "user1", "aadObjectId": "33333333-3333-4333-8333-333333333333"},
        "recipient": {"id": "bot1", "name": "Renewal Risk Analyst"},
        "conversation": {"id": "chat1", "conversationType": "personal"},
        "channelData": {"tenant": {"id": TENANT}},
        "text": text,
    }
    payload.update(updates)
    return payload


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = AsyncMock()
        self.engine.reply.return_value = "Risk: High\nA grounded draft"
        self.env = LOCAL
        self.client = await self.open_app(self.env)

    async def open_app(self, env):
        client = TestClient(TestServer(create_app(
            Settings.from_env(env), self.engine, env
        )))
        await client.start_server()
        return client

    async def asyncTearDown(self):
        await self.client.close()

    async def send(self, payload):
        response = await self.client.post("/api/messages", json=payload)
        assert response.status == 200, await response.text()
        body = await response.json()
        return [item.get("text", "") for item in body["activities"] if item["type"] == "message"]

    async def test_sdk_routes_message_and_preserves_identity(self):
        result = await self.send(activity())
        assert result == ["Risk: High\nA grounded draft"]
        key, text = self.engine.reply.call_args.args
        assert len(key) == 64
        assert text == "Analiza Contoso"

    async def test_help_reset_and_empty_messages_do_not_invoke_model(self):
        assert "agente remoto" in (await self.send(activity("/help")))[0]
        assert "Historial borrado" in (await self.send(activity("/reset")))[0]
        assert "consulta de texto" in (await self.send(activity("")))[0]
        self.engine.reply.assert_not_awaited()
        self.engine.reset.assert_awaited_once()

    async def test_error_notifies_user_without_leaking_backend_detail(self):
        self.engine.reply.side_effect = RuntimeError("private backend diagnostic")
        with self.assertLogs("app", level="ERROR"):
            result = await self.send(activity())
        assert "referencia" in result[0]
        assert "private backend diagnostic" not in result[0]

    async def test_channel_mentions_removed(self):
        payload = activity("<at>Renewal Risk Analyst</at> Analiza Contoso")
        payload["entities"] = [{
            "type": "mention",
            "mentioned": {"id": "bot1", "name": "Renewal Risk Analyst"},
            "text": "<at>Renewal Risk Analyst</at>",
        }]
        await self.send(payload)
        assert self.engine.reply.call_args.args[1] == "Analiza Contoso"

    async def test_production_rejects_missing_and_malformed_auth(self):
        await self.client.close()
        self.client = await self.open_app(PRODUCTION)
        for headers in ({}, {"Authorization": "Bearer not-a-jwt"}):
            response = await self.client.post("/api/messages", json=activity(), headers=headers)
            assert response.status == 401
        assert (await self.client.get("/healthz")).status == 200
        self.engine.reply.assert_not_awaited()


def test_long_output_is_lossless_and_bounded():
    text = "risk\n" * 2000 + "x" * 7001
    chunks = message_chunks(text)
    assert "".join(chunks) == text
    assert all(0 < len(chunk) <= 3500 for chunk in chunks)


def test_copilot_rich_text_reaches_agent_as_plain_text():
    raw = ('analiza el riesgo de <strong class="x" data-lexical-text="true">Northwind</strong>'
           ' &amp; prepara el email <br>')
    assert plain_text(raw) == "analiza el riesgo de Northwind & prepara el email"
    assert plain_text("ventas < 10 y > 5") == "ventas < 10 y > 5"
    assert plain_text("sin html, 3 < 5") == "sin html, 3 < 5"


class ProductionUserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = AsyncMock()
        self.engine.reply.return_value = "Contoso: riesgo alto"

        def capture_message(_agent, activity_type, **kwargs):
            assert activity_type == "message"
            self.auth_handlers = kwargs["auth_handlers"]

            def register(handler):
                self.handler = handler
                return handler
            return register

        with patch.object(AgentApplication, "activity", capture_message):
            create_app(Settings.from_env(PRODUCTION), self.engine, PRODUCTION)
        self.token_patch = patch("app.Authorization.get_token", new_callable=AsyncMock)
        self.token = self.token_patch.start()
        self.token.return_value = TokenResponse(token="opaque-test-token")
        self.graph_patch = patch("app.verify_entra_identity", new_callable=AsyncMock)
        self.graph = self.graph_patch.start()

    async def asyncTearDown(self):
        self.graph_patch.stop()
        self.token_patch.stop()

    async def send(self, streaming=False, **updates):
        context = MagicMock(spec=TurnContext)
        context.activity = Activity.model_validate(activity(**updates))
        context.send_activity = AsyncMock()
        context.streaming_response = MagicMock()
        context.streaming_response.is_streaming_channel = streaming
        context.streaming_response.end_stream = AsyncMock()
        await self.handler(context, None)
        return context

    async def test_route_requires_sdk_oauth_and_verified_user_before_gcp(self):
        assert self.auth_handlers == ["ENTRA"]
        await self.send()
        self.token.assert_awaited_once()
        self.graph.assert_awaited_once_with("opaque-test-token", USER)
        self.engine.reply.assert_awaited_once()
        assert "opaque-test-token" not in str(self.engine.reply.call_args)

    async def test_other_tenant_or_unlisted_user_never_calls_gcp(self):
        await self.send(channelData={"tenant": {"id": "other-tenant"}})
        await self.send(**{"from": {
            "id": "teams-user", "aadObjectId": "44444444-4444-4444-8444-444444444444",
        }})
        self.token.assert_not_awaited()
        self.graph.assert_not_awaited()
        self.engine.reply.assert_not_awaited()

    async def test_sender_requires_entra_object_id_not_teams_id(self):
        for sender in ({"id": USER}, {"id": "user", "aadObjectId": "invalid"}):
            await self.send(**{"from": sender})
        self.graph.assert_not_awaited()
        self.engine.reply.assert_not_awaited()

    async def test_missing_signin_or_mismatched_signin_blocks_query_and_reset(self):
        self.graph.side_effect = UserAuthorizationError("verification failed")
        for text in ("Analiza Contoso", "/reset", "/help"):
            context = await self.send(text=text)
            assert "No se ha invocado" in context.send_activity.call_args.args[0]
        self.engine.reply.assert_not_awaited()
        self.engine.reset.assert_not_awaited()

    async def test_absent_oauth_token_is_not_passed_to_gcp(self):
        self.token.return_value = None
        self.graph.side_effect = UserAuthorizationError("missing token")
        await self.send()
        self.graph.assert_awaited_once_with("", USER)
        self.engine.reply.assert_not_awaited()

    async def test_copilot_thread_reaches_gcp_but_group_chat_does_not(self):
        await self.send(channelId="msteams:COPILOT",
                        conversation={"id": "19:abc@thread.v2"})
        self.engine.reply.assert_awaited_once()
        await self.send(conversation={"id": "19:abc@thread.v2", "conversationType": "groupChat"})
        self.engine.reply.assert_awaited_once()

    async def test_plain_word_commands_work_where_copilot_reserves_slash(self):
        context = await self.send(channelId="msteams:COPILOT", text="Ayuda")
        assert "reiniciar" in context.send_activity.call_args.args[0]
        await self.send(channelId="msteams:COPILOT", text="reiniciar")
        self.engine.reset.assert_awaited_once()
        self.engine.reply.assert_not_awaited()

    async def test_streaming_channel_sends_progress_then_single_final_answer(self):
        context = await self.send(streaming=True, channelId="msteams:COPILOT")
        stream = context.streaming_response
        stream.queue_informative_update.assert_called_once()
        stream.queue_text_chunk.assert_called_once_with("Contoso: riesgo alto")
        stream.end_stream.assert_awaited_once()
        context.send_activity.assert_not_awaited()

    async def test_streaming_failure_closes_stream_before_error_handler(self):
        self.engine.reply.side_effect = RuntimeError("gcp down")
        stream = MagicMock(is_streaming_channel=True, end_stream=AsyncMock())
        with patch.object(TurnContext, "streaming_response", stream, create=True):
            context = MagicMock(spec=TurnContext)
            context.activity = Activity.model_validate(activity())
            context.send_activity = AsyncMock()
            context.streaming_response = stream
            with self.assertRaises(RuntimeError):
                await self.handler(context, None)
        stream.end_stream.assert_awaited_once()
        assert "No se pudo" in stream.queue_text_chunk.call_args.args[0]

    async def test_long_streamed_answer_overflows_into_extra_messages(self):
        self.engine.reply.return_value = "x" * 25000
        context = await self.send(streaming=True)
        assert len(context.streaming_response.queue_text_chunk.call_args.args[0]) == 20000
        assert len(context.send_activity.call_args.args[0].text) == 5000
