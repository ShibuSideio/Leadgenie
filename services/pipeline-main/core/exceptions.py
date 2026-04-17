"""
Pipeline-main — custom exception hierarchy.

Mirrors orchestrator/core/exceptions.py for pipeline-specific failures.
Pipeline services import from here; route handlers map to HTTP status codes.
"""


class LeadSniperError(Exception):
    """Base exception for all pipeline errors."""

    def __init__(self, message: str, *, http_status: int = 500) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status


class ValidationError(LeadSniperError):
    """Invalid request payload. Maps to HTTP 400."""

    def __init__(self, message: str) -> None:
        super().__init__(message, http_status=400)


class SchemaViolationError(ValidationError):
    """Lead payload failed Pydantic schema validation."""


class DatabaseTimeoutError(LeadSniperError):
    """Firestore or BigQuery operation exceeded its deadline."""


class SerperRateLimitError(LeadSniperError):
    """Serper API exhausted retries with HTTP 429."""


class VertexAITimeoutError(LeadSniperError):
    """Vertex AI / Gemini call exceeded 45-second wall-clock ceiling."""


class NegShieldTimeoutError(LeadSniperError):
    """Negative Signal Shield BigQuery fetch exceeded 3-second circuit breaker."""


class ConfidenceRouterTimeoutError(LeadSniperError):
    """Intent_Keywords confidence SUM query exceeded 3-second circuit breaker."""
