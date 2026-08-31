class RebleError(Exception):
    """Base for all reble errors."""


class WriteGuardError(RebleError):
    """A write targeted a table outside the current branch's scope."""


class BranchError(RebleError):
    """Branch lifecycle problem (missing, duplicate, bad state)."""


class ProjectError(RebleError):
    """Project/config problem (not a reble project, bad reble.yml)."""
