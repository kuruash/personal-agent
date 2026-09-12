"""Optional Langfuse integration.

The agent's answer path must work without the local observability stack.
When LANGFUSE_ENABLED=false, this module returns no-op clients/decorators
with the tiny surface the app uses.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def langfuse_enabled() -> bool:
    return os.environ.get("LANGFUSE_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


class _NoopObservation:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def update(self, **_kwargs) -> None:
        return None


class _NoopClient:
    def update_current_span(self, **_kwargs) -> None:
        return None

    def update_current_generation(self, **_kwargs) -> None:
        return None

    @contextmanager
    def start_as_current_observation(self, **_kwargs):
        yield _NoopObservation()


_NOOP_CLIENT = _NoopClient()


if langfuse_enabled():
    try:
        from langfuse import Langfuse, get_client, observe  # type: ignore
    except Exception:
        get_client = lambda: _NOOP_CLIENT  # type: ignore

        def observe(*_args, **_kwargs):  # type: ignore
            def decorator(fn: F) -> F:
                return fn
            return decorator

        Langfuse = None  # type: ignore
else:
    get_client = lambda: _NOOP_CLIENT  # type: ignore

    def observe(*_args, **_kwargs):  # type: ignore
        def decorator(fn: F) -> F:
            return fn
        return decorator

    Langfuse = None  # type: ignore


def init_langfuse() -> None:
    if not langfuse_enabled() or Langfuse is None:
        return
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    if not public_key or not secret_key:
        print("[OBSERVABILITY] Langfuse disabled: missing API keys", flush=True)
        return
    Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        host=os.environ.get("LANGFUSE_BASE_URL", "http://localhost:3000"),
    )
