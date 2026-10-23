import argparse
import json
from pathlib import Path
import re
import struct
from urllib.parse import urlparse
from uuid import UUID
from zipfile import ZipFile, ZIP_DEFLATED
import zlib

ROOT = Path(__file__).resolve().parent.parent


def icon_png(size: int, outline: bool = False) -> bytes:
    def block(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xFFFFFFFF
        )

    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            px, py = x / size, y / size
            axis = (0.21 < px < 0.26 and 0.2 < py < 0.8) or (
                0.21 < px < 0.8 and 0.75 < py < 0.8
            )
            trend = 0.32 < px < 0.8 and abs(py - (0.85 - 0.7 * px)) < 0.035
            pixel = (255, 255, 255, 255) if axis or trend else (
                (0, 0, 0, 0) if outline else (124, 58, 237, 255)
            )
            rows.extend(pixel)
    return (
        b"\x89PNG\r\n\x1a\n"
        + block(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + block(b"IDAT", zlib.compress(bytes(rows)))
        + block(b"IEND", b"")
    )


def build_package(
    output: Path, bot_id: str, domain: str, developer: str,
    website: str, privacy: str, terms: str, app_id: str | None = None,
) -> Path:
    bot_id = str(UUID(bot_id))
    app_id = str(UUID(app_id)) if app_id else bot_id
    if not re.fullmatch(
        r"(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
        r"[a-zA-Z]{2,63}", domain
    ):
        raise ValueError("domain must be a public hostname, without scheme, port or path")
    for url in (website, privacy, terms):
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Website, privacy and terms must be public HTTPS URLs")
    if not developer.strip() or len(developer) > 32:
        raise ValueError("Developer name must contain 1..32 characters")
    replacements = {
        "${BOT_ID}": bot_id, "${APP_ID}": app_id, "${BOT_DOMAIN}": domain.lower(),
        "${DEVELOPER_NAME}": developer, "${WEBSITE_URL}": website,
        "${PRIVACY_URL}": privacy, "${TERMS_URL}": terms,
    }
    manifest = json.loads((ROOT / "appPackage" / "manifest.template.json").read_text("utf-8"))

    def expand(value):
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        return replacements.get(value, value) if isinstance(value, str) else value

    manifest = expand(manifest)
    if "${" in json.dumps(manifest):
        raise ValueError("Manifest contains unresolved placeholders")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as package:
        package.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        package.writestr("color.png", icon_png(192))
        package.writestr("outline.png", icon_png(32, outline=True))
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the M365 custom engine agent app package")
    parser.add_argument("--bot-id", required=True, help="Entra client ID registered with Azure Bot")
    parser.add_argument("--app-id", help="Optional separate M365 app UUID")
    parser.add_argument("--domain", required=True, help="Public HTTPS messaging endpoint hostname")
    parser.add_argument("--developer", required=True)
    parser.add_argument("--website", required=True)
    parser.add_argument("--privacy", required=True)
    parser.add_argument("--terms", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "renewal-risk-agent-mcp.zip")
    args = parser.parse_args()
    print(build_package(
        args.output, args.bot_id, args.domain, args.developer,
        args.website, args.privacy, args.terms, args.app_id,
    ))
