"""Deployment settings: the project's git-ignored .env first, then the process environment."""
import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_env(path: Path = ENV_FILE) -> None:
    # variables already set in the process win, like python-dotenv with override=False
    if not path.is_file():
        return
    for raw in path.read_text("utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def require(*names: str) -> None:
    missing = [name for name in names if not setting(name)]
    if missing:
        raise RuntimeError(f"Missing {', '.join(missing)} in {ENV_FILE} (see .env.example)")
