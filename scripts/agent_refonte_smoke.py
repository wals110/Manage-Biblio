#!/usr/bin/env python3
"""Smoke test de l'agent Refonte — Phase A.

Invoque directement build_diagnostic_graph().invoke() sur un profil et
affiche les métriques (durée, llm_calls, status) + le rapport markdown.

Usage :
    uv run python scripts/agent_refonte_smoke.py [profile] [max_llm_calls]

Exemples :
    uv run python scripts/agent_refonte_smoke.py default
    uv run python scripts/agent_refonte_smoke.py test 3

Charge .env manuellement (compatible avec le mécanisme de klodo.sh).
Réservé aux essais manuels — pas exécuté en CI (coûte ~$0.01-0.05 par run).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_env_file() -> None:
    """Parse .env à la racine du projet et exporte les paires non déjà set."""
    env_path = ROOT / ".env"
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


def main() -> int:
    _load_env_file()

    profile = sys.argv[1] if len(sys.argv) >= 2 else "default"
    max_llm_calls = int(sys.argv[2]) if len(sys.argv) >= 3 else 5

    if not os.environ.get("SILICONFLOW_API_KEY"):
        print("✗ SILICONFLOW_API_KEY absente — ajoute-la dans .env ou exporte-la.")
        return 1

    print(f"━━━ Smoke test agent Refonte — profil `{profile}` ━━━")
    print(f"  Modèle LLM : {os.environ.get('KLODO_AGENT_MODEL', 'deepseek-ai/DeepSeek-V3.2-Exp')}")
    print(f"  Budget LLM : {max_llm_calls} appels max")
    print()

    from agents.refonte import build_diagnostic_graph

    graph = build_diagnostic_graph(max_llm_calls=max_llm_calls)

    t0 = time.time()
    try:
        result = graph.invoke({"profile": profile})
    except Exception as exc:
        elapsed = time.time() - t0
        print(f"✗ CRASH après {elapsed:.1f}s : {type(exc).__name__}: {exc}")
        return 2
    elapsed = time.time() - t0

    print("━━━ Métriques ━━━")
    print(f"  Status      : {result.get('status')}")
    print(f"  LLM calls   : {result.get('llm_calls', 0)} / {max_llm_calls}")
    print(f"  Durée       : {elapsed:.1f}s")
    if result.get("error"):
        print(f"  Erreur      : {result['error']}")
    print()

    messages = result.get("messages", [])
    tool_calls = sum(
        1 for m in messages if hasattr(m, "tool_calls") and m.tool_calls
    )
    tool_results = sum(1 for m in messages if type(m).__name__ == "ToolMessage")
    print(f"  Tool calls (AI→tool) : {tool_calls}")
    print(f"  Tool results stockés : {tool_results}")
    print()

    print("━━━ Rapport markdown ━━━")
    print(result.get("report") or "(rapport vide)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
