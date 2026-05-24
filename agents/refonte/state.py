"""État de l'agent Refonte — partagé entre les phases A/B/C.

Hérite de `MessagesState` (LangGraph) pour gérer nativement la conversation
agent ↔ tools (chaque tool call et tool response sont ajoutés à `messages`).

Champs métier ajoutés par-dessus :
  - profile  : nom du profil cible
  - phase    : "A" | "B" | "C"
  - run_id   : UUID4 unique pour la run (clé .cache/refonte/<run_id>/)
  - status   : "pending" | "running" | "done" | "error"
  - error    : message d'erreur si status=="error"
  - report   : markdown final produit en Phase A (write_report node)
  - llm_calls: compteur d'appels LLM pour faire respecter le budget
"""

from langgraph.graph import MessagesState


class RefonteState(MessagesState, total=False):
    """État partagé entre les nœuds du graphe de refonte.

    `MessagesState` apporte `messages: list[BaseMessage]` avec reducer
    `add_messages` qui concatène + dédoublonne sur id.
    """

    profile: str
    phase: str
    run_id: str
    status: str
    error: str
    report: str
    llm_calls: int
