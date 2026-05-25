"""Allow running the dashboard with: python -m dashboard"""

import os
import sys
from pathlib import Path

import uvicorn


def _load_dotenv() -> None:
    """Load .env at the project root into os.environ (without overriding existing).

    Without this, running the dashboard via `python -m dashboard` (instead of
    `./klodo.sh dashboard` which sources .env explicitly) leaves SILICONFLOW_API_KEY
    unset — the agent then fails with RuntimeError when the user triggers a
    diagnostic from the UI.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
uvicorn.run("dashboard.app:app", host="127.0.0.1", port=port)
