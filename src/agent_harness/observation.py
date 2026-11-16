"""Optional, task-local observation hooks with no dashboard dependency."""

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
import logging


Observer = Callable[[str, dict], None]
_observer: ContextVar[Observer | None] = ContextVar("harness_observer", default=None)


@contextmanager
def observation(observer: Observer | None):
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def notify(kind: str, **payload) -> None:
    """A monitoring failure must never change an agent's outcome."""
    observer = _observer.get()
    if observer is not None:
        try:
            observer(kind, payload)
        except Exception:
            logging.getLogger(__name__).warning(
                "Agent observer failed; execution continues"
            )
