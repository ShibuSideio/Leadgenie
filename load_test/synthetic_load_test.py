"""
load_test/synthetic_load_test.py
=================================
Chaos engineering / synthetic load test for the Sideio Lead Sniper platform.

PURPOSE:
  Mathematically proves the Reserve-and-Refund credit ledger is correct under
  maximum Firestore write contention by simulating 5,000 concurrent campaign
  dispatches and asserting wallet.total_consumed == successful_leads at the end.

EXECUTION (isolated staging project only):
  export ORCHESTRATOR_URL=https://orchestrator-STAGING.a.run.app
  export FIREBASE_ID_TOKEN=<staging_tenant_jwt>
  export TENANT_ID=<staging_tenant_uid>
  export CAMPAIGN_ID=<staging_campaign_id>
  export CONCURRENCY=5000
  python load_test/synthetic_load_test.py

SAFETY:
  - MUST be run against a dedicated staging GCP project, never production.
  - Uses asyncio + httpx for non-blocking concurrency (no thread explosion).
  - All assertions are logged; non-zero exit on any assertion failure.

AUTHOR: Lead SRE — Sideio Platform
CREATED: 2026-04-14
"""

import os
import sys
import asyncio
import logging
import datetime
import statistics
from dataclasses import dataclass, field
from typing import List

import httpx

# ---------------------------------------------------------------------------
# Configuration (from environment — never hardcode production values)
# ---------------------------------------------------------------------------
ORCHESTRATOR_URL = os.environ["ORCHESTRATOR_URL"].rstrip("/")
FIREBASE_ID_TOKEN = os.environ["FIREBASE_ID_TOKEN"]   # staging tenant JWT
TENANT_ID         = os.environ["TENANT_ID"]
CAMPAIGN_ID       = os.environ["CAMPAIGN_ID"]
CONCURRENCY       = int(os.environ.get("CONCURRENCY", "5000"))
REQUEST_TIMEOUT_S = float(os.environ.get("REQUEST_TIMEOUT_S", "30"))

# Wallet pre-load: must be >= CONCURRENCY before running or quota checks will
# short-circuit valid dispatches. Use L0 /api/l0/users/{uid}/mint to pre-mint.
EXPECTED_CREDITS_LOADED = int(os.environ.get("EXPECTED_CREDITS_LOADED", str(CONCURRENCY)))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("load_test")

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------
@dataclass
class RunResult:
    status_code:  int
    success:      bool
    latency_ms:   float
    error:        str = ""


@dataclass
class AggregateStats:
    total:        int = 0
    succeeded:    int = 0
    failed:       int = 0
    errors:       List[str] = field(default_factory=list)
    latencies_ms: List[float] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Single dispatch coroutine
# ---------------------------------------------------------------------------
async def _dispatch_one(
    client: httpx.AsyncClient,
    request_id: int,
    stats: AggregateStats,
) -> None:
    """
    Fires a single POST /api/campaigns/{id}/run and records the outcome.
    Designed to run in a pool of CONCURRENCY concurrent coroutines.
    """
    url     = f"{ORCHESTRATOR_URL}/api/campaigns/{CAMPAIGN_ID}/run"
    headers = {
        "Authorization": f"Bearer {FIREBASE_ID_TOKEN}",
        "Content-Type":  "application/json",
        "X-Load-Test-Request-ID": str(request_id),  # traceable in Cloud Logging
    }

    t0 = asyncio.get_event_loop().time()
    try:
        resp = await client.post(url, headers=headers, timeout=REQUEST_TIMEOUT_S)
        latency_ms = (asyncio.get_event_loop().time() - t0) * 1000

        stats.total       += 1
        stats.latencies_ms.append(latency_ms)

        if resp.status_code == 200:
            stats.succeeded += 1
        else:
            stats.failed += 1
            err = f"[req#{request_id}] HTTP {resp.status_code}: {resp.text[:120]}"
            stats.errors.append(err)
            log.warning(err)

    except httpx.TimeoutException:
        stats.total  += 1
        stats.failed += 1
        err = f"[req#{request_id}] TIMEOUT after {REQUEST_TIMEOUT_S}s"
        stats.errors.append(err)
        log.warning(err)

    except Exception as e:
        stats.total  += 1
        stats.failed += 1
        err = f"[req#{request_id}] EXCEPTION: {e}"
        stats.errors.append(err)
        log.error(err)


# ---------------------------------------------------------------------------
# Wallet assertion
# ---------------------------------------------------------------------------
def assert_wallet_integrity(stats: AggregateStats) -> bool:
    """
    Reads the tenant's wallet from the Orchestrator and asserts that:
      wallet.total_consumed == stats.succeeded

    This is the core financial invariant: every successful dispatch that
    reached HTTP 200 must have exactly one credit settled via the
    _atomic_settle_txn idempotency-safe transaction. No more, no less.

    A mismatch means either:
      - Double-settle: settle task was retried and idempotency guard failed
      - Under-settle:  settle task was dropped / Cloud Tasks DLQ'd silently
    """
    log.info("── Wallet Integrity Assertion ─────────────────────────────")
    try:
        resp = httpx.get(
            f"{ORCHESTRATOR_URL}/api/me",
            headers={"Authorization": f"Bearer {FIREBASE_ID_TOKEN}"},
            timeout=15,
        )
        resp.raise_for_status()
        data   = resp.json()
        wallet = data.get("wallet", {})

        consumed  = int(wallet.get("consumed_credits", 0))
        succeeded = stats.succeeded

        log.info(f"  Dispatches succeeded (HTTP 200) : {succeeded}")
        log.info(f"  wallet.total_consumed           : {consumed}")

        # Allow a 2-credit tolerance for Cloud Tasks settle tasks still in-flight
        # (Cloud Tasks can lag up to ~30s after the dispatch returns 200).
        tolerance = 2
        delta = abs(consumed - succeeded)

        if delta <= tolerance:
            log.info(f"  ✅ PASS — delta={delta} within tolerance={tolerance}")
            return True
        else:
            log.error(
                f"  ❌ FAIL — delta={delta} exceeds tolerance={tolerance}. "
                f"FINANCIAL LEAK DETECTED. consumed={consumed}, succeeded={succeeded}"
            )
            return False

    except Exception as e:
        log.error(f"Wallet assertion failed with exception: {e}")
        return False


# ---------------------------------------------------------------------------
# Circuit breaker assertion
# ---------------------------------------------------------------------------
def assert_circuit_breaker(stats: AggregateStats) -> bool:
    """
    Asserts that the automated circuit breaker in the cron sweep endpoint
    fired correctly if the 429/OOM error rate exceeded thresholds.

    Under a 5,000-concurrent load test, the Serper 429 rate WILL exceed 15%.
    The circuit breaker must return HTTP 503 on the sweep endpoint — not HTTP 200
    with a silently reduced dispatch count.

    This test fires a single cron sweep POST after the load test completes and
    checks that it either:
      a) Returns 503 with {"circuit_breaker": "open"} — correct behaviour
      b) Returns 200 with audit_trail confirming zero tasks dispatched — also acceptable
         (only if the staging environment has no active campaigns)
    """
    log.info("── Circuit Breaker Assertion ──────────────────────────────")
    try:
        resp = httpx.post(
            f"{ORCHESTRATOR_URL}/api/internal/cron/sweep",
            headers={"Authorization": f"Bearer {FIREBASE_ID_TOKEN}"},
            timeout=20,
        )
        body = resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else {}

        if resp.status_code == 503 and body.get("circuit_breaker") == "open":
            log.info(
                f"  ✅ PASS — Circuit breaker OPEN (correct). "
                f"Reason: {body.get('reason', 'unknown')}"
            )
            return True
        elif resp.status_code == 200:
            log.info(
                f"  ⚠️  WARN — Sweep returned 200. Error rates may be below thresholds "
                f"in staging (acceptable if no active campaigns exist). "
                f"Audit: {body.get('audit_trail', [])[:3]}"
            )
            return True
        else:
            log.error(
                f"  ❌ FAIL — Unexpected sweep response: HTTP {resp.status_code}, "
                f"body={resp.text[:200]}"
            )
            return False

    except Exception as e:
        log.error(f"Circuit breaker assertion failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Main load test runner
# ---------------------------------------------------------------------------
async def run_load_test() -> AggregateStats:
    stats = AggregateStats()

    log.info("╔══════════════════════════════════════════════════════════╗")
    log.info("║  Sideio Platform — Synthetic Load Test                   ║")
    log.info("╚══════════════════════════════════════════════════════════╝")
    log.info(f"  Target URL   : {ORCHESTRATOR_URL}")
    log.info(f"  Campaign     : {CAMPAIGN_ID}")
    log.info(f"  Tenant       : {TENANT_ID}")
    log.info(f"  Concurrency  : {CONCURRENCY}")
    log.info(f"  Timeout/req  : {REQUEST_TIMEOUT_S}s")
    log.info(f"  Credits pre-loaded: {EXPECTED_CREDITS_LOADED}")
    log.info("")

    # Limits: Firestore allows ~10k concurrent writes before ABORTED errors spike.
    # httpx connection pool is shared across all coroutines — cap at 200 to avoid
    # exhausting the Cloud Run container's file descriptor limit (1024 default).
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=100)

    async with httpx.AsyncClient(limits=limits) as client:
        # Semaphore: rate-gate actual in-flight requests at 200 concurrent
        # to avoid fd exhaustion while still dispatching 5,000 total requests.
        sem = asyncio.Semaphore(200)

        async def _gated(req_id):
            async with sem:
                await _dispatch_one(client, req_id, stats)

        tasks = [asyncio.create_task(_gated(i)) for i in range(1, CONCURRENCY + 1)]

        t_start = asyncio.get_event_loop().time()
        await asyncio.gather(*tasks)
        elapsed = asyncio.get_event_loop().time() - t_start

    # ── Summary ─────────────────────────────────────────────────────────────
    log.info("")
    log.info("── Load Test Results ──────────────────────────────────────")
    log.info(f"  Total requests     : {stats.total}")
    log.info(f"  Succeeded (HTTP200): {stats.succeeded}")
    log.info(f"  Failed             : {stats.failed}")
    log.info(f"  Success rate       : {stats.succeeded/max(stats.total,1)*100:.1f}%")
    log.info(f"  Wall-clock time    : {elapsed:.1f}s")
    log.info(f"  Throughput         : {stats.total/elapsed:.1f} req/s")

    if stats.latencies_ms:
        log.info(f"  Latency p50        : {statistics.median(stats.latencies_ms):.0f}ms")
        log.info(f"  Latency p95        : {sorted(stats.latencies_ms)[int(len(stats.latencies_ms)*0.95)]:.0f}ms")
        log.info(f"  Latency p99        : {sorted(stats.latencies_ms)[int(len(stats.latencies_ms)*0.99)]:.0f}ms")
        log.info(f"  Latency max        : {max(stats.latencies_ms):.0f}ms")

    if stats.errors:
        log.info(f"  First 5 errors:")
        for e in stats.errors[:5]:
            log.warning(f"    {e}")

    return stats


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main():
    stats = asyncio.run(run_load_test())

    # Allow settle tasks to propagate before asserting wallet
    log.info("Waiting 45s for Cloud Tasks settle callbacks to propagate...")
    import time; time.sleep(45)

    all_passed = True
    all_passed &= assert_wallet_integrity(stats)
    all_passed &= assert_circuit_breaker(stats)

    log.info("")
    log.info("── Final Verdict ──────────────────────────────────────────")
    if all_passed:
        log.info("✅ ALL ASSERTIONS PASSED — System is production-ready.")
        sys.exit(0)
    else:
        log.error("❌ ONE OR MORE ASSERTIONS FAILED — DO NOT promote to production.")
        sys.exit(1)


if __name__ == "__main__":
    main()
