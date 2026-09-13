"""Project-specific errors with stable public error codes."""

from __future__ import annotations


class InkscapeMCPError(RuntimeError):
    code = "operation_failed"


class DocumentNotFoundError(InkscapeMCPError):
    code = "document_not_found"


class ObjectNotFoundError(InkscapeMCPError):
    code = "object_not_found"


class RevisionConflictError(InkscapeMCPError):
    code = "revision_conflict"


class InvalidDrawingValueError(InkscapeMCPError):
    code = "invalid_value"


class PolicyDeniedError(InkscapeMCPError):
    code = "policy_denied"


class LiveBackendUnavailableError(InkscapeMCPError):
    code = "live_instance_unavailable"


class LiveActionUnsupportedError(InkscapeMCPError):
    code = "live_action_unsupported"


class RendererError(InkscapeMCPError):
    code = "renderer_failed"
