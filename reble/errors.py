"""Exit codes and error types. Normative: spec section 6."""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_DRIFT = 3
EXIT_PROMOTE_BLOCKED = 4
EXIT_EMPTY_SCOPE = 5
EXIT_LINEAGE = 6
EXIT_MISSING_KEY = 7
EXIT_INTERRUPTED = 130


class RebleError(Exception):
    """Base error carrying the spec exit code."""

    exit_code = EXIT_ERROR

    def __init__(self, message: str, exit_code: int | None = None):
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class ConfigError(RebleError):
    exit_code = EXIT_CONFIG


class DriftError(RebleError):
    exit_code = EXIT_DRIFT


class PromoteBlocked(RebleError):
    exit_code = EXIT_PROMOTE_BLOCKED


class EmptyScope(RebleError):
    exit_code = EXIT_EMPTY_SCOPE


class LineageError(RebleError):
    exit_code = EXIT_LINEAGE


class MissingDiffKey(RebleError):
    exit_code = EXIT_MISSING_KEY
