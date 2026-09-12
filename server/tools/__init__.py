"""Tools package: registry + implementations + shared Context.

Importers should reach for:
  - TOOLS / IMPLS / tool_by_name / ollama_tool_specs — from registry
  - Context / ToolImpl                                — from context
"""

from .context import Context, ToolImpl
from .registry import IMPLS, TOOLS, ollama_tool_specs, tool_by_name

__all__ = [
    "Context",
    "IMPLS",
    "TOOLS",
    "ToolImpl",
    "ollama_tool_specs",
    "tool_by_name",
]
