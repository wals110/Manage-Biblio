"""Allow running the dashboard with: python -m dashboard"""

import sys

import uvicorn

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
uvicorn.run("dashboard.app:app", host="127.0.0.1", port=port)
