#!/usr/bin/env python3
"""Targeted validation: Inbound Radar + domain intelligence.

Covers:
  1. Visitor beacon + domain profile → domain metadata stamped
  2. Visitor beacon + no profile → no extra domain fields (BC)
  3. Sentiment write threshold: high-confidence domain adjustment
  4. Sentiment write threshold: low-confidence (thin) milder adjustment
  5. Signal list floor respects intent_threshold_used

Usage (from repo root):
  set PYTHONPATH=services;services/orchestrator
  set PROJECT_ID=sideio-leads-v16
  set LOCATION=asia-south1
  set VELOCITY_THRESHOLD=10
  set ENCRYPTION_KEY=<test-fernet-key>
  python services/orchestrator/tests/validate_inbound_domain.py
"""
from __future__ import annotations

import ast
import os
import sys
import traceback
from pathlib import Path

# ---------------------------------------------------------------------------
# Path + env bootstrap
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]  # services/
ORCH = ROOT / "orchestrator"
REPO = ROOT.parent
for p in (str(ORCH), str(ROOT), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Minimal env so orchestrator core.config can import if job helpers are pulled in
os.environ.setdefault("PROJECT_ID", "sideio-leads-v16")
os.environ.setdefault("LOCATION", "asia-south1")
os.environ.setdefault("VELOCITY_THRESHOLD", "10")
os.environ.setdefault(
    "ENCRYPTION_KEY",
    "uNqG8Jc-44SjK22N8B5-2GksnE5F_88_V5wQZ02j1A0=",
)

PASSED = 0
FAILED = 0
WARNINGS: list[str] = []
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}" + (f" — {detail}" if detail else ""))
    else:
        FAILED += 1
        msg = name + (f" — {detail}" if detail else "")
        FAILURES.append(msg)
        print(f"  FAIL  {msg}")


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"  WARN  {msg}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


# Fixtures -----------------------------------------------------------------

HIGH_RE_PROFILE = {
    "version": "domain-v2",
    "domain_family": "real_estate",
    "confidence": 0.92,
    "profile_confidence": "high",
    "thin_campaign": False,
    "input_richness": "high",
    "strictness_bias": -0.30,
    "soft_domain_adjustments": False,
    "liquidity_level": "low",
    "low_liquidity_market": True,
    "override_active": False,
}

LOW_THIN_PROFILE = {
    "version": "domain-v2",
    "domain_family": "real_estate",
    "confidence": 0.42,
    "profile_confidence": "low",
    "thin_campaign": True,
    "input_richness": "low",
    "strictness_bias": -0.30,
    "soft_domain_adjustments": True,
    "liquidity_level": "low",
    "low_liquidity_market": True,
    "override_active": False,
}

STRICT_FINANCE_PROFILE = {
    "version": "domain-v2",
    "domain_family": "finance",
    "confidence": 0.88,
    "profile_confidence": "high",
    "thin_campaign": False,
    "strictness_bias": 0.25,
    "soft_domain_adjustments": False,
    "override_active": False,
}

LEGACY_BEACON_KEYS = {
    "tenant_id",
    "page_url",
    "referrer",
    "page_title",
    "screen_width",
    "visit_hash",
    "ip_hash",
    "created_at",
    "company_resolved",
    "company_name",
}

DOMAIN_BEACON_KEYS = {
    "domain_family",
    "domain_source",
    "profile_confidence",
    "thin_campaign",
    "strictness_bias",
    "enrichment_priority",
    "matched_campaign_id",
}


def _list_passes_intent_floor(s: dict) -> bool:
    """Mirror leads.py list_inbound_signals floor logic."""
    score = float(s.get("intent_score", 0.0) or 0.0)
    used = s.get("intent_threshold_used")
    if used is not None:
        try:
            return score >= float(used)
        except (TypeError, ValueError):
            pass
    return score >= 0.35


def main() -> int:
    print("LeadGenie — Inbound Radar Domain Integration Validation")
    print(f"Services root: {ROOT}")

    # ------------------------------------------------------------------
    section("0. Imports & wiring")
    # ------------------------------------------------------------------
    try:
        from shared.domain_gate import (
            compute_intent_threshold,
            extract_domain_meta,
            profile_confidence_label,
        )
        from api.routers.visitor_signals import (
            _build_domain_fields,
            _select_domain_profile_from_campaigns,
            _enrichment_priority_for_profile,
        )
        from jobs.inbound_sentiment_job import (
            MIN_INTENT_SCORE,
            GEMINI_MIN_INTENT_SCORE,
            _load_campaign_domain_profile,
        )
        from services.inbound_sentiment_service import InboundSentimentService
        check("import domain_gate + visitor helpers + job helpers", True)
    except Exception as exc:
        check("imports", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        return _finish()

    # Static wiring markers
    try:
        visitor_src = (ORCH / "api" / "routers" / "visitor_signals.py").read_text(
            encoding="utf-8"
        )
        job_src = (ORCH / "jobs" / "inbound_sentiment_job.py").read_text(encoding="utf-8")
        leads_src = (ORCH / "api" / "routers" / "leads.py").read_text(encoding="utf-8")
        svc_src = (ORCH / "services" / "inbound_sentiment_service.py").read_text(
            encoding="utf-8"
        )
        for label, src, needles in [
            ("visitor_signals", visitor_src, [
                "visitor_domain_profile_used",
                "visitor_domain_adjustment_applied",
                "compute_intent_threshold",
                "enrichment_priority",
                "_build_domain_fields",
            ]),
            ("inbound_sentiment_job", job_src, [
                "inbound_domain_profile_used",
                "inbound_domain_adjustment_applied",
                "compute_intent_threshold",
                "intent_threshold_used",
                "domain_family",
            ]),
            ("leads list/convert", leads_src, [
                "intent_threshold_used",
                "domain_family",
                "domain_source",
            ]),
            ("inbound_sentiment_service", svc_src, [
                "domain_profile",
                "gemini_min_intent_score",
                "extract_domain_meta",
            ]),
        ]:
            for n in needles:
                check(f"{label} references `{n}`", n in src)
        for path in (
            ORCH / "api" / "routers" / "visitor_signals.py",
            ORCH / "jobs" / "inbound_sentiment_job.py",
            ORCH / "api" / "routers" / "leads.py",
        ):
            try:
                ast.parse(path.read_text(encoding="utf-8"))
                check(f"AST parse {path.name}", True)
            except SyntaxError as se:
                check(f"AST parse {path.name}", False, str(se))
    except Exception as exc:
        check("static wiring suite", False, str(exc))

    # ------------------------------------------------------------------
    section("1. Visitor beacon WITH domain profile")
    # ------------------------------------------------------------------
    try:
        camps = [
            {
                "campaign_id": "camp_saas",
                "system_domain_profile": {
                    "domain_family": "saas",
                    "confidence": 0.5,
                    "profile_confidence": "medium",
                    "strictness_bias": 0.1,
                },
            },
            {
                "campaign_id": "camp_re",
                "system_domain_profile": {**HIGH_RE_PROFILE},
            },
        ]
        # Prefer high-confidence RE over medium SaaS
        prof, cid = _select_domain_profile_from_campaigns(camps)
        check("selects higher-confidence / preferred profile",
              cid == "camp_re" and (prof or {}).get("domain_family") == "real_estate",
              f"cid={cid} family={(prof or {}).get('domain_family')}")

        fields = _build_domain_fields(HIGH_RE_PROFILE, "camp_re")
        # Simulate beacon document merge
        beacon_doc = {
            "tenant_id": "t1",
            "page_url": "https://example.com/pricing",
            "referrer": "",
            "page_title": "Pricing",
            "screen_width": 1920,
            "visit_hash": "abc",
            "ip_hash": "def",
            "created_at": "now",
            "company_resolved": False,
            "company_name": None,
        }
        beacon_doc.update(fields)

        check("stamps domain_family", beacon_doc.get("domain_family") == "real_estate")
        check("stamps domain_source",
              beacon_doc.get("domain_source") in (
                  "system_domain_profile", "domain_override"
              ),
              str(beacon_doc.get("domain_source")))
        check("stamps profile_confidence=high",
              beacon_doc.get("profile_confidence") == "high")
        check("stamps strictness_bias",
              beacon_doc.get("strictness_bias") is not None
              and abs(float(beacon_doc["strictness_bias"]) - (-0.30)) < 1e-6)
        # real_estate is consumer-leaning firmographic → high conf demotes to medium
        check("stamps enrichment_priority for high-conf RE (medium firmographic)",
              beacon_doc.get("enrichment_priority") in ("high", "medium"),
              str(beacon_doc.get("enrichment_priority")))
        check("stamps enrichment_queue for downstream workers",
              beacon_doc.get("enrichment_queue") in ("realtime", "batch", "deferred"),
              str(beacon_doc.get("enrichment_queue")))
        check("stamps matched_campaign_id",
              beacon_doc.get("matched_campaign_id") == "camp_re")
        check("stamps domain_threshold_delta (observability)",
              "domain_threshold_delta" in beacon_doc)

        # Override wins
        camps2 = [
            {"campaign_id": "a", "system_domain_profile": HIGH_RE_PROFILE},
            {
                "campaign_id": "b",
                "system_domain_profile": {
                    **STRICT_FINANCE_PROFILE,
                    "override_active": True,
                    "confidence": 0.5,
                },
            },
        ]
        p2, c2 = _select_domain_profile_from_campaigns(camps2)
        check("override_active profile preferred",
              c2 == "b" and (p2 or {}).get("domain_family") == "finance",
              f"cid={c2}")

        # Thin profile enrichment priority low
        thin_fields = _build_domain_fields(LOW_THIN_PROFILE, "camp_thin")
        check("thin profile enrichment_priority=low",
              thin_fields.get("enrichment_priority") == "low",
              str(thin_fields.get("enrichment_priority")))
        check("thin profile profile_confidence=low",
              thin_fields.get("profile_confidence") == "low")
    except Exception as exc:
        check("visitor + profile suite", False, str(exc))
        traceback.print_exc()

    # ------------------------------------------------------------------
    section("2. Visitor beacon WITHOUT domain profile (BC)")
    # ------------------------------------------------------------------
    try:
        empty_fields = _build_domain_fields(None, None)
        check("no profile → empty domain field dict", empty_fields == {})

        empty_fields2 = _build_domain_fields({}, None)
        check("empty profile dict → no stamps", empty_fields2 == {})

        prof_none, cid_none = _select_domain_profile_from_campaigns(
            [{"campaign_id": "x", "name": "No profile"}]
        )
        check("campaigns without system_domain_profile → None",
              prof_none is None and cid_none is None)

        legacy_doc = {
            "tenant_id": "t1",
            "page_url": "https://example.com/",
            "referrer": "",
            "page_title": "Home",
            "screen_width": 1200,
            "visit_hash": "xyz",
            "ip_hash": "ip",
            "created_at": "now",
            "company_resolved": False,
            "company_name": None,
        }
        legacy_doc.update(empty_fields)
        extra = set(legacy_doc.keys()) - LEGACY_BEACON_KEYS
        check("BC: no domain keys on legacy beacon doc",
              not (extra & DOMAIN_BEACON_KEYS),
              f"extra_domain_keys={sorted(extra & DOMAIN_BEACON_KEYS)}")
        check("enrichment_priority default helper is medium when no profile",
              _enrichment_priority_for_profile(None) == "medium")
        # But we must not write it when no profile:
        check("BC: enrichment_priority not written without profile",
              "enrichment_priority" not in legacy_doc)

        # Explicit enrichment priority unit checks (shared helper)
        from shared.domain_gate import (
            compute_enrichment_priority,
            enrichment_plan_for_priority,
            enrichment_sort_key,
            should_run_company_resolve,
        )
        p_saas, m_saas = compute_enrichment_priority({
            "domain_family": "saas",
            "profile_confidence": "high",
            "thin_campaign": False,
            "confidence": 0.9,
        })
        check("high-conf SaaS → enrichment high", p_saas == "high", str(m_saas.get("reasons")))
        p_thin, _ = compute_enrichment_priority(LOW_THIN_PROFILE)
        check("thin RE → enrichment low", p_thin == "low")
        p_med_mfg, _ = compute_enrichment_priority({
            "domain_family": "manufacturing",
            "profile_confidence": "medium",
            "thin_campaign": False,
            "confidence": 0.5,
        })
        check("medium-conf manufacturing → high (B2B value)", p_med_mfg == "high")
        plan_low = enrichment_plan_for_priority("low")
        check("low plan defers company resolve",
              plan_low.get("resolve_company") is False
              and plan_low.get("queue") == "deferred")
        plan_high = enrichment_plan_for_priority("high")
        check("high plan resolves company + deep graph",
              plan_high.get("resolve_company") is True
              and plan_high.get("deep_graph") is True)
        check("should_run_company_resolve high",
              should_run_company_resolve("high") is True)
        check("should_run_company_resolve low skips when budget tight",
              should_run_company_resolve("low", budget_tight=True) is False)
        sorted_docs = sorted(
            [
                {"enrichment_priority": "low", "intent_score": 0.9, "signal_id": "a"},
                {"enrichment_priority": "high", "intent_score": 0.5, "signal_id": "b"},
                {"enrichment_priority": "medium", "intent_score": 0.8, "signal_id": "c"},
            ],
            key=enrichment_sort_key,
        )
        check("sort key processes high first",
              [d["signal_id"] for d in sorted_docs] == ["b", "c", "a"],
              str([d["signal_id"] for d in sorted_docs]))
    except Exception as exc:
        check("visitor BC suite", False, str(exc))
        traceback.print_exc()

    # ------------------------------------------------------------------
    section("3. Inbound sentiment — high-confidence threshold adjustment")
    # ------------------------------------------------------------------
    try:
        base = float(MIN_INTENT_SCORE)
        check("MIN_INTENT_SCORE base is 0.45", abs(base - 0.45) < 1e-9, str(base))
        check("GEMINI_MIN_INTENT_SCORE base is 0.30",
              abs(float(GEMINI_MIN_INTENT_SCORE) - 0.30) < 1e-9)

        t_none, m_none = compute_intent_threshold(base, None)
        check("no profile → threshold unchanged",
              abs(t_none - base) < 1e-9 and m_none.get("domain_applied") is False,
              f"t={t_none} meta={m_none}")

        t_high, m_high = compute_intent_threshold(
            base, HIGH_RE_PROFILE, floor=0.35, ceiling=0.60, bias_unit=0.12
        )
        check("high conf lenient bias lowers write floor",
              t_high < base and m_high.get("domain_applied") is True,
              f"base={base} effective={t_high} delta={m_high.get('threshold_delta')}")
        check("high conf scale is 1.0",
              abs(float(m_high.get("confidence_scale") or 0) - 1.0) < 1e-9)

        t_strict, m_strict = compute_intent_threshold(
            base, STRICT_FINANCE_PROFILE, floor=0.35, ceiling=0.60, bias_unit=0.12
        )
        check("high conf strict bias raises write floor",
              t_strict > base,
              f"base={base} effective={t_strict}")

        g_high, gm_high = compute_intent_threshold(
            float(GEMINI_MIN_INTENT_SCORE),
            HIGH_RE_PROFILE,
            floor=0.22,
            ceiling=0.42,
            bias_unit=0.08,
        )
        check("high conf gemini floor also adjusts",
              g_high < float(GEMINI_MIN_INTENT_SCORE),
              f"gemini_base=0.30 effective={g_high}")

        # Job profile loader
        camp_with = {
            "campaign_id": "c1",
            "system_domain_profile": HIGH_RE_PROFILE,
        }
        camp_without = {"campaign_id": "c2", "bio": "something"}
        check("job loads profile when present",
              _load_campaign_domain_profile(camp_with) is not None)
        check("job returns None when no profile",
              _load_campaign_domain_profile(camp_without) is None)

        # Service accepts domain_profile + gemini floor
        svc = InboundSentimentService(
            persona={"name": "p", "bio": "b", "pain_points": []},
            campaign={"campaign_id": "c1", "keywords": "property"},
            domain_profile=HIGH_RE_PROFILE,
            gemini_min_intent_score=g_high,
        )
        check("service stores domain_profile",
              (svc.domain_profile or {}).get("domain_family") == "real_estate")
        check("service uses domain-adjusted gemini floor",
              abs(float(svc.gemini_min_intent_score) - g_high) < 1e-9)

        # Simulate signal stamp like the job
        sig = {
            "signal_id": "s1",
            "intent_score": 0.43,
            "intent_label": "EXPRESSING_PAIN",
        }
        meta = extract_domain_meta(HIGH_RE_PROFILE)
        sig["intent_threshold_used"] = t_high
        sig["domain_family"] = meta["domain_family"]
        sig["domain_source"] = meta["domain_source"]
        sig["profile_confidence"] = meta["profile_confidence"]
        check("simulated high-conf signal carries domain meta",
              sig["domain_family"] == "real_estate"
              and sig["profile_confidence"] == "high"
              and sig["intent_threshold_used"] == t_high)
        # 0.43 should pass lowered threshold (~0.414)
        check("score 0.43 passes high-conf lenient threshold",
              float(sig["intent_score"]) >= float(sig["intent_threshold_used"]),
              f"score={sig['intent_score']} thr={sig['intent_threshold_used']}")
    except Exception as exc:
        check("sentiment high-confidence suite", False, str(exc))
        traceback.print_exc()

    # ------------------------------------------------------------------
    section("4. Inbound sentiment — low-confidence (thin) milder adjustment")
    # ------------------------------------------------------------------
    try:
        base = float(MIN_INTENT_SCORE)
        t_high, m_high = compute_intent_threshold(
            base, HIGH_RE_PROFILE, floor=0.35, ceiling=0.60, bias_unit=0.12
        )
        t_low, m_low = compute_intent_threshold(
            base, LOW_THIN_PROFILE, floor=0.35, ceiling=0.60, bias_unit=0.12
        )
        check("thin profile_confidence label is low",
              profile_confidence_label(LOW_THIN_PROFILE) == "low")
        check("thin confidence_scale is 0.3",
              abs(float(m_low.get("confidence_scale") or 0) - 0.3) < 1e-9,
              str(m_low.get("confidence_scale")))
        check("thin adjustment milder than high (closer to base)",
              abs(t_low - base) < abs(t_high - base),
              f"high_delta={m_high.get('threshold_delta')} low_delta={m_low.get('threshold_delta')}")
        check("thin still slightly lenient for negative bias",
              t_low < base,
              f"t_low={t_low} base={base}")

        g_high, _ = compute_intent_threshold(
            0.30, HIGH_RE_PROFILE, floor=0.22, ceiling=0.42, bias_unit=0.08
        )
        g_low, _ = compute_intent_threshold(
            0.30, LOW_THIN_PROFILE, floor=0.22, ceiling=0.42, bias_unit=0.08
        )
        check("thin gemini floor milder than high",
              abs(g_low - 0.30) < abs(g_high - 0.30),
              f"g_high={g_high} g_low={g_low}")

        # Service BC without profile
        svc_bc = InboundSentimentService(
            persona={"name": "p", "bio": "b", "pain_points": []},
            campaign={"campaign_id": "c0"},
        )
        check("service BC: domain_profile is None", svc_bc.domain_profile is None)
        check("service BC: gemini floor stays 0.30",
              abs(float(svc_bc.gemini_min_intent_score) - 0.30) < 1e-9)
    except Exception as exc:
        check("sentiment thin suite", False, str(exc))
        traceback.print_exc()

    # ------------------------------------------------------------------
    section("5. Signal list API respects intent_threshold_used")
    # ------------------------------------------------------------------
    try:
        # Domain-lenient signal: score 0.42, threshold used 0.414 → keep
        s_keep = {
            "status": "new",
            "intent_score": 0.42,
            "intent_threshold_used": 0.414,
            "domain_family": "real_estate",
        }
        # Below domain threshold → drop
        s_drop = {
            "status": "new",
            "intent_score": 0.40,
            "intent_threshold_used": 0.414,
        }
        # No threshold_used, score 0.40 → passes default floor 0.35
        s_legacy_ok = {
            "status": "new",
            "intent_score": 0.40,
        }
        # No threshold_used, score 0.30 → fails default 0.35
        s_legacy_drop = {
            "status": "new",
            "intent_score": 0.30,
        }
        # Wrong status filtered elsewhere; still document floor helper
        check("list keeps domain-lenient signal at 0.42 vs thr 0.414",
              _list_passes_intent_floor(s_keep) is True)
        check("list drops signal below its intent_threshold_used",
              _list_passes_intent_floor(s_drop) is False)
        check("list keeps legacy 0.40 without threshold_used (floor 0.35)",
              _list_passes_intent_floor(s_legacy_ok) is True)
        check("list drops legacy 0.30 without threshold_used",
              _list_passes_intent_floor(s_legacy_drop) is False)

        # Strict domain: score 0.46 must pass thr 0.48? No
        s_strict = {
            "status": "new",
            "intent_score": 0.46,
            "intent_threshold_used": 0.48,
        }
        check("list drops below strict domain threshold",
              _list_passes_intent_floor(s_strict) is False)
        s_strict_ok = {**s_strict, "intent_score": 0.50}
        check("list keeps at/above strict domain threshold",
              _list_passes_intent_floor(s_strict_ok) is True)

        # Full filter pass like list endpoint
        batch = [s_keep, s_drop, s_legacy_ok, s_legacy_drop, s_strict, s_strict_ok]
        kept = [
            s for s in batch
            if s.get("status") == "new" and _list_passes_intent_floor(s)
        ]
        check("list batch keeps expected 3 signals",
              len(kept) == 3,
              f"kept_scores={[s['intent_score'] for s in kept]}")
    except Exception as exc:
        check("signal list floor suite", False, str(exc))
        traceback.print_exc()

    # ------------------------------------------------------------------
    section("6. Gaps / notes")
    # ------------------------------------------------------------------
    warn(
        "No live HTTP/Firestore integration — validation uses pure helpers + "
        "source wiring markers (no Serper/Gemini/Firestore)."
    )
    warn(
        "Visitor path does not invent intent scores; domain bias drives "
        "enrichment_priority + metadata only."
    )
    warn(
        "Maps inbound signals inherit domain stamps in the job loop; "
        "InboundMapsService itself is not domain-aware internally."
    )

    return _finish()


def _finish() -> int:
    total = PASSED + FAILED
    print("\n" + "=" * 64)
    print("INBOUND RADAR DOMAIN VALIDATION RESULTS")
    print("=" * 64)
    print(f"  Total checks : {total}")
    print(f"  Passed       : {PASSED}")
    print(f"  Failed       : {FAILED}")
    print(f"  Warnings     : {len(WARNINGS)}")
    if FAILURES:
        print("\n  Failures:")
        for f in FAILURES:
            print(f"    - {f}")
    if WARNINGS:
        print("\n  Warnings / gaps:")
        for w in WARNINGS:
            print(f"    - {w}")
    if FAILED == 0:
        print("\n  STATUS: GREEN — all inbound domain integration checks passed.")
        return 0
    print("\n  STATUS: RED — one or more inbound domain checks failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
