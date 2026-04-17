#!/usr/bin/env python3
"""
Sideio V23 Smoke Test Suite
============================
Runs all smoke test cases from the QA checklist against a target URL.

Usage:
    # Against local dev server (port 8080)
    python smoke_tests.py --url http://localhost:8080 --token $(gcloud auth print-identity-token)

    # Against Cloud Run preview revision
    python smoke_tests.py --url $PREVIEW_URL --token $TOKEN

    # Against production (read-only tests only)
    python smoke_tests.py --url $PROD_URL --token $TOKEN --readonly

Environment variables (alternative to CLI flags):
    SMOKE_URL    Target base URL
    SMOKE_TOKEN  Bearer token (Firebase ID token)
    TEST_TENANT  Tenant ID for write tests (optional, defaults to token tenant)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

# ── ANSI colours ─────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
RESET  = "\033[0m"
BOLD   = "\033[1m"


@dataclass
class TestResult:
    name: str
    passed: bool
    status_code: int         = 0
    expected_status: int     = 200
    detail: str              = ""
    response_body: Any       = None
    duration_ms: float       = 0.0
    findings: list[str]      = field(default_factory=list)


class SmokeTestRunner:
    """Executes all smoke test cases and collects results."""

    def __init__(self, base_url: str, token: str, readonly: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.token    = token
        self.readonly = readonly
        self.results: list[TestResult] = []
        self.client   = httpx.Client(timeout=15.0, follow_redirects=True)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type":  "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        token_override: str | None = None,
    ) -> tuple[int, Any, float]:
        """Execute HTTP request and return (status_code, body_dict, duration_ms)."""
        headers = {
            "Authorization": f"Bearer {token_override or self.token}",
            "Content-Type":  "application/json",
        }
        url = f"{self.base_url}{path}"
        start = time.monotonic()
        try:
            if method == "GET":
                resp = self.client.get(url, headers=headers)
            elif method == "PUT":
                resp = self.client.put(url, headers=headers, json=payload)
            else:
                resp = self.client.post(url, headers=headers, json=payload)
            duration_ms = (time.monotonic() - start) * 1000
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            return resp.status_code, body, duration_ms
        except httpx.ConnectError as exc:
            return 0, {"error": f"Connection refused: {exc}"}, 0.0
        except Exception as exc:
            return 0, {"error": str(exc)}, 0.0

    def run(self, name: str, case_fn) -> TestResult:
        """Execute a single test case function and record the result."""
        print(f"  {BLUE}▶{RESET}  {name} ...", end="", flush=True)
        result = case_fn()
        result.name = name
        self.results.append(result)
        status = f"{GREEN}PASS{RESET}" if result.passed else f"{RED}FAIL{RESET}"
        print(f"\r  {'✓' if result.passed else '✗'}  {name} [{status}] "
              f"(HTTP {result.status_code}, {result.duration_ms:.0f}ms)")
        if not result.passed and result.detail:
            print(f"      {YELLOW}↳ {result.detail}{RESET}")
        for finding in result.findings:
            print(f"      {YELLOW}ℹ {finding}{RESET}")
        return result

    # =========================================================================
    # ── TEST CASES ────────────────────────────────────────────────────────────
    # =========================================================================

    # ── T01: Health check — confirms V23 entrypoint is live ──────────────────
    def t01_health_check(self) -> TestResult:
        code, body, ms = self._request("GET", "/health")
        passed = (
            code == 200
            and isinstance(body, dict)
            and body.get("arch") == "modular"
        )
        findings = []
        if isinstance(body, dict) and body.get("arch") != "modular":
            findings.append(
                f"arch={body.get('arch')!r} — V22 legacy is answering, not V23. "
                "Entrypoint switch not applied."
            )
        return TestResult(
            name="", passed=passed,
            status_code=code, expected_status=200,
            detail="" if passed else f"body={body!r}",
            response_body=body, duration_ms=ms, findings=findings,
        )

    # ── T02: GET /api/me — auth_service + user fetch ─────────────────────────
    def t02_get_me(self) -> TestResult:
        code, body, ms = self._request("GET", "/api/me")
        passed = code == 200 and isinstance(body, dict) and "wallet" in body
        findings = []
        if isinstance(body, dict) and "wallet" in body:
            wallet = body["wallet"]
            findings.append(
                f"wallet.allocated={wallet.get('allocated_credits')}, "
                f"wallet.consumed={wallet.get('consumed_credits')}"
            )
        return TestResult(
            name="", passed=passed,
            status_code=code, expected_status=200,
            detail="" if passed else f"Missing 'wallet' in response: {body!r}",
            response_body=body, duration_ms=ms, findings=findings,
        )

    # ── T03: GET /api/campaigns — campaigns collection schema ────────────────
    def t03_get_campaigns(self) -> TestResult:
        code, body, ms = self._request("GET", "/api/campaigns")
        passed = code == 200 and isinstance(body, dict) and "data" in body
        findings = []
        if isinstance(body, dict) and "data" in body:
            items = body["data"]
            findings.append(f"Returned {len(items)} campaign(s)")
            if items:
                sample = items[0]
                required = {"id", "bio", "status", "tenant_id"}
                missing = required - set(sample.keys())
                if missing:
                    passed = False
                    findings.append(f"Schema gap: missing fields {missing}")
        return TestResult(
            name="", passed=passed,
            status_code=code, expected_status=200,
            detail="" if passed else f"body={body!r}",
            response_body=body, duration_ms=ms, findings=findings,
        )

    # ── T04: GET /api/leads?crm=false — dashboard feed ───────────────────────
    def t04_get_leads_dashboard(self) -> TestResult:
        code, body, ms = self._request("GET", "/api/leads?crm=false")
        passed = code == 200 and isinstance(body, dict) and "data" in body
        findings = []
        if isinstance(body, dict) and "data" in body:
            findings.append(f"Returned {len(body['data'])} lead(s) from dashboard feed")
        return TestResult(
            name="", passed=passed,
            status_code=code, expected_status=200,
            detail="" if passed else f"body={body!r}",
            response_body=body, duration_ms=ms, findings=findings,
        )

    # ── T05: GET /api/leads?crm=true — CRM board ─────────────────────────────
    def t05_get_leads_crm(self) -> TestResult:
        code, body, ms = self._request("GET", "/api/leads?crm=true")
        passed = code == 200 and isinstance(body, dict) and "data" in body
        findings = []
        if isinstance(body, dict) and "data" in body:
            findings.append(f"Returned {len(body['data'])} lead(s) from CRM board")
        return TestResult(
            name="", passed=passed,
            status_code=code, expected_status=200,
            detail="" if passed else f"body={body!r}",
            response_body=body, duration_ms=ms, findings=findings,
        )

    # ── T06: GET /api/analytics/roi?date_range=7 — ROI computation ───────────
    def t06_get_roi(self) -> TestResult:
        code, body, ms = self._request("GET", "/api/analytics/roi?date_range=7")
        passed = (
            code == 200
            and isinstance(body, dict)
            and "metrics" in body
            and "unit_economics" in body
        )
        findings = []
        if isinstance(body, dict) and "metrics" in body:
            m = body["metrics"]
            required_keys = {
                "n_approved", "n_contacted", "n_total_feed",
                "ad_savings", "labor_savings", "total_offset",
                "pipeline_value", "roi_ratio",
            }
            missing = required_keys - set(m.keys())
            if missing:
                passed = False
                findings.append(f"Metrics schema gap: missing {missing}")
            else:
                # Validate math: ad_savings = n_approved * avg_cpl
                ue = body.get("unit_economics", {})
                avg_cpl = float(ue.get("avg_cpl", 50.0))
                n_approved = int(m.get("n_approved", 0))
                expected_ad = round(n_approved * avg_cpl, 2)
                actual_ad = float(m.get("ad_savings", -1))
                if abs(expected_ad - actual_ad) > 0.01:
                    passed = False
                    findings.append(
                        f"Math error: ad_savings={actual_ad} ≠ expected {expected_ad} "
                        f"(n_approved={n_approved} × avg_cpl={avg_cpl})"
                    )
                else:
                    findings.append(
                        f"Math OK: n_approved={n_approved}, "
                        f"ad_savings={actual_ad}, labor_savings={m.get('labor_savings')}, "
                        f"roi_ratio={m.get('roi_ratio')}"
                    )
        return TestResult(
            name="", passed=passed,
            status_code=code, expected_status=200,
            detail="" if passed else f"body={body!r}",
            response_body=body, duration_ms=ms, findings=findings,
        )

    # ── T07: PUT /api/analytics/unit-economics — write + verify round-trip ───
    def t07_unit_economics_write(self) -> TestResult:
        if self.readonly:
            r = TestResult(name="", passed=True, status_code=0)
            r.findings = ["SKIPPED (--readonly flag)"]
            return r

        test_cpl = round(42.0 + (uuid.uuid4().int % 100) * 0.01, 2)  # unique value
        payload = {"avg_cpl": test_cpl, "sdr_hourly_rate": 18.0}
        code, body, ms = self._request("PUT", "/api/analytics/unit-economics", payload)
        if code != 200:
            return TestResult(
                name="", passed=False,
                status_code=code, expected_status=200,
                detail=f"PUT failed: {body!r}", duration_ms=ms,
            )

        # Immediately re-read and verify round-trip
        time.sleep(0.4)  # short Firestore propagation window
        code2, body2, ms2 = self._request("GET", "/api/analytics/roi?date_range=7")
        passed = False
        detail = ""
        findings: list[str] = []

        if code2 == 200 and isinstance(body2, dict):
            ue = body2.get("unit_economics", {})
            actual_cpl = float(ue.get("avg_cpl", -1))
            if abs(actual_cpl - test_cpl) < 0.01:
                passed = True
                findings.append(
                    f"Round-trip OK: wrote avg_cpl={test_cpl}, "
                    f"read back avg_cpl={actual_cpl}"
                )
            else:
                detail = (
                    f"avg_cpl mismatch: wrote {test_cpl}, "
                    f"read back {actual_cpl}"
                )
        else:
            detail = f"Re-read failed: HTTP {code2} {body2!r}"

        return TestResult(
            name="", passed=passed,
            status_code=code, expected_status=200,
            detail=detail, response_body=body2,
            duration_ms=ms + ms2, findings=findings,
        )

    # ── T08: 401 — invalid token → clean JSON error (not HTML 500) ───────────
    def t08_invalid_token_401(self) -> TestResult:
        code, body, ms = self._request(
            "GET", "/api/me",
            token_override="Bearer INVALID_TOKEN_FOR_SMOKE_TEST"
        )
        passed = (
            code == 401
            and isinstance(body, dict)
            and "error" in body
            and "<html" not in str(body)   # must NOT be an HTML stack trace
        )
        findings = []
        if isinstance(body, dict):
            findings.append(f"Error body: {body}")
        elif isinstance(body, str) and "<html" in body.lower():
            findings.append("CRITICAL: HTML stack trace leaked — not a structured JSON error!")
        return TestResult(
            name="", passed=passed,
            status_code=code, expected_status=401,
            detail="" if passed else (
                f"Expected HTTP 401 JSON, got HTTP {code}: {str(body)[:200]}"
            ),
            response_body=body, duration_ms=ms, findings=findings,
        )

    # ── T09: 403 — /api/l0/ with L1 token ────────────────────────────────────
    def t09_l0_forbidden_403(self) -> TestResult:
        code, body, ms = self._request("GET", "/api/l0/telemetry")
        # V22 legacy catch-all handles /api/l0/ — either 403 (correct) or
        # 404 (if legacy catch-all not wired) — both are acceptable here.
        # A 200 response would be a CRITICAL failure.
        passed = code in (401, 403) and code != 200
        findings = []
        if code == 200:
            findings.append("CRITICAL: L0 endpoint returned HTTP 200 to a non-super_admin token!")
        elif code in (401, 403):
            findings.append(
                f"Correctly blocked with HTTP {code} — "
                f"{'403 Forbidden' if code == 403 else '401 Unauthorized'}"
            )
        return TestResult(
            name="", passed=passed,
            status_code=code, expected_status=403,
            detail="" if passed else f"Expected 401 or 403, got HTTP {code}: {body!r}",
            response_body=body, duration_ms=ms, findings=findings,
        )

    # ── T10: V22 legacy route still serves (Strangler Fig integrity) ─────────
    def t10_legacy_route_still_live(self) -> TestResult:
        """Confirm an unmigrated V22 route (personas) still routes correctly."""
        code, body, ms = self._request("GET", "/api/personas")
        # Should not be a 404 (route lost) or 500 (shim crashed)
        # 200 or 401 means the legacy shim is correctly forwarding
        passed = code not in (404, 500, 0)
        findings = [
            f"/api/personas returned HTTP {code} — "
            f"{'legacy shim active ✓' if passed else 'shim failure ✗'}"
        ]
        return TestResult(
            name="", passed=passed,
            status_code=code, expected_status=200,
            detail="" if passed else f"Strangler Fig shim broken — got HTTP {code}",
            response_body=body, duration_ms=ms, findings=findings,
        )

    # =========================================================================
    # ── RUNNER ORCHESTRATION ─────────────────────────────────────────────────
    # =========================================================================

    def run_all(self) -> bool:
        """Execute the full smoke test suite.

        Returns:
            ``True`` if all tests passed (GO decision).
        """
        print(f"\n{BOLD}{'='*65}{RESET}")
        print(f"{BOLD}  Sideio V23 Smoke Test Suite{RESET}")
        print(f"{BOLD}  Target: {self.base_url}{RESET}")
        print(f"{BOLD}  Mode:   {'READ-ONLY' if self.readonly else 'FULL (reads + writes)'}{RESET}")
        print(f"{BOLD}{'='*65}{RESET}\n")

        print(f"{BOLD}── Phase 1: Read-Only Data Repository Validation ────────────{RESET}")
        self.run("T01  Health check (arch=modular confirms V23)",       self.t01_health_check)
        self.run("T02  GET /api/me — auth_service + wallet",            self.t02_get_me)
        self.run("T03  GET /api/campaigns — schema validation",         self.t03_get_campaigns)
        self.run("T04  GET /api/leads?crm=false — dashboard feed",      self.t04_get_leads_dashboard)
        self.run("T05  GET /api/leads?crm=true  — CRM board",           self.t05_get_leads_crm)
        self.run("T06  GET /api/analytics/roi   — math audit",          self.t06_get_roi)

        print(f"\n{BOLD}── Phase 2: State-Change (Write + Read-Back) ────────────────{RESET}")
        self.run("T07  PUT /api/analytics/unit-economics → round-trip", self.t07_unit_economics_write)

        print(f"\n{BOLD}── Phase 3: Exception Handling Audit ────────────────────────{RESET}")
        self.run("T08  Invalid token → HTTP 401 JSON (not HTML 500)",   self.t08_invalid_token_401)
        self.run("T09  L1 token on /api/l0/ → HTTP 403 Forbidden",      self.t09_l0_forbidden_403)
        self.run("T10  Unmigrated route via Strangler Fig shim",         self.t10_legacy_route_still_live)

        return self._print_summary()

    def _print_summary(self) -> bool:
        passed = [r for r in self.results if r.passed]
        failed = [r for r in self.results if not r.passed]

        print(f"\n{BOLD}{'='*65}{RESET}")
        print(f"{BOLD}  SMOKE TEST SUMMARY{RESET}")
        print(f"{BOLD}{'='*65}{RESET}")
        print(f"  Passed: {GREEN}{len(passed)}{RESET}/{len(self.results)}")
        print(f"  Failed: {RED}{len(failed)}{RESET}/{len(self.results)}")

        if failed:
            print(f"\n  {RED}{BOLD}FAILED TESTS:{RESET}")
            for r in failed:
                print(f"    {RED}✗ {r.name}{RESET}")
                if r.detail:
                    print(f"      {r.detail}")

        avg_ms = sum(r.duration_ms for r in self.results) / max(len(self.results), 1)
        print(f"\n  Avg response time: {avg_ms:.0f}ms")

        verdict = len(failed) == 0
        verdict_str = (
            f"\n  {GREEN}{BOLD}✅  VERDICT: GO — V23 is production-ready.{RESET}"
            if verdict else
            f"\n  {RED}{BOLD}🚫  VERDICT: NO-GO — {len(failed)} test(s) failed.{RESET}"
        )
        print(verdict_str)
        print(f"{BOLD}{'='*65}{RESET}\n")
        return verdict


def main() -> int:
    parser = argparse.ArgumentParser(description="Sideio V23 Smoke Tests")
    parser.add_argument("--url",      default=os.environ.get("SMOKE_URL", ""),
                        required=not os.environ.get("SMOKE_URL"),
                        help="Base URL of target service")
    parser.add_argument("--token",    default=os.environ.get("SMOKE_TOKEN", ""),
                        required=not os.environ.get("SMOKE_TOKEN"),
                        help="Firebase ID token (Bearer)")
    parser.add_argument("--readonly", action="store_true",
                        help="Skip write tests (safe against production)")
    args = parser.parse_args()

    runner = SmokeTestRunner(
        base_url=args.url,
        token=args.token,
        readonly=args.readonly,
    )
    go = runner.run_all()
    return 0 if go else 1


if __name__ == "__main__":
    sys.exit(main())
