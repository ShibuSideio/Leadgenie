"""
Pipeline-main — centralized configuration.

All environment variables and constants live here.
Business logic never calls os.environ directly.

Phase 3 fix: ENCRYPTION_KEY raises ValueError on missing key (L-2 audit find).
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
# Cloud Tasks / Services
# ---------------------------------------------------------------------------
QUEUE: str = os.environ.get("QUEUE", "lead-pipeline-queue")
ORCHESTRATOR_URL: str = os.environ.get("ORCHESTRATOR_URL", "")
SCRAPER_HEAVY_URL: str = os.environ.get(
    "SCRAPER_HEAVY_URL",
    "https://scraper-heavy-abc.a.run.app/scrape",
)

# ---------------------------------------------------------------------------
# BigQuery
# ---------------------------------------------------------------------------
GCS_FIREHOSE_BUCKET: str = os.environ.get(
    "GCS_FIREHOSE_BUCKET", "sideio-raw-firehose-lake"
)
CB_WINDOW_MINUTES: int = int(os.environ.get("CB_WINDOW_MINUTES", "15"))
VELOCITY_THRESHOLD: int = int(os.environ.get("VELOCITY_THRESHOLD", "10"))

# ---------------------------------------------------------------------------
# Hybrid Starter Motor defaults
# ---------------------------------------------------------------------------
DEFAULT_CONFIDENCE_THRESHOLD: float = 1000.0
CONFIDENCE_BQ_TIMEOUT_S: float = 3.0    # hard ceiling on BQ confidence query
NEG_SHIELD_BQ_TIMEOUT_S: float = 3.0   # hard ceiling on Negative_Signals fetch
GEMINI_TIMEOUT_S: float = 45.0          # wall-clock ceiling on Vertex AI calls

# ---------------------------------------------------------------------------
# Serper secrets (key name in Secret Manager — resolved at runtime)
# ---------------------------------------------------------------------------
SERPER_API_KEY_NAME: str = (
    f"projects/{PROJECT_ID}/secrets/serper_api_key/versions/latest"
)

# ---------------------------------------------------------------------------
# Fernet encryption — fail-fast on missing key (Phase 3 fix L-2)
# ---------------------------------------------------------------------------
_raw_fernet_key: str | None = os.environ.get("ENCRYPTION_KEY")
if not _raw_fernet_key:
    raise ValueError(
        "ENCRYPTION_KEY environment variable is not set. "
        "Deploy must supply this via --update-env-vars or Secret Manager."
    )
CIPHER_SUITE: Fernet = Fernet(_raw_fernet_key.encode())
