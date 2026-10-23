"""LangChain weather agent: Azure OpenAI (Entra, no keys) + OpenMeteo OpenAPI tools."""
from pathlib import Path
import os

from azure.identity import DefaultAzureCredential, ManagedIdentityCredential, get_bearer_token_provider
from langchain.agents import create_agent
from langchain_openai import AzureChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

from openapi_tools import tools_from_spec

ROOT = Path(__file__).resolve().parent

INSTRUCTIONS = """You are a weather assistant.

Language rules (highest priority):

- Detect the language of the user's latest message.
- ALWAYS respond in the same language as the user's latest message.
- Never translate your response to another language.
- If the user writes in English, respond in English.
- If the user writes in Spanish, respond in Spanish.
- If the user writes in French, respond in French.

Weather rules:

- If the user does not provide a location, ask for it.
- When the user provides a city:

1. Use the 'Search location' operation in OpenMeteo-Geocoding tool to get latitude and longitude.
2. Read latitude and longitude from the first result.
3. Use the 'Get weather forecast' operation in OpenMeteo-Weather tool with those coordinates.
4. Return the current weather.

- Always use the tools.
- Never answer from memory"""


def build_model(env=os.environ):
    if env.get("IDENTITY_ENDPOINT"):
        credential = ManagedIdentityCredential(client_id=env.get("AZURE_CLIENT_ID"))
    else:
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return AzureChatOpenAI(
        azure_endpoint=env["AZURE_OPENAI_ENDPOINT"],
        azure_deployment=env.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.6-luna"),
        api_version=env.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
        azure_ad_token_provider=get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"),
        timeout=60, max_retries=2,
    )


class WeatherAgent:
    """Conversation memory per thread_id in process (single replica, like the other bridges)."""

    def __init__(self, model, tools):
        self.checkpointer = InMemorySaver()
        self.graph = create_agent(model, tools=tools, system_prompt=INSTRUCTIONS,
                                  checkpointer=self.checkpointer)

    async def reply(self, thread_id: str, text: str) -> str:
        state = await self.graph.ainvoke(
            {"messages": [{"role": "user", "content": text}]},
            config={"configurable": {"thread_id": thread_id}, "recursion_limit": 12},
        )
        answer = state["messages"][-1]
        content = answer.content
        if isinstance(content, list):  # responses-style content blocks
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        return (content or "").strip()

    async def reset(self, thread_id: str) -> None:
        self.checkpointer.delete_thread(thread_id)


def create_weather_agent(env=os.environ) -> WeatherAgent:
    tools = []
    for spec in sorted((ROOT / "openapi").glob("*.json")):
        tools.extend(tools_from_spec(spec))
    return WeatherAgent(build_model(env), tools)
