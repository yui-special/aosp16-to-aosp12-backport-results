"""Patch Hunk Dependency Grouping Agent.

Analyses cross-hunk dependencies inside an original patch and groups hunks that
must be backported together.  This package never generates, modifies or applies
backport code; that remains the job of the downstream backport agent (PortGPT).
"""

from __future__ import annotations

__all__ = [
    "ANALYSIS_VERSION",
    "PACKAGE_VERSION",
    "AnalysisError",
    "PatchParseError",
    "SchemaError",
]

PACKAGE_VERSION = "1.0.0"
ANALYSIS_VERSION = "1.0"


class PatchHunkAgentError(Exception):
    """Base class for all errors raised by this package."""


class PatchParseError(PatchHunkAgentError):
    """Raised when a patch cannot be parsed into a trustworthy hunk inventory."""


class SchemaError(PatchHunkAgentError):
    """Raised when a record does not satisfy the documented schema."""


class AnalysisError(PatchHunkAgentError):
    """Raised when the dependency analysis could not produce verifiable output."""


from .patch_parser import Hunk, PatchDocument, PatchParser  # noqa: E402

__all__ += ["Hunk", "PatchDocument", "PatchParser", "PatchHunkAgentError"]
