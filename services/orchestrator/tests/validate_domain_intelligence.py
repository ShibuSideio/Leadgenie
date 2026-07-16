#!/usr/bin/env python3
"""Targeted validation for LeadGenie domain intelligence features.

Covers:
  - Auto infer_domain_profile on a realistic campaign
  - domain_override precedence (string + object)
  - Override clear → auto-inference fallback
  - Invalid override graceful handling
  - Domain query shaping
  - Pre-filter domain softening flag
  - Adaptive strictness_bias → threshold adjustment
  - build_domain_impact_summary shape
  - produce/dispatch wiring points (resolve + logging symbols)

Usage (from repo root):
  set PYTHONPATH=services/pipeline-main
  python services/orchestrator/tests/validate_domain_intelligence.py
"""
from __future__ import annotations

import ast
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Path bootstrap (same pattern as other orchestrator tests)
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]  # services/
PIPELINE_ROOT = ROOT / "pipeline-main"
REPO_ROOT = ROOT.parent
for p in (str(PIPELINE_ROOT), str(ROOT), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

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
        msg = f"{name}" + (f" — {detail}" if detail else "")
        FAILURES.append(msg)
        print(f"  FAIL  {msg}")


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"  WARN  {msg}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REALISTIC_REAL_ESTATE = {
    "name": "Muscat Villa Buyer Outreach",
    "bio": "We help expats find trusted real estate brokers for villas and apartments.",
    "effective_bio": "Property brokerage lead gen for residential buyers in Oman.",
    "pain_point": "Hard to find reliable property agents who respond quickly.",
    "keywords": "real estate, property broker, villa, apartment, bayut",
    "persona_name": "Expat home buyers",
    "persona_bio": "Families relocating to Muscat looking for long-term rentals or purchase.",
    "persona_targeting_signals": [
        "looking for property agent",
        "need villa near international school",
        "NOT jobs",
    ],
    "location": "Muscat, Oman",
    "sourcing_vector": "B2C",
}

SAAS_OVERRIDE_CAMPAIGN = {
    **REALISTIC_REAL_ESTATE,
    "domain_override": {"domain_family": "saas", "strictness_bias": 0.2},
}


def main() -> int:
    print("LeadGenie — Domain Intelligence Targeted Validation")
    print(f"Pipeline root: {PIPELINE_ROOT}")

    # ------------------------------------------------------------------
    section("1. Imports")
    # ------------------------------------------------------------------
    try:
        from services.domain_intelligence import (
            infer_domain_profile,
            resolve_campaign_domain_profile,
            validate_domain_override,
            expand_domain_override,
            apply_domain_query_profile,
            build_domain_impact_summary,
            domain_impact_for_scored_out,
            KNOWN_DOMAIN_FAMILIES,
            DOMAIN_PROFILE_VERSION,
        )
        from services.adaptive_policy import build_dispatch_policy
        from services.gemini_service import is_prefilter_domain_softening_active
        from services.lead_confidence import calculate_lead_confidence
        check("import domain_intelligence + adaptive_policy + gemini helpers", True)
    except Exception as exc:
        check("import domain intelligence stack", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        return _finish()

    # ------------------------------------------------------------------
    section("2. Auto domain inference (realistic campaign)")
    # ------------------------------------------------------------------
    try:
        profile = infer_domain_profile(REALISTIC_REAL_ESTATE)
        check("domain_family == real_estate", profile.get("domain_family") == "real_estate",
              str(profile.get("domain_family")))
        check("version is domain-v2", profile.get("version") == DOMAIN_PROFILE_VERSION,
              str(profile.get("version")))
        check("confidence in (0, 1]", 0 < float(profile.get("confidence") or 0) <= 1.0,
              str(profile.get("confidence")))
        check("liquidity_level == low (Oman)", profile.get("liquidity_level") == "low",
              str(profile.get("liquidity_level")))
        check("low_liquidity_market True", profile.get("low_liquidity_market") is True)
        check("preferred_sources non-empty list",
              isinstance(profile.get("preferred_sources"), list)
              and len(profile.get("preferred_sources") or []) > 0)
        check("preferred_query_hints present",
              isinstance(profile.get("preferred_query_hints"), list)
              and len(profile.get("preferred_query_hints") or []) > 0)
        check("strictness_bias is float in [-0.5, 0.5]",
              isinstance(profile.get("strictness_bias"), (int, float))
              and -0.5 <= float(profile["strictness_bias"]) <= 0.5,
              str(profile.get("strictness_bias")))
        check("notes present", bool(profile.get("notes")))
    except Exception as exc:
        check("auto inference ran without crash", False, str(exc))
        traceback.print_exc()

    # ------------------------------------------------------------------
    section("3. domain_override precedence")
    # ------------------------------------------------------------------
    try:
        # Object override
        p_obj, meta_obj = resolve_campaign_domain_profile(SAAS_OVERRIDE_CAMPAIGN)
        check("object override active", meta_obj.get("override_active") is True,
              str(meta_obj.get("source")))
        check("object override source=override", meta_obj.get("source") == "override")
        check("object override forces saas", p_obj.get("domain_family") == "saas",
              str(p_obj.get("domain_family")))
        check("object override confidence=1.0", float(p_obj.get("confidence") or 0) == 1.0)
        check("object override strictness_bias=0.2",
              abs(float(p_obj.get("strictness_bias") or 0) - 0.2) < 1e-6,
              str(p_obj.get("strictness_bias")))
        check("object override_active flag on profile", p_obj.get("override_active") is True)

        # String override
        camp_str = {**REALISTIC_REAL_ESTATE, "domain_override": "manufacturing"}
        p_str, meta_str = resolve_campaign_domain_profile(camp_str)
        check("string override active", meta_str.get("override_active") is True)
        check("string override forces manufacturing",
              p_str.get("domain_family") == "manufacturing",
              str(p_str.get("domain_family")))
        check("string override expands preferred_sources",
              isinstance(p_str.get("preferred_sources"), list)
              and len(p_str.get("preferred_sources") or []) > 0)

        # validate_domain_override string + object
        n1, e1 = validate_domain_override("healthcare")
        check("validate string healthcare", e1 is None and n1 == {"domain_family": "healthcare"})
        n2, e2 = validate_domain_override({
            "domain_family": "finance",
            "strictness_bias": 0.25,
            "liquidity_level": "high",
        })
        check("validate object finance", e2 is None and n2 is not None
              and n2.get("domain_family") == "finance")
    except Exception as exc:
        check("override precedence suite", False, str(exc))
        traceback.print_exc()

    # ------------------------------------------------------------------
    section("4. Clear override → auto-inference")
    # ------------------------------------------------------------------
    try:
        for clear_val, label in [
            (None, "None"),
            ({}, "empty dict"),
            ("", "empty string"),
            (False, "False"),
        ]:
            camp = {
                **REALISTIC_REAL_ESTATE,
                "domain_override": clear_val,
                "system_domain_profile": {
                    "version": DOMAIN_PROFILE_VERSION,
                    "domain_family": "saas",
                    "confidence": 1.0,
                    "override_active": True,
                    "strictness_bias": 0.2,
                    "liquidity_level": "high",
                    "preferred_sources": ["reddit"],
                    "preferred_query_hints": [],
                    "blocked_subreddits": [],
                    "low_liquidity_market": False,
                    "notes": "stale override cache",
                },
            }
            p, m = resolve_campaign_domain_profile(camp)
            check(
                f"clear({label}) → not override_active",
                m.get("override_active") is False,
                str(m.get("source")),
            )
            check(
                f"clear({label}) → re-infers real_estate",
                p.get("domain_family") == "real_estate",
                str(p.get("domain_family")),
            )
    except Exception as exc:
        check("clear override suite", False, str(exc))
        traceback.print_exc()

    # ------------------------------------------------------------------
    section("5. Invalid override graceful handling")
    # ------------------------------------------------------------------
    try:
        n, err = validate_domain_override({"domain_family": "spaceships"})
        check("invalid family rejected by validate", n is None and bool(err), str(err))

        n, err = validate_domain_override({"domain_family": "saas", "bogus_key": 1})
        check("unknown key rejected", n is None and bool(err), str(err))

        n, err = validate_domain_override({"strictness_bias": 0.1})  # missing family
        check("missing domain_family rejected", n is None and bool(err), str(err))

        camp_bad = {**REALISTIC_REAL_ESTATE, "domain_override": {"domain_family": "nope"}}
        p, m = resolve_campaign_domain_profile(camp_bad)
        check("invalid override does not crash resolve", True)
        check("invalid override falls back (not override_active)",
              m.get("override_active") is False, str(m))
        check("invalid override source marks fallback",
              m.get("source") == "inferred_invalid_override", str(m.get("source")))
        check("invalid override still yields a profile",
              isinstance(p, dict) and bool(p.get("domain_family")),
              str(p.get("domain_family")))
        # Should re-infer real estate from campaign text
        check("invalid override still auto-detects real_estate",
              p.get("domain_family") == "real_estate", str(p.get("domain_family")))

        # expand_domain_override on validated partial
        expanded = expand_domain_override({"domain_family": "construction"}, REALISTIC_REAL_ESTATE)
        check("expand fills version + sources",
              expanded.get("version") == DOMAIN_PROFILE_VERSION
              and expanded.get("override_active") is True
              and isinstance(expanded.get("preferred_sources"), list))
    except Exception as exc:
        check("invalid override suite", False, str(exc))
        traceback.print_exc()

    # ------------------------------------------------------------------
    section("6. Domain-aware query shaping")
    # ------------------------------------------------------------------
    try:
        re_profile = infer_domain_profile(REALISTIC_REAL_ESTATE)
        queries = [
            "site:reddit.com/r/frugal cheap villa muscat",
            "looking for property agent in Muscat",
            "site:reddit.com/r/Oman trusted broker",
        ]
        shaped = apply_domain_query_profile(
            queries,
            re_profile,
            location="Muscat, Oman",
            keywords="villa, apartment",
        )
        check("query shaping returns queries list", isinstance(shaped.get("queries"), list))
        check("blocked subreddit frugal dropped",
              int(shaped.get("dropped") or 0) >= 1
              and all("frugal" not in q.lower() for q in shaped["queries"]),
              f"dropped={shaped.get('dropped')}")
        check("domain_family echoed in result",
              shaped.get("domain_family") in (None, "real_estate", re_profile.get("domain_family")))

        # No profile → unchanged
        identity = apply_domain_query_profile(queries, None)
        check("no profile leaves queries unchanged",
              identity.get("queries") == queries
              and int(identity.get("dropped") or 0) == 0
              and int(identity.get("injected") or 0) == 0)

        # Manufacturing inject
        mfg_profile = {
            "domain_family": "manufacturing",
            "preferred_query_hints": ["site:indiamart.com"],
            "preferred_sources": ["serper_discovery"],
            "blocked_subreddits": [],
            "low_liquidity_market": False,
        }
        mfg = apply_domain_query_profile(
            ["need industrial equipment supplier"],
            mfg_profile,
            location="Pune, India",
            keywords="CNC machines",
        )
        check("manufacturing injects platform query",
              int(mfg.get("injected") or 0) >= 1
              and any("indiamart" in q.lower() for q in mfg.get("queries") or []),
              f"injected={mfg.get('injected')} queries={mfg.get('queries')}")
    except Exception as exc:
        check("query shaping suite", False, str(exc))
        traceback.print_exc()

    # ------------------------------------------------------------------
    section("7. Pre-filter domain softening + promotion strictness")
    # ------------------------------------------------------------------
    try:
        re_profile = infer_domain_profile(REALISTIC_REAL_ESTATE)
        soft = is_prefilter_domain_softening_active(re_profile)
        check("real_estate low-liquidity enables prefilter softening", soft is True)

        saas_like = {
            "domain_family": "saas",
            "strictness_bias": 0.15,
            "liquidity_level": "high",
            "low_liquidity_market": False,
            "preferred_sources": ["reddit", "hackernews"],
        }
        soft_saas = is_prefilter_domain_softening_active(saas_like)
        check("saas high-liquidity does not soften directories", soft_saas is False)

        soft_none = is_prefilter_domain_softening_active(None)
        check("no profile → softening False", soft_none is False)

        # Adaptive policy: negative bias lowers threshold_adjustment relative to none
        base_camp = {"bio": "Rich ICP context for scoring with clear buyer intent and depth"}
        pol_none = build_dispatch_policy(
            campaign=base_camp,
            sourcing_vector="B2B",
            queue_depth=10,
            recent_new_count=3,
            recent_enrichment_pending_count=0,
            velocity_threshold=10,
            domain_profile=None,
        )
        pol_lenient = build_dispatch_policy(
            campaign=base_camp,
            sourcing_vector="B2B",
            queue_depth=10,
            recent_new_count=3,
            recent_enrichment_pending_count=0,
            velocity_threshold=10,
            domain_profile={
                "domain_family": "real_estate",
                "strictness_bias": -0.3,
                "liquidity_level": "low",
                "low_liquidity_market": True,
            },
        )
        pol_strict = build_dispatch_policy(
            campaign=base_camp,
            sourcing_vector="B2B",
            queue_depth=10,
            recent_new_count=3,
            recent_enrichment_pending_count=0,
            velocity_threshold=10,
            domain_profile={
                "domain_family": "finance",
                "strictness_bias": 0.25,
                "liquidity_level": "high",
            },
        )
        check("policy version adaptive-v3", pol_lenient.get("policy_version") == "adaptive-v3")
        check("negative strictness_bias lowers threshold_adjustment",
              float(pol_lenient["threshold_adjustment"]) < float(pol_none["threshold_adjustment"]),
              f"lenient={pol_lenient['threshold_adjustment']} none={pol_none['threshold_adjustment']}")
        check("positive strictness_bias raises threshold_adjustment",
              float(pol_strict["threshold_adjustment"]) > float(pol_none["threshold_adjustment"]),
              f"strict={pol_strict['threshold_adjustment']} none={pol_none['threshold_adjustment']}")
        check("domain_threshold_delta present on policy",
              "domain_threshold_delta" in pol_lenient
              and float(pol_lenient["domain_threshold_delta"]) < 0)

        # Confidence gate consumes threshold_adjustment
        eval_mid = {
            "score": 6,
            "tier": "MEDIUM",
            "topic_coherence": 0.62,
            "pain_summary": "Need better pipeline",
        }
        text = "Need help improving lead flow and recommendations for tools"
        base_gate = calculate_lead_confidence(
            evaluation=eval_mid, text=text, url="https://forum.example.com/x",
            source_tier="Medium", threshold_adjustment=0.0,
        )
        relaxed_gate = calculate_lead_confidence(
            evaluation=eval_mid, text=text, url="https://forum.example.com/x",
            source_tier="Medium",
            threshold_adjustment=float(pol_lenient["threshold_adjustment"]),
        )
        check("lenient policy lowers confidence threshold",
              relaxed_gate["confidence_threshold"] < base_gate["confidence_threshold"],
              f"relaxed={relaxed_gate['confidence_threshold']} base={base_gate['confidence_threshold']}")
    except Exception as exc:
        check("prefilter + strictness suite", False, str(exc))
        traceback.print_exc()

    # ------------------------------------------------------------------
    section("8. build_domain_impact_summary")
    # ------------------------------------------------------------------
    try:
        re_profile = infer_domain_profile(REALISTIC_REAL_ESTATE)
        pol = build_dispatch_policy(
            campaign={"bio": REALISTIC_REAL_ESTATE["bio"]},
            sourcing_vector="B2C",
            queue_depth=2,
            recent_new_count=0,
            recent_enrichment_pending_count=0,
            velocity_threshold=10,
            domain_profile=re_profile,
        )
        summary = build_domain_impact_summary(
            re_profile,
            policy=pol,
            query_stats={"dropped": 1, "injected": 2, "boosted": 1, "reordered": True},
            prefilter_domain_softening=True,
            domain_tier_dropped=1,
            leads_promoted=3,
            leads_scored_out=4,
            cycle="dispatch",
        )
        expected_keys = {
            "cycle",
            "domain_family",
            "confidence",
            "liquidity_level",
            "strictness_bias",
            "threshold_adjustment",
            "domain_threshold_delta",
            "prefilter_domain_softening",
            "queries_dropped",
            "queries_injected",
            "queries_boosted",
            "queries_reordered",
            "leads_promoted",
            "leads_scored_out",
            "policy_mode",
            "policy_version",
        }
        missing = expected_keys - set(summary.keys())
        check("summary is dict", isinstance(summary, dict))
        check("summary has expected keys", not missing, f"missing={sorted(missing)}")
        check("summary domain_family matches", summary.get("domain_family") == "real_estate")
        check("summary cycle=dispatch", summary.get("cycle") == "dispatch")
        check("summary leads_promoted=3", summary.get("leads_promoted") == 3)
        check("summary queries_injected=2", summary.get("queries_injected") == 2)
        check("summary prefilter_domain_softening True",
              summary.get("prefilter_domain_softening") is True)

        compact = domain_impact_for_scored_out(summary)
        check("scored_out compact is non-empty subset",
              isinstance(compact, dict) and "domain_family" in compact
              and len(compact) <= len(summary))

        empty = build_domain_impact_summary(None, cycle="produce")
        check("empty profile still returns stable schema",
              isinstance(empty, dict) and empty.get("cycle") == "produce"
              and "domain_family" in empty)
    except Exception as exc:
        check("impact summary suite", False, str(exc))
        traceback.print_exc()

    # ------------------------------------------------------------------
    section("9. Produce / dispatch wiring (static + resolve call path)")
    # ------------------------------------------------------------------
    try:
        produce_path = PIPELINE_ROOT / "api" / "routers" / "produce.py"
        dispatch_path = PIPELINE_ROOT / "api" / "routers" / "dispatch.py"
        produce_src = produce_path.read_text(encoding="utf-8")
        dispatch_src = dispatch_path.read_text(encoding="utf-8")

        for label, src, needles in [
            ("produce", produce_src, [
                "resolve_campaign_domain_profile",
                "apply_domain_query_profile",
                "build_domain_impact_summary",
                "produce_domain_override_active",
                "produce_domain_impact_summary",
                "produce_domain_profile_loaded",
            ]),
            ("dispatch", dispatch_src, [
                "resolve_campaign_domain_profile",
                "build_domain_impact_summary",
                "domain_impact_for_scored_out",
                "is_prefilter_domain_softening_active",
                "dispatch_domain_override_active",
                "dispatch_domain_impact_summary",
                "domain_impact_summary",
                "build_dispatch_policy",
            ]),
        ]:
            for needle in needles:
                check(f"{label} references `{needle}`", needle in src)

        # AST parse routers
        for path in (produce_path, dispatch_path):
            try:
                ast.parse(path.read_text(encoding="utf-8"))
                check(f"AST parse {path.name}", True)
            except SyntaxError as se:
                check(f"AST parse {path.name}", False, str(se))

        # End-to-end resolve as produce/dispatch would call it
        for camp, expect_family, expect_override in [
            (REALISTIC_REAL_ESTATE, "real_estate", False),
            (SAAS_OVERRIDE_CAMPAIGN, "saas", True),
            ({**REALISTIC_REAL_ESTATE, "domain_override": "spaceships"}, "real_estate", False),
        ]:
            profile, meta = resolve_campaign_domain_profile(camp)
            check(
                f"resolve flow family={expect_family} override={expect_override}",
                profile.get("domain_family") == expect_family
                and bool(meta.get("override_active")) is expect_override,
                f"got family={profile.get('domain_family')} meta={meta}",
            )
            # Simulate impact summary at end of dispatch
            pol = build_dispatch_policy(
                campaign=camp,
                sourcing_vector=str(camp.get("sourcing_vector") or "B2B"),
                queue_depth=5,
                recent_new_count=1,
                recent_enrichment_pending_count=0,
                velocity_threshold=10,
                domain_profile=profile,
            )
            summary = build_domain_impact_summary(
                profile,
                policy=pol,
                prefilter_domain_softening=is_prefilter_domain_softening_active(profile),
                cycle="dispatch",
                leads_promoted=1,
                leads_scored_out=0,
            )
            check(
                f"dispatch-style summary for {expect_family}",
                summary.get("domain_family") == expect_family
                and summary.get("threshold_adjustment") is not None,
                f"family={summary.get('domain_family')} adj={summary.get('threshold_adjustment')}",
            )
    except Exception as exc:
        check("produce/dispatch wiring suite", False, str(exc))
        traceback.print_exc()

    # ------------------------------------------------------------------
    section("10. Edge notes / coverage gaps (informational)")
    # ------------------------------------------------------------------
    # Document known gaps without failing the run.
    warn(
        "No live HTTP produce/dispatch integration test — wiring verified via "
        "static source markers + resolve/policy call chain only."
    )
    warn(
        "pre_filter_gemini Gemini path not invoked (would need Vertex mock); "
        "softening flag + fallback noise path covered separately in unit smoke."
    )
    # SSOT drift check: shared.domain_constants must match what domain_intelligence re-exports
    try:
        from shared.domain_constants import (
            KNOWN_DOMAIN_FAMILIES as SHARED_FAMILIES,
            is_valid_domain_family as shared_is_valid,
        )
        check(
            "SSOT: domain_intelligence.KNOWN_DOMAIN_FAMILIES is shared set",
            KNOWN_DOMAIN_FAMILIES is SHARED_FAMILIES
            or set(KNOWN_DOMAIN_FAMILIES) == set(SHARED_FAMILIES),
            f"local={len(KNOWN_DOMAIN_FAMILIES)} shared={len(SHARED_FAMILIES)}",
        )
        check(
            "SSOT: is_valid_domain_family('real_estate')",
            shared_is_valid("real_estate") and not shared_is_valid("spaceships"),
        )
    except Exception as exc:
        check("SSOT domain_constants import", False, str(exc))
    warn(
        "Concurrent override + stale system_domain_profile with matching family "
        "may skip persist (should_persist=False) — intentional cache hit."
    )

    return _finish()


def _finish() -> int:
    total = PASSED + FAILED
    print("\n" + "=" * 64)
    print("DOMAIN INTELLIGENCE VALIDATION RESULTS")
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
        print("\n  Warnings / coverage gaps:")
        for w in WARNINGS:
            print(f"    - {w}")
    if FAILED == 0:
        print("\n  STATUS: GREEN — all targeted domain intelligence checks passed.")
        return 0
    print("\n  STATUS: RED — one or more domain intelligence checks failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
