"""
Gunicorn server configuration — Pipeline Main (V23).

SF-008 FIX — gRPC Fork-Boundary Safety (post_fork hook):
  Gunicorn's pre-fork model (gthread or sync workers) forks worker processes
  AFTER the master process has imported all application code. Any gRPC channel
  opened before the fork is inherited by child processes as a DEAD file
  descriptor — the mutex protecting the channel is held by the parent thread
  which no longer exists, causing indefinite deadlocks (no Python timeout fires
  because the mutex is at the C-extension level below Python signal machinery).

  The V23 architecture already prevents pre-fork gRPC via:
    - threading.Lock DCL in core/clients.py (get_db, get_sm_client, etc.)
    - No gRPC at module scope (pyflakes-verified)
    - main_v23.py: app factory pattern (create_app()) defers all client init

  This post_fork hook provides a BELT-AND-SUSPENDERS guarantee: it explicitly
  resets all singleton handles in core.clients after the fork. Even if a future
  module accidentally opens a channel at import time, the post_fork hook will
  clear it before any request is served, forcing re-initialization in the worker.

Current worker model: 1 worker × 8 gthreads → no process fork occurs.
This hook is a no-op today but MUST be present before scaling to >1 worker.
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Gunicorn tuning
# ---------------------------------------------------------------------------
bind          = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
workers       = 1               # Cloud Run: single-process, multi-thread
threads       = 8               # gthread concurrency
worker_class  = "gthread"
timeout       = 120             # hard worker timeout (seconds)
graceful_timeout = 30           # SIGTERM grace period
loglevel      = "info"
accesslog     = "-"             # stdout → Cloud Logging
errorlog      = "-"             # stdout → Cloud Logging


# ---------------------------------------------------------------------------
# SF-008: Post-fork gRPC singleton reset
# ---------------------------------------------------------------------------

def post_fork(server, worker):                                       # noqa: ANN001
    """Reset all gRPC singleton handles in the forked worker process.

    Called by Gunicorn in the worker process immediately after fork().
    Any gRPC channel inherited from the master's address space is dead.
    Resetting the handles forces lazy re-initialization on first request.
    """
    try:
        import core.clients as _cc                                   # noqa: PLC0415
        _cc._db_instance          = None
        _cc._sm_instance          = None
        _cc._bq_instance          = None
        _cc._tasks_instance       = None
        _cc._vertex_initialised   = False
        _cc._serper_key_cache     = None
        server.log.info(
            f"[SF-008] post_fork: gRPC singletons reset in worker pid={worker.pid}. "
            "All clients will re-initialize on first request."
        )
    except Exception as exc:
        # Never let this block the worker from starting
        server.log.warning(f"[SF-008] post_fork reset failed (non-fatal): {exc}")
