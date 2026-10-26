# Agentes externos en Microsoft 365 Copilot

Integraciones de referencia para usar desde **Microsoft 365 Copilot y Teams** agentes que
viven **fuera de Microsoft 365**, sin reescribirlos. Todas siguen el patrón de
[*custom engine agent*](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/overview-custom-engine-agent):
un puente con el **Microsoft 365 Agents SDK**, alojado en **Azure Container Apps**, recibe
los mensajes de Copilot a través de **Azure Bot Service** y los reenvía al agente externo.

## Integraciones

| Carpeta | Agente externo | Cómo llega el puente al agente | Identidad hacia el agente |
|---|---|---|---|
| [m365-renewal-risk-agent](m365-renewal-risk-agent/README.md) | *Renewal Risk Analyst* (Google ADK) en **Vertex AI Agent Engine** | REST de Agent Engine, directamente desde el puente | Identidad administrada de Azure federada con GCP (Workload Identity Federation), sin claves |
| [m365-renewal-risk-agent-mcp](m365-renewal-risk-agent-mcp/README.md) | El mismo agente de GCP | **Servidor MCP** en ACA (*agente como herramienta*); solo el MCP tiene acceso a GCP | Entra ID (rol `Agent.Invoke`) del puente al MCP; WIF del MCP a GCP |
| [m365-weather-agent-langchain](m365-weather-agent-langchain/README.md) | Asistente del tiempo con **LangChain** y `gpt-5.6-luna` (Microsoft Foundry) en ACA | **MCP** expuesto por el propio agente, en otro entorno ACA | Entra ID (rol `Agent.Invoke`); el agente llama al modelo con su identidad administrada |

Cada carpeta es un proyecto independiente, con su guía paso a paso, su arquitectura, su
infraestructura (Bicep), sus scripts de despliegue y sus pruebas. Elementos comunes:

- **Streaming** hacia Copilot y Teams, con mensajes de progreso que evitan el corte de 45 s de Copilot.
- **OAuth del usuario** (Entra ID, `User.Read`), comprobación de tenant y **lista de usuarios autorizados**.
- **Sesiones por chat**: cada conversación de Copilot se corresponde con una conversación del agente remoto.
- **Sin secretos en código ni en ficheros**: identidades administradas, federación y secretos de ACA.

## Cómo desarrollar el puente para un nuevo agente externo

El puente se construye con el
[**Microsoft 365 Agents SDK**](https://github.com/microsoft/Agents-for-python) (paquetes
`microsoft-agents-*`), el SDK de Microsoft para los *custom engine agents* de Copilot.
Los tres puentes de este repositorio comparten el mismo código: entre ellos solo cambian
unas 9 líneas de `app.py` (textos para el usuario y el import del cliente) y el cliente que
habla con el agente remoto. Por eso, integrar otro agente no consiste en escribir un puente
nuevo, sino en **copiar uno existente y sustituir el cliente**.

### Qué hace y qué no hace el puente

El puente es **solo transporte y control de acceso**: no contiene prompts, modelos,
herramientas ni lógica de negocio. Todo eso sigue en el agente externo. El puente:

1. Recibe la actividad de Copilot o Teams y valida el JWT del canal (Bot Framework).
2. Comprueba tenant, chat personal o de Copilot, y que el usuario esté en la lista autorizada.
3. Obtiene el token OAuth del usuario y verifica con Graph (`/me`) que coincide con el remitente.
4. Calcula una clave opaca por conversación y llama al agente remoto.
5. Devuelve la respuesta por *streaming*, con un mensaje de progreso mientras el agente trabaja.

### Anatomía del puente

Tomando como plantilla [m365-weather-agent-langchain/bridge](m365-weather-agent-langchain/bridge):

| Archivo | Responsabilidad | ¿Cambia para otro agente? |
|---|---|---|
| `app.py` | Hosting aiohttp, `CloudAdapter`, `AgentApplication`, OAuth `ENTRA`, filtros de tenant, canal y usuario, comandos `ayuda`/`reiniciar`, *streaming*, gestión de errores, `/healthz` | Solo los textos (`HELP`, actualización informativa, mensaje de error) y el import del cliente |
| `config.py` | Lee y valida la configuración; el puente no arranca si falta algo de seguridad | Solo si el cliente necesita otros parámetros |
| `user_auth.py` | Verificación del usuario con Graph `/me` | No |
| `mcp_engine.py` (o `engine.py`) | **Cliente del agente remoto** | **Sí: es la única pieza específica** |
| `appPackage/manifest.template.json`, `scripts/build_package.py` | Manifiesto M365 (v1.22, `copilotAgents.customEngineAgents`) y paquete `.zip` | Nombre, descripción, color y ruta de salida |
| `Dockerfile` | Imagen no root que copia solo los archivos del puente | Solo si cambia el nombre del cliente |
| `infra/bridge.bicep` | Container App con identidad administrada y el secreto del bot como secreto de ACA | Variables de entorno del cliente |
| `tests/` | Pruebas del puente con el cliente simulado (`AsyncMock`) | Solo las del cliente |

### El contrato del cliente

`app.py` solo usa cuatro elementos del módulo del cliente. Cualquier agente externo
se integra implementándolos:

```python
def conversation_key(tenant: str, channel: str, conversation: str, user: str) -> str:
    """Clave opaca y estable por tenant, canal, chat y usuario (SHA-256)."""


class AgentEngine:
    async def reply(self, key: str, text: str) -> str:
        """Envía el mensaje a la conversación remota `key` y devuelve la respuesta final."""

    async def reset(self, key: str) -> None:
        """Olvida la conversación remota (comando 'reiniciar')."""

    async def close(self) -> None:
        """Libera conexiones al apagar el puente."""


def create_engine(...) -> AgentEngine:
    """Construye el cliente con su credencial: local (ADC, az login) o identidad administrada."""
```

Reglas que cumplen los clientes de este repositorio:

- **Una conversación remota por `key`.** El agente externo guarda el historial; el puente no
  guarda estado y puede reiniciarse sin perder conversaciones.
- **Errores como excepciones.** `app.py` las captura, cierra el *stream* y muestra al usuario
  una referencia, sin detalles internos. No hay reintentos automáticos.
- **Serializar los turnos del mismo chat** si el backend no lo hace. [engine.py](m365-renewal-risk-agent/engine.py)
  usa un `asyncio.Lock` por clave; por eso ese puente funciona con una sola réplica.
- **Credenciales sin claves.** Identidad administrada de Azure, federada con la nube del agente
  (Workload Identity Federation) o con un token Entra para una API propia.

### Paso a paso

1. **Elegir cómo se llega al agente.**
   - **Por MCP (recomendado).** Si el agente expone MCP, o se le pone delante un adaptador MCP
     como [mcp-server](m365-renewal-risk-agent-mcp/mcp-server), el puente **no se modifica**:
     se reutiliza [mcp_engine.py](m365-weather-agent-langchain/bridge/mcp_engine.py)
     cambiando `ASK_TOOL` (la herramienta debe aceptar `message` y `conversation_id`, y
     existir otra `reset_conversation`), y se configuran `MCP_SERVER_URL` y
     `MCP_TOKEN_RESOURCE`. Ver el contrato en la sección *Contrato MCP* de
     [m365-weather-agent-langchain](m365-weather-agent-langchain/README.md).
   - **Por la API del agente.** Escribir un cliente que cumpla el contrato anterior, como
     [engine.py](m365-renewal-risk-agent/engine.py) para Vertex AI Agent Engine.
2. **Copiar el puente** de `m365-weather-agent-langchain/bridge`, con sus pruebas, el
   manifiesto, el `Dockerfile` e `infra/bridge.bicep`.
3. **Adaptar los textos** de `app.py` y el manifiesto (nombre, descripción, color, ruta del
   `.zip`). Generar un **ID de app nuevo**: cada agente es una app distinta en M365.
4. **Probar en local** con `LOCAL_DEVELOPMENT=true` (anónimo y solo en loopback) y el
   [Agents Playground](https://learn.microsoft.com/en-us/microsoft-365/agents-sdk/test-with-toolkit-project)
   (`npx @microsoft/teams-app-test-tool`) apuntando a `http://127.0.0.1:3978/api/messages`.
   Las pruebas de `app.py` sustituyen el cliente por un `AsyncMock` y siguen sirviendo sin
   cambios; en `tests/test_mcp_engine.py` solo hay que renombrar la herramienta simulada.
5. **Registrar la identidad del agente en Entra y crear el Azure Bot**: registro *single
   tenant* con redirect `https://token.botframework.com/.auth/web/redirect` y permiso
   delegado `User.Read`; Bot F0 con los canales **Microsoft Teams** y **Microsoft 365
   Extensions**, y una conexión OAuth *Azure AD v2*. Las fases 2 y 3 de la
   [guía de la versión directa](m365-renewal-risk-agent/README.md#4-guía-paso-a-paso-para-el-cliente)
   lo detallan, y [deploy_weather_stack.py](m365-weather-agent-langchain/scripts/deploy_weather_stack.py)
   lo automatiza.
6. **Desplegar en Azure Container Apps** con la imagen por digest y configurar el
   *messaging endpoint* del Bot en `https://<fqdn>/api/messages`.
7. **Empaquetar y publicar** con `scripts/build_package.py` en el catálogo de la organización.

### Requisitos de Copilot que el puente ya resuelve

Estos comportamientos costaron tiempo de diagnóstico y están resueltos en `app.py`; conviene
no perderlos al copiar el puente:

| Comportamiento de Copilot | Cómo lo resuelve el puente |
|---|---|
| Corta el turno si no hay actividad en unos 45 s | `streaming_response`: actualización informativa inmediata y *keep-alive* del SDK cada 35 s |
| Los chats 1:1 de Copilot llegan como subcanal `msteams:COPILOT`, no como `personal` | Se aceptan chats personales y el subcanal Copilot; se rechazan grupos y canales |
| `/help` y otros comandos con barra los reserva Copilot | Comandos sin barra: `ayuda` y `reiniciar` |
| El texto llega como HTML enriquecido del compositor | `plain_text()` lo convierte a texto plano |
| Sin el canal **Microsoft 365 Extensions** en el Bot, Copilot no entrega mensajes | Es obligatorio al crear el Bot (paso 5) |
| Respuestas largas | Se trocean en varios mensajes sin perder contenido |

## Presentación

[presentacion/Agentes-externos-en-M365-Copilot.pptx](presentacion/Agentes-externos-en-M365-Copilot.pptx)
presenta las tres integraciones: arquitectura común, detalle de cada agente, comparativa,
seguridad, aprendizajes, escalado a cientos de agentes y diagramas de secuencia.

## Configuración

Los identificadores de cada entorno (suscripción y tenant de Azure, IDs de apps de Entra,
proyecto de GCP, ACR, endpoints…) **no están en el repositorio**. Cada proyecto incluye un
`.env.example`; para desplegar, copiarlo a `.env` en la misma carpeta y rellenarlo. Los
`.env`, el estado local de despliegue (`data/`) y los paquetes generados (`dist/`, `*.zip`)
están excluidos de git.

## Pruebas

Cada proyecto tiene su entorno virtual y sus pruebas, que usan servicios simulados y no
necesitan `.env`:

```powershell
cd m365-renewal-risk-agent      # o m365-renewal-risk-agent-mcp, m365-weather-agent-langchain
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

La creación del entorno virtual y las dependencias se explica en el apartado
*Desarrollo local y pruebas* de cada README.
