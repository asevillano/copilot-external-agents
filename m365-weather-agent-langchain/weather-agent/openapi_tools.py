"""LangChain tools generated from the OpenAPI (Swagger 2.0) files used in Copilot Studio.

Only GET operations with query parameters are supported, which is all OpenMeteo needs.
Each tool can only reach the host declared in its own spec.
"""
import asyncio
import json
from pathlib import Path
import re

import aiohttp
from langchain_core.tools import StructuredTool
from pydantic import Field, create_model

TYPES = {"string": str, "integer": int, "number": float, "boolean": bool}
MAX_BODY = 20_000


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text.strip()).strip("-")


def tools_from_spec(path: Path, session_factory=aiohttp.ClientSession) -> list[StructuredTool]:
    spec = json.loads(path.read_text("utf-8"))
    if spec.get("swagger") != "2.0":
        raise ValueError(f"{path.name}: only Swagger 2.0 specs are supported")
    if "https" not in spec.get("schemes", ["https"]):
        raise ValueError(f"{path.name}: only https APIs are allowed")
    base = f"https://{spec['host']}{spec.get('basePath', '/').rstrip('/')}"
    title = _slug(spec["info"]["title"])
    ops = [(p, m, op) for p, item in spec["paths"].items() for m, op in item.items()]

    tools = []
    for api_path, method, op in ops:
        if method.lower() != "get":
            raise ValueError(f"{path.name}: {method} {api_path} is not supported")
        fields = {}
        for prm in op.get("parameters", []):
            if prm.get("in") != "query":
                raise ValueError(f"{path.name}: parameter {prm['name']} must be a query parameter")
            kind = TYPES[prm.get("type", "string")]
            default = ... if prm.get("required") else prm.get("default")
            fields[prm["name"]] = (kind if prm.get("required") else kind | None,
                                   Field(default, description=prm.get("description", prm["name"])))
        # one operation per spec -> the tool is named like the Copilot Studio connector
        name = title if len(ops) == 1 else f"{title}_{op['operationId']}"
        args = create_model(f"{_slug(op['operationId'])}_args", **fields)
        url = base + api_path

        async def call(_url=url, **query):
            params = {k: str(v).lower() if isinstance(v, bool) else v
                      for k, v in query.items() if v is not None}
            last = ""
            # public API with occasional >20s stalls: retry once, then let the model explain
            for attempt in range(2):
                try:
                    async with session_factory(timeout=aiohttp.ClientTimeout(total=15)) as http:
                        async with http.get(_url, params=params, allow_redirects=False) as resp:
                            body = await resp.text()
                            if resp.status == 200:
                                return body[:MAX_BODY]
                            last = f"HTTP {resp.status}: {body[:300]}"
                            if resp.status < 500:
                                break
                except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                    last = f"{type(err).__name__} calling {spec['host']}"
            return json.dumps({"error": "weather service unavailable", "detail": last})

        summary = op.get("summary", op["operationId"])
        tools.append(StructuredTool.from_function(
            coroutine=call, name=name, args_schema=args,
            description=f"{spec['info']['title']} - '{summary}' operation. {op.get('description', '')}".strip(),
        ))
    return tools
