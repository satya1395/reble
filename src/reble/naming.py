"""Git branch name → Iceberg ref name sanitization (spec sections 2 and 3)."""

from __future__ import annotations

import hashlib
import os
import re

_MAX_REF_LEN = 255


def sanitize_branch_name(name: str, replace: dict[str, str] | None = None) -> str:
    """Apply configured replacements, then conservative Iceberg-ref-safe cleanup.

    Iceberg ref names must be free of '/'-based ambiguity; we additionally
    collapse any remaining non [A-Za-z0-9_] characters to '_'.
    """
    replace = replace or {"/": "__", " ": "_"}
    out = name
    for old, new in replace.items():
        out = out.replace(old, new)
    out = re.sub(r"[^A-Za-z0-9_]", "_", out)
    out = re.sub(r"_+", "_", out).strip("_")
    if not out:
        out = "unnamed"
    return out[:_MAX_REF_LEN]


def disambiguate(ref: str, existing: set[str]) -> str:
    """If ref collides with an existing catalog ref, auto-suffix with user + hash.

    Two engineers on the same git branch get separate data branches
    (<branch>__<user-suffix>); never silently shared (spec section 2).
    """
    if ref not in existing:
        return ref
    user = re.sub(r"[^A-Za-z0-9]", "", os.environ.get("USER", "user")) or "user"
    digest = hashlib.sha256(f"{ref}:{user}".encode()).hexdigest()[:6]
    return f"{ref}__{user[:12]}{digest}"[:_MAX_REF_LEN]
