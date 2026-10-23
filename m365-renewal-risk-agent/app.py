import asyncio
from html.parser import HTMLParser
import logging
import os
import time
from uuid import UUID, uuid4

from aiohttp import web
from dotenv import load_dotenv
from microsoft_agents.activity import Activity, ActivityTypes, ChannelId
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import (
    CloudAdapter,
    jwt_authorization_middleware,
    start_agent_process,
)
from microsoft_agents.hosting.core import (
    AgentApplication,
    AgentAuthConfiguration,
    AuthHandler,
    Authorization,
    AnonymousTokenProvider,
    ConnectionManager,
    MemoryStorage,
    TurnContext,
    TurnState,
)

from config import CONNECTION, ROOT, Settings
from engine import RenewalEngine, conversation_key, create_engine
from user_auth import UserAuthorizationError, verify_entra_identity

logger = logging.getLogger(__name__)
HELP = (
    "Soy Renewal Risk Analyst. Analizo CRM, soporte, uso y contratos con el agente de GCP. "
    "Por ejemplo: 'Analiza el riesgo de renovacion de Contoso y prepara un email'.\n\n"
    "Este puente invoca al agente remoto y no cambia sus instrucciones ni sus herramientas. "
    "Escribe 'reiniciar' para borrar su sesion de este chat en GCP o 'ayuda' para ver esta ayuda "
    "(en Teams tambien valen /reset y /help; en Copilot esos comandos los reserva Copilot)."
)


def message_chunks(text: str, limit: int = 3500) -> list[str]:
    chunks = []
    while len(text) > limit:
        end = text.rfind("\n", 0, limit + 1)
        if end <= 0:
            end = limit
        chunks.append(text[:end])
        text = text[end:]
    if text:
        chunks.append(text)
    return chunks


class _TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div", "li"}:
            self.out.append("\n")

    def handle_data(self, data):
        self.out.append(data)


def plain_text(text: str) -> str:
    # Copilot sends the composer's rich text (<strong data-lexical-text>, <br>) as HTML
    if "<" not in text or ">" not in text:
        return text
    parser = _TextOnly()
    parser.feed(text)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.out).splitlines())
    return "\n".join(line for line in lines if line)


def create_app(settings: Settings, engine: RenewalEngine, env: dict[str, str]) -> web.Application:
    if settings.local:
        auth_config = AgentAuthConfiguration(anonymous_allowed=True)
        connections = ConnectionManager(
            provider_factory=lambda _config: AnonymousTokenProvider(),
            connections_configurations={"SERVICE_CONNECTION": auth_config},
        )
    else:
        auth_config = AgentAuthConfiguration(
            client_id=env[CONNECTION + "CLIENTID"],
            client_secret=env[CONNECTION + "CLIENTSECRET"],
            tenant_id=settings.tenant_id,
            anonymous_allowed=False,
            validate_issuer=True,
        )
        connections = MsalConnectionManager(
            connections_configurations={"SERVICE_CONNECTION": auth_config}
        )
    adapter = CloudAdapter(connection_manager=connections)

    storage = MemoryStorage()
    authorization = None if settings.local else Authorization(
        storage, connections, use_cache=False,
        auth_handlers={"ENTRA": AuthHandler(
            name="ENTRA", abs_oauth_connection_name=settings.oauth_connection,
            scopes=["User.Read"], title="Iniciar sesion",
            text="Accede con tu cuenta corporativa autorizada de Microsoft 365.",
        )},
    )
    agent = AgentApplication[TurnState](
        storage=storage, connection_manager=connections if settings.local else None,
        authorization=authorization,
    )

    @agent.activity("message", auth_handlers=None if settings.local else ["ENTRA"])
    async def on_message(context: TurnContext, _state: TurnState):
        activity = context.activity
        conversation = activity.conversation
        # Copilot 1:1 chats arrive as msteams:COPILOT on a thread id, without conversationType=personal
        sub_channel = (ChannelId.get_sub_channel(activity.channel_id) or "").lower()
        copilot = sub_channel.startswith("copilot")
        if conversation is None or (
            not settings.local and conversation.conversation_type != "personal" and not copilot
        ):
            logger.warning(
                "Rejected non-personal conversation: channel=%s type=%s",
                activity.channel_id, conversation.conversation_type if conversation else None,
            )
            await context.send_activity("Este agente solo admite chats personales.")
            return
        data = activity.channel_data or {}
        tenant_data = data.get("tenant") or {}
        tenant = tenant_data.get("id") or conversation.tenant_id
        if settings.local:
            tenant = tenant or "local"
        elif not tenant or tenant.lower() != settings.m365_tenant_id:
            logger.warning("Rejected activity outside configured tenant")
            await context.send_activity("Este agente no esta disponible para este tenant.")
            return
        sender = activity.from_property
        user = (sender.aad_object_id or (sender.id if settings.local else None)) if sender else None
        if not user or not conversation.id or not activity.channel_id:
            logger.warning("Rejected activity without conversation/sender identity")
            await context.send_activity("No se pudo identificar el usuario o la conversacion.")
            return
        if not settings.local:
            try:
                user = str(UUID(user))
            except ValueError:
                logger.warning("Rejected activity with invalid Entra sender object ID")
                await context.send_activity("No se pudo verificar tu identidad corporativa.")
                return
            if user not in settings.allowed_user_ids:
                logger.warning("Rejected Entra user outside explicit pilot allowlist")
                await context.send_activity("Tu usuario no esta autorizado para utilizar este agente.")
                return
            if authorization is None:
                raise RuntimeError("Production user authorization is not configured")
            token_response = await authorization.get_token(context, "ENTRA")
            try:
                await verify_entra_identity(
                    (token_response.token or "") if token_response else "", user
                )
            except UserAuthorizationError as error:
                logger.warning("Entra user verification rejected the request: %s", error)
                await context.send_activity(
                    "No se pudo verificar tu inicio de sesion corporativo. "
                    "No se ha invocado al agente de GCP."
                )
                return
        text = plain_text(
            TurnContext.remove_recipient_mention(activity) or activity.text or ""
        ).strip()
        if not text:
            logger.warning("Rejected empty text: channel=%s attachments=%d",
                           activity.channel_id, len(activity.attachments or []))
            await context.send_activity("Envia una consulta de texto; no proceso archivos adjuntos.")
            return
        if len(text) > 16000:
            await context.send_activity("La consulta es demasiado larga. Limitala a 16000 caracteres.")
            return
        key = conversation_key(tenant, activity.channel_id, conversation.id, user)
        command = text.lower()
        if command in {"/help", "ayuda"}:
            await context.send_activity(HELP)
            return
        async with asyncio.timeout(settings.timeout):
            if command in {"/reset", "reiniciar"}:
                await engine.reset(key)
                await context.send_activity("Historial borrado para tu usuario en este chat.")
                return
            stream = context.streaming_response
            started = time.monotonic()
            logger.info("Invoking GCP agent: channel=%s key=%s streaming=%s",
                        activity.channel_id, key[:12], stream.is_streaming_channel)
            if not stream.is_streaming_channel:
                await context.send_activity(Activity(type=ActivityTypes.typing))
                answer = await engine.reply(key, text)
                logger.info("GCP answered key=%s in %.1fs (%d chars)",
                            key[:12], time.monotonic() - started, len(answer))
                for chunk in message_chunks(answer):
                    await context.send_activity(
                        Activity(type=ActivityTypes.message, text=chunk, text_format="markdown")
                    )
                return
            # Copilot drops the turn after ~45s without stream activity; the SDK keeps it alive
            # every 35s and falls back to a normal message past 105s. Analyses take 1-3 minutes.
            stream.queue_informative_update(
                "Consultando al Renewal Risk Analyst en GCP (CRM, soporte, uso y contratos)..."
            )
            try:
                answer = await engine.reply(key, text)
            except BaseException:
                stream.queue_text_chunk("No se pudo completar el analisis en GCP.")
                await stream.end_stream()
                raise
            logger.info("GCP answered key=%s in %.1fs (%d chars)",
                        key[:12], time.monotonic() - started, len(answer))
            chunks = message_chunks(answer, 20000)
            stream.queue_text_chunk(chunks[0])
            await stream.end_stream()
            for chunk in chunks[1:]:
                await context.send_activity(
                    Activity(type=ActivityTypes.message, text=chunk, text_format="markdown")
                )

    @agent.error
    async def on_error(context: TurnContext, error: Exception):
        reference = uuid4().hex[:12]
        logger.error(
            "Agent turn failed; reference=%s", reference,
            exc_info=(type(error), error, error.__traceback__),
        )
        await context.send_activity(
            f"No se pudo completar la solicitud (referencia {reference}). "
            "No se reintentara automaticamente. Si habia una accion en curso, comprueba "
            "su estado antes de repetirla."
        )

    async def messages(request: web.Request) -> web.Response:
        return await start_agent_process(request, agent, adapter)

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    @web.middleware
    async def channel_auth(request: web.Request, handler):
        if request.path == "/healthz":
            return await handler(request)
        return await jwt_authorization_middleware(request, handler)

    async def cleanup(_app: web.Application):
        await engine.close()

    app = web.Application(middlewares=[channel_auth], client_max_size=256 * 1024)
    app["agent_configuration"] = auth_config
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/healthz", health)
    app.on_cleanup.append(cleanup)
    return app


if __name__ == "__main__":
    load_dotenv(ROOT / ".env", override=False)
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env(os.environ)
    app = create_app(settings, create_engine(settings.resource), dict(os.environ))
    web.run_app(app, host=settings.host, port=settings.port)
