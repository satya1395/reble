"""Event stream schema (SPEC v0.2).

Events are a library API first: the core (Runner, diff loop) calls an
injected ``on_event(name, **payload)`` callback; the CLI ``--events`` flag is
one adapter that serializes them as NDJSON on stdout. Event records carry an
``event`` field; the final JSON envelope does not — one self-describing
stream. Additive changes only within a major version of ``EVENTS_SCHEMA``.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable

from . import __version__

EVENTS_SCHEMA = "1"

EventCallback = Callable[..., None]


def event(command: str, name: str, **payload) -> dict:
    record = {
        "reble": __version__,
        "events": EVENTS_SCHEMA,
        "command": command,
        "event": name,
        "ts": round(time.time(), 3),
    }
    record.update(payload)
    return record


def ndjson_emitter(command: str, stream=None) -> EventCallback:
    """CLI adapter: one JSON object per line on stdout (envelope prints last)."""
    stream = stream or sys.stdout

    def emit(name: str, **payload) -> None:
        stream.write(json.dumps(event(command, name, **payload), default=str) + "\n")
        stream.flush()

    return emit


def noop_emitter() -> EventCallback:
    def emit(name: str, **payload) -> None:
        pass

    return emit
