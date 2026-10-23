import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

async def verify() -> None:
    from engine import conversation_key, create_engine

    state = json.loads((ROOT / "data" / "gcp-deployment.json").read_text("utf-8"))
    resource = state["resource"]
    engine = create_engine(resource)
    key = conversation_key("integration-test", "smoke", uuid4().hex, "read-only-probe")
    try:
        async with asyncio.timeout(360):
            answer = await engine.reply(
                key,
                "Evalua el riesgo de renovacion de Contoso. Consulta CRM, incidencias, "
                "get_usage_trend y search_contracts y cita tus fuentes. Recomienda acciones "
                "y prepara un borrador breve de email. Solo lectura: no crees ni modifiques "
                "incidentes, no envies correos ni ejecutes ninguna accion de escritura.",
            )
            print("REMOTE ANALYSIS:\n" + answer)
            follow_up = await engine.reply(
                key,
                "Sin volver a consultar herramientas ni realizar acciones, indica el nombre "
                "del cliente que acabamos de analizar y su nivel de riesgo. Responde en una frase.",
            )
            print("\nSESSION FOLLOW-UP:\n" + follow_up)
            if "contoso" not in answer.lower() or "contoso" not in follow_up.lower():
                raise RuntimeError("Remote session did not retain the customer context")
            report = {
                "resource": resource, "analysis": answer, "follow_up": follow_up,
                "transport": "Agent Engine REST through the M365 bridge client",
            }
            (ROOT / "data" / "gcp-smoke-result.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
    finally:
        try:
            await engine.reset(key)
        finally:
            await engine.close()


if __name__ == "__main__":
    asyncio.run(verify())
