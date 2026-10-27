import json
import struct
from zipfile import ZipFile

import pytest

from scripts.build_package import build_package

BOT = "22222222-2222-4222-8222-222222222222"


def package(tmp_path, **updates):
    args = dict(
        output=tmp_path / "agent.zip", bot_id=BOT, domain="agent.example.org",
        developer="Bakeoff", website="https://example.org",
        privacy="https://example.org/privacy", terms="https://example.org/terms",
    )
    args.update(updates)
    return build_package(**args)


def test_copilot_package_wires_same_bot_id_and_has_valid_icon_sizes(tmp_path):
    with ZipFile(package(tmp_path)) as archive:
        assert set(archive.namelist()) == {"manifest.json", "color.png", "outline.png"}
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["copilotAgents"]["customEngineAgents"] == [{"id": BOT, "type": "bot"}]
        assert manifest["bots"][0]["botId"] == BOT
        assert manifest["bots"][0]["scopes"] == ["personal"]
        assert "${" not in json.dumps(manifest)
        for name, size in (("color.png", 192), ("outline.png", 32)):
            data = archive.read(name)
            assert data[:8] == b"\x89PNG\r\n\x1a\n"
            assert struct.unpack(">II", data[16:24]) == (size, size)


@pytest.mark.parametrize("updates", [
    {"bot_id": "not-a-uuid"}, {"domain": "https://agent.example.org/api/messages"},
    {"privacy": "http://example.org/privacy"}, {"developer": ""},
])
def test_bad_package_input_fails_without_creating_zip(tmp_path, updates):
    with pytest.raises(ValueError):
        package(tmp_path, **updates)
    assert not (tmp_path / "agent.zip").exists()
