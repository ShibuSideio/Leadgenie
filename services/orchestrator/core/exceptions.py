"""
Sideio Lead Sniper — Centralized custom exception hierarchy.

Design contract:
  All service code raises typed exceptions from this module.
  Route handlers catch typed exceptions and map them to HTTP status codes.
  Bare ``except Exception`` is only used inside fire-and-forget daemon
  threads where propagation is structurally impossible.

Hierarchy:
    LeadSniperError
    ├── AuthError
    │   ├── TokenVerificationError
    │   └── AccountSuspendedError
    ├── QuotaError
    │   ├── QuotaExhaustedError
    │   └── ApprovalPendingError
    ├── DatabaseError
    │   ├── DatabaseTimeoutError
    │   └── TransactionConflictError
    ├── ExternalServiceError
    │   ├── SerperRateLimitError
    │   ├── VertexAITimeoutError
    │   └── SecretManagerError
    └── ValidationError
        └── SchemaViolationError
"""


class LeadSniperError(Exception):
    """Base exception for all Sideio Lead Sniper application errors."""

    def __init__(self, message: str, *, http_status: int = 500) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status


# ---------------------------------------------------------------------------
# Authentication & Authorization
# ---------------------------------------------------------------------------

class AuthError(LeadSniperError):
    """Base class for auth failures. Maps to HTTP 401."""

    def __init__(self, message: str) -> None:
        super().__init__(message, http_status=401)


class TokenVerificationError(AuthError):
    """Firebase ID token is invalid, expired, or malformed."""


class AccountSuspendedError(AuthError):
    """Account has been suspended by L0 Governance Protocol."""


class ForbiddenError(LeadSniperError):
    """Caller lacks required role (e.g. super_admin). Maps to HTTP 403."""

    def __init__(self, message: str = "Insufficient permissions.") -> None:
        super().__init__(message, http_status=403)


# ---------------------------------------------------------------------------
# Quota & Credits
# ---------------------------------------------------------------------------

class QuotaError(LeadSniperError):
    """Base class for credit/quota failures."""


class QuotaExhaustedError(QuotaError):
    """Tenant has no remaining credits. Maps to HTTP 402."""

    def __init__(self, message: str = "Beta quota exhausted. Contact admin to reload.") -> None:
        super().__init__(message, http_status=402)


class ApprovalPendingError(QuotaError):
    """Tenant account is pending L0 approval. Maps to HTTP 403."""

    def __init__(self, message: str = "Your application is under review.") -> None:
        super().__init__(message, http_status=403)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class DatabaseError(LeadSniperError):
    """Base class for Firestore / BigQuery failures."""


class DatabaseTimeoutError(DatabaseError):
    """A Firestore or BigQuery operation exceeded its deadline."""


class TransactionConflictError(DatabaseError):
    """A Firestore transaction was aborted due to contention (TOCTOU race)."""


# ---------------------------------------------------------------------------
# External Services
# ---------------------------------------------------------------------------

class ExternalServiceError(LeadSniperError):
    """Base class for third-party API failures."""


class SerperRateLimitError(ExternalServiceError):
    """Serper API responded with HTTP 429 after all retries exhausted."""


class VertexAITimeoutError(ExternalServiceError):
    """Vertex AI / Gemini call exceeded the configured wall-clock timeout."""


class SecretManagerError(ExternalServiceError):
    """Failed to retrieve a secret from GCP Secret Manager."""


# ---------------------------------------------------------------------------
# Payload Validation
# ---------------------------------------------------------------------------

class ValidationError(LeadSniperError):
    """Base class for payload validation failures. Maps to HTTP 400."""

    def __init__(self, message: str) -> None:
        super().__init__(message, http_status=400)


class SchemaViolationError(ValidationError):
    """Lead payload failed Pydantic LeadPayload schema validation."""
