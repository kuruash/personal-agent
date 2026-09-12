"""Interaction memory: SQLite + Ollama embeddings for past-turn recall."""

from .store import (
    DB_PATH,
    MIN_SIM,
    TOP_K,
    format_recall_for_prompt,
    log_interaction,
    recall,
)

__all__ = [
    "DB_PATH",
    "MIN_SIM",
    "TOP_K",
    "format_recall_for_prompt",
    "log_interaction",
    "recall",
]
