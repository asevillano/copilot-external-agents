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
