"""
Orchestrator — GCP client singleton factory.

All GCP SDK clients are initialised once (lazy) and reused across requests.
Business logic never instantiates clients directly — it calls the factory
functions here.  This makes dependency injection and unit testing possible:
tests monkeypatch ``get_db``, ``get_tasks_client``, etc. without touching
production code.

Thread-safety (V23 Task 2 hardening):
  ``functools.lru_cache(maxsize=None)`` is GIL-safe for the cache *lookup*,
  but two threads can simultaneously observe a cache miss and both call the
  gRPC constructor under Gunicorn's gthread worker class.

  ``firebase_admin.initialize_app()`` is wrapped in ``_ensure_firebase_init()``
  using threading.Lock double-checked locking so it is called exactly once
  per process and NEVER at module-import time (which is the pre-fork leak).
  All other heavy clients retain lru_cache — they are not gRPC-stub-based
  constructors and the GIL provides sufficient protection.
"""
from __future__ import annotations

import functools
import os
import threading

import firebase_admin
from firebase_admin import credentials, auth, firestore as fb_firestore
from google.cloud import tasks_v2, bigquery, storage, secretmanager, kms
import vertexai


# ---------------------------------------------------------------------------
# Firebase Admin SDK — lazy, thread-safe initialisation (V23 Task 2 fix)
#
# PREVIOUS BUG: ``firebase_admin.initialize_app()`` was called at module scope
# (lines 28–29 of the original file).  This runs at Blueprint import time,
# BEFORE Gunicorn forks workers, creating a gRPC stub in the master process.
# Child worker processes inherit a copy-on-write view of the open file
# descriptors, causing mutex contention / deadlock on the first actual RPC.
#
# FIX: Wrap in double-checked locking so the SDK is initialised exactly once,
# lazily, inside the first call to get_db() within a worker process.
# ---------------------------------------------------------------------------
_firebase_init_lock: threading.Lock = threading.Lock()


def _ensure_firebase_init() -> None:
    """Initialise Firebase Admin SDK exactly once per process (thread-safe)."""
    if not firebase_admin._apps:
        with _firebase_init_lock:
            # Re-check inside lock — another thread may have won the race.
            if not firebase_admin._apps:
                firebase_admin.initialize_app()


@functools.lru_cache(maxsize=None)
def get_db():
    """Return the shared Firestore client (Firebase Admin SDK).

    Calls ``_ensure_firebase_init()`` to guarantee lazy, thread-safe SDK
    initialisation before constructing the Firestore client.

    Returns:
        :class:`google.cloud.firestore.Client` — authenticated via ADC.
    """
    _ensure_firebase_init()
    return fb_firestore.client()


@functools.lru_cache(maxsize=None)
def get_tasks_client() -> tasks_v2.CloudTasksClient:
    """Return the shared Cloud Tasks client.

    Returns:
        :class:`google.cloud.tasks_v2.CloudTasksClient`
    """
    return tasks_v2.CloudTasksClient()


@functools.lru_cache(maxsize=None)
def get_bq_client() -> bigquery.Client:
    """Return the shared BigQuery client, pinned to asia-south1.

    REGIONALITY FIX (2026-04-28):
    Without an explicit location the BQ SDK defaults to US, causing
    "Not found" (Code 5) errors for any dataset provisioned in asia-south1.
    The location param here fixes ALL queries that route through this singleton.

    Returns:
        :class:`google.cloud.bigquery.Client`
    """
    project = os.environ.get("PROJECT_ID", "sideio-leads-v16")
    return bigquery.Client(project=project, location="asia-south1")


@functools.lru_cache(maxsize=None)
def get_secret_manager_client() -> secretmanager.SecretManagerServiceClient:
    """Return the shared Secret Manager client.

    Returns:
        :class:`google.cloud.secretmanager.SecretManagerServiceClient`
    """
    return secretmanager.SecretManagerServiceClient()


@functools.lru_cache(maxsize=None)
def get_kms_client() -> kms.KeyManagementServiceClient:
    """Return the shared KMS client.

    Returns:
        :class:`google.cloud.kms.KeyManagementServiceClient`
    """
    return kms.KeyManagementServiceClient()


def get_gcs_client() -> storage.Client:
    """Return a GCS client.  Not cached — GCS operations are infrequent.

    Returns:
        :class:`google.cloud.storage.Client`
    """
    return storage.Client()


# ---------------------------------------------------------------------------
# Vertex AI — initialised once per process
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=None)
def init_vertex() -> None:
    """Initialise the Vertex AI SDK (idempotent; call before any model use).

    Returns:
        None
    """
    vertexai.init(location="us-central1")


# ---------------------------------------------------------------------------
# Aliases — shorter names used by some routers (e.g. social_redirect.py)
# ---------------------------------------------------------------------------
get_sm_client = get_secret_manager_client

