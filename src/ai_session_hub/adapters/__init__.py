from __future__ import annotations

from typing import Callable, Iterable, Protocol

from ai_session_hub.models import LaunchSpec, SessionRef, SessionSnapshot, SourceSpec, Unavailable


class Adapter(Protocol):
    """Protocol for tool-specific transcript and resume adapters."""

    def discover(self, source: SourceSpec) -> Iterable[SessionRef]:
        """Discover session references from a source root."""
        ...

    def read(self, ref: SessionRef, cancelled: Callable[[], bool]) -> SessionSnapshot:
        """Read full session snapshot including messages and metadata."""
        ...

    def prepare_resume(self, ref: SessionRef) -> LaunchSpec | Unavailable:
        """Validate and prepare a foreground launch specification for resuming."""
        ...


_REGISTRY: dict[str, Adapter] = {}


def register_adapter(tool: str, adapter: Adapter) -> None:
    """Register an adapter instance for a tool name."""
    _REGISTRY[tool] = adapter


def _ensure_registered() -> None:
    # Importing one concrete adapter must not suppress registration of the rest.
    # Python caches these imports, so repeated lookups do not recreate adapters.
    from ai_session_hub.adapters import catalog, claude, codex, dsh, hermes, omp


def get_adapter(tool: str) -> Adapter | None:
    """Look up an adapter for a tool name."""
    _ensure_registered()
    return _REGISTRY.get(tool)


def registered_tools() -> list[str]:
    """List all registered tool names."""
    _ensure_registered()
    return sorted(_REGISTRY.keys())
