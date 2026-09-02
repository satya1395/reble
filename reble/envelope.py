"""JSON envelope for --json output. Normative: spec section 5.

Additive changes only within a minor version; breaking envelope changes bump
the major version.
"""

from __future__ import annotations

import json
import sys

from . import __version__
from .errors import RebleError


def envelope(
    command: str,
    ok: bool,
    data: dict,
    branch: dict | None = None,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
) -> dict:
    return {
        "reble": __version__,
        "command": command,
        "ok": ok,
        "branch": branch or {},
        "data": data,
        "warnings": warnings or [],
        "errors": errors or [],
    }


def emit(env: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(env, indent=2, default=str))
    else:
        for key, value in env["data"].items():
            print(f"{key}: {value}")
        for w in env["warnings"]:
            print(f"WARN {w}", file=sys.stderr)
        for e in env["errors"]:
            print(f"ERROR {e}", file=sys.stderr)


def error_envelope(command: str, exc: RebleError) -> dict:
    return envelope(
        command,
        ok=False,
        data={},
        errors=[str(exc)],
    )
