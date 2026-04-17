"""
Pipeline-main — GCP client singleton factory.

Identical pattern to orchestrator/core/clients.py.

Phase 3 fix (M-5 / hot-loop audit):
  The Serper API key is fetched from Secret Manager ONCE at first call and
  cached in ``_serper_key_cache``.  Subsequent calls return the cached value.
  This eliminates the per-query Secret Manager RPC that was adding 20-100 ms
  of latency per Serper call in the scraping hot loop.
"""
from __future__ import annotations

import functools
import os
from typing import Optional

from google.cloud import firestore, bigquery, tasks_v2, secretmanager, storage
import vertexai

# ---------------------------------------------------------------------------
# Serper API key — module-level cache (Phase 3 M-5 fix)
# ---------------------------------------------------------------------------
_serper_key_cache: Optional[str] = None


def get_serper_key(secret_name: Optional[str] = None) -> str:
    """Return the Serper API key, fetching from Secret Manager on first call.

    The key is cached in a module-level variable for the lifetime of the
    container process.  This eliminates the per-query RPC that was adding
    latency in the scraping hot loop (M-5 audit finding).

    Args:
        secret_name: Full Secret Manager resource name.  Defaults to
            ``projects/{PROJECT_ID}/secrets/serper_api_key/versions/latest``.

    Returns:
        Serper API key string.

    Raises:
        SecretManagerError: If the Secret Manager call fails.
    """
    global _serper_key_cache
    if _serper_key_cache is not None:
        return _serper_key_cache

    from core.exceptions import SerperRateLimitError  # noqa: PLC0415 (local import OK here)
    _project = os.environ.get("PROJECT_ID", "sideio-leads-v16")
    _name = secret_name or f"projects/{_project}/secrets/serper_api_key/versions/latest"
    try:
        sm = get_secret_manager_client()
        response = sm.access_secret_version(request={"name": _name})
        _serper_key_cache = response.payload.data.decode("UTF-8").strip()
        return _serper_key_cache
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch Serper key from Secret Manager: {exc}") from exc


# ---------------------------------------------------------------------------
# Firestore
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=None)
def get_db() -> firestore.Client:
    """Return the shared Firestore client.

    Returns:
        :class:`google.cloud.firestore.Client`
    """
    return firestore.Client()


# ---------------------------------------------------------------------------
# BigQuery
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=None)
def get_bq_client() -> bigquery.Client:
    """Return the shared BigQuery client.

    Returns:
        :class:`google.cloud.bigquery.Client`
    """
    project = os.environ.get("PROJECT_ID", "sideio-leads-v16")
    return bigquery.Client(project=project)


# ---------------------------------------------------------------------------
# Cloud Tasks
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=None)
def get_tasks_client() -> tasks_v2.CloudTasksClient:
    """Return the shared Cloud Tasks client.

    Returns:
        :class:`google.cloud.tasks_v2.CloudTasksClient`
    """
    return tasks_v2.CloudTasksClient()


# ---------------------------------------------------------------------------
# Secret Manager
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=None)
def get_secret_manager_client() -> secretmanager.SecretManagerServiceClient:
    """Return the shared Secret Manager client.

    Returns:
        :class:`google.cloud.secretmanager.SecretManagerServiceClient`
    """
    return secretmanager.SecretManagerServiceClient()


# ---------------------------------------------------------------------------
# GCS
# ---------------------------------------------------------------------------
def get_gcs_client() -> storage.Client:
    """Return a GCS client (not cached — used infrequently).

    Returns:
        :class:`google.cloud.storage.Client`
    """
    return storage.Client()


# ---------------------------------------------------------------------------
# Vertex AI
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=None)
def init_vertex() -> None:
    """Initialise Vertex AI SDK (idempotent).

    Returns:
        None
    """
    vertexai.init(location="us-central1")
