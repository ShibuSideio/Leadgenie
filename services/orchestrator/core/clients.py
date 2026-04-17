"""
Orchestrator — GCP client singleton factory.

All GCP SDK clients are initialised once (lazy) and reused across requests.
Business logic never instantiates clients directly — it calls the factory
functions here.  This makes dependency injection and unit testing possible:
tests monkeypatch ``get_db``, ``get_tasks_client``, etc. without touching
production code.

Thread-safety: ``functools.lru_cache(maxsize=None)`` is thread-safe in
CPython (the GIL ensures the first call wins and subsequent calls receive
the cached instance).  Each gunicorn worker process has its own cache.
"""
from __future__ import annotations

import functools
import os

import firebase_admin
from firebase_admin import credentials, auth, firestore as fb_firestore
from google.cloud import tasks_v2, bigquery, storage, secretmanager, kms
import vertexai


# ---------------------------------------------------------------------------
# Firebase Admin SDK — initialised once per process
# ---------------------------------------------------------------------------
if not firebase_admin._apps:
    firebase_admin.initialize_app()


@functools.lru_cache(maxsize=None)
def get_db():
    """Return the shared Firestore client (Firebase Admin SDK).

    Returns:
        :class:`google.cloud.firestore.Client` — authenticated via ADC.
    """
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
    """Return the shared BigQuery client.

    Returns:
        :class:`google.cloud.bigquery.Client`
    """
    project = os.environ.get("PROJECT_ID", "sideio-leads-v16")
    return bigquery.Client(project=project)


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
