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
PROJECT_ID: str = os.environ.get("PROJECT_ID", "sideio-leads-v16")
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

# ---------------------------------------------------------------------------
# Application limits
# ---------------------------------------------------------------------------
MAX_CHILD_CAMPAIGNS: int = int(os.environ.get("MAX_CHILD_CAMPAIGNS", "5"))
VELOCITY_THRESHOLD: int = int(os.environ.get("VELOCITY_THRESHOLD", "10"))
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
# Fernet encryption
# Phase 3 fix: raises ValueError on missing key instead of using a
# repository-committed fallback (H-3 / L-2 audit findings).
# ---------------------------------------------------------------------------
_raw_fernet_key: str | None = os.environ.get("ENCRYPTION_KEY")
if not _raw_fernet_key:
    raise ValueError(
        "ENCRYPTION_KEY environment variable is not set. "
        "Deploy must supply this via --update-env-vars or Secret Manager."
    )
CIPHER_SUITE: Fernet = Fernet(_raw_fernet_key.encode())
