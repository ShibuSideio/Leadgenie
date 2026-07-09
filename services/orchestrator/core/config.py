"""
Orchestrator — centralized configuration.

All environment variables and application constants live here.
Services import from this module; never call os.environ directly
in business logic.

Fernet hardening (Phase 3 fix):
  ENCRYPTION_KEY has no fallback default.  If unset the container fails
  fast at startup (raises ValueError) rather than silently using a
  key committed to the repository.
"""
from __future__ import annotations

import os
from cryptography.fernet import Fernet

# ---------------------------------------------------------------------------
# GCP Project
# ---------------------------------------------------------------------------
PROJECT_ID: str = os.environ["PROJECT_ID"]  # ENTERPRISE: NO FALLBACKS - FAIL FAST IF UNSET
LOCATION: str = os.environ.get("LOCATION", "asia-south1")

# ---------------------------------------------------------------------------
# Cloud Tasks
# ---------------------------------------------------------------------------
QUEUE: str = os.environ.get("QUEUE", "lead-pipeline-queue")
ORCHESTRATOR_URL: str = os.environ.get("ORCHESTRATOR_URL", "")
PIPELINE_URL: str = os.environ.get(
    "PIPELINE_URL",
    "https://lead-pipeline-main-abc.a.run.app/dispatch",
)
# Static SA email for OIDC task auth — set via ORCHESTRATOR_SA_EMAIL env var.
# Eliminates per-request blocking calls to http://metadata.google.internal.
# If unset, _oidc_task falls back to a 1-second metadata fetch with a hard timeout.
ORCHESTRATOR_SA_EMAIL: str = os.environ.get("ORCHESTRATOR_SA_EMAIL", "")

# ---------------------------------------------------------------------------
# Application limits
# ---------------------------------------------------------------------------
MAX_CHILD_CAMPAIGNS: int = int(os.environ.get("MAX_CHILD_CAMPAIGNS", "5"))
VELOCITY_THRESHOLD: int = int(os.environ["VELOCITY_THRESHOLD"])  # ENTERPRISE: NO FALLBACKS - FAIL FAST IF UNSET
OPS_CACHE_TTL: int = 300  # seconds — L0 telemetry TTLCache

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
ALLOWED_ORIGINS: list[str] = [
    "https://lead-sniper-prod.web.app",
    "https://lead-sniper-prod.firebaseapp.com",
]

# ---------------------------------------------------------------------------
# ROI / unit-economics defaults
# ---------------------------------------------------------------------------
ROI_DEFAULTS: dict[str, float | str] = {
    "avg_cpl": 50.0,          # USD — HubSpot 2024 benchmark
    "avg_deal_size": 0.0,     # must be set by tenant to unlock pipeline_value
    "sdr_hourly_rate": 15.0,  # USD/hr — BLS SDR median wage 2024
    "est_conversion_rate": 0.02,  # 2% — Salesforce benchmarks
    "currency": "USD",
}

# ---------------------------------------------------------------------------
# Fernet encryption — lazy initialization
#
# Phase 3 fix: raises ValueError on missing key instead of using a
# repository-committed fallback (H-3 / L-2 audit findings).
#
# Lazy pattern rationale:
#   Eager init (Fernet at module import) causes test failures when
#   ENCRYPTION_KEY is injected AFTER the first import (common in CI
#   and in Cloud Run when the module cache loads before env propagation).
#   Production containers always have the key set before any request
#   arrives, so lazy init has zero runtime cost difference.
# ---------------------------------------------------------------------------
_cipher_suite: Fernet | None = None


def get_cipher() -> Fernet:
    """Return the singleton Fernet cipher, initializing on first call.

    Raises:
        ValueError: If ENCRYPTION_KEY is not set or is an empty string.
    """
    global _cipher_suite
    if _cipher_suite is None:
        _raw_key: str | None = os.environ.get("ENCRYPTION_KEY")
        if not _raw_key:
            raise ValueError(
                "ENCRYPTION_KEY environment variable is not set. "
                "Deploy must supply this via --update-env-vars or Secret Manager."
            )
        _cipher_suite = Fernet(_raw_key.encode())
    return _cipher_suite


# Backwards-compatible alias — resolves lazily on attribute access
class _LazyCipher:
    """Descriptor that resolves CIPHER_SUITE lazily to avoid import-time failures."""
    def __get__(self, obj, objtype=None) -> Fernet:
        return get_cipher()


# CIPHER_SUITE is usable as: ``from core.config import CIPHER_SUITE``
# It resolves to the Fernet instance on first attribute access.
class _Config:
    CIPHER_SUITE = _LazyCipher()


_config_singleton = _Config()


# Make ``from core.config import CIPHER_SUITE`` work via module-level property
import sys as _sys


class _LazyModule(_sys.modules[__name__].__class__):
    @property
    def CIPHER_SUITE(self) -> Fernet:  # type: ignore[override]
        return get_cipher()


_sys.modules[__name__].__class__ = _LazyModule
```
```tool
TOOL_NAME: run_terminal_command
BEGIN_ARG: command
"Select-String -Path 'services/orchestrator/core/config.py' -Pattern 'PROJECT_ID: str = os\.environ\["PROJECT_ID"\]' | Select-Object -First 1"
