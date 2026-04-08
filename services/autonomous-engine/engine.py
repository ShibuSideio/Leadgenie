"""
engine.py - V16 Autonomous Engine: Triangulation, Dedup, Token Bucket, Gemini Dossier
=======================================================================================
Phase 4 changes:
  - Write destination: users/{tenant_id}/predictive_cache (72h TTL) NOT leads collection
  - Discovery injection: 15% of token budget reserved for explore domains
    (baseline_weight < 1.0 OR total_yield == 0) with relaxed threshold 1.4
"""

import os
import json
import hashlib
import logging
import datetime
from urllib.parse import urlparse
from typing import Optional, Tuple, List, Dict

import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
from google.cloud import firestore
from pydantic import ValidationError

from models import LeadPayload
from ingestors import JobBoardIngestor, FundingIngestor

log = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("PROJECT_ID", "sideio-leads-v16")
GEMINI_MODEL = "gemini-2.5-flash"

# ── Routing thresholds ─────────────────────────────────────────────────────────
EXPLOIT_THRESHOLD   = 1.8   # standard: both signals + neutral ontology
EXPLORE_THRESHOLD   = 1.4   # relaxed: discovery partition only

# ── Token budgets ──────────────────────────────────────────────────────────────
MAX_DAILY_TOKENS      = 5000
TOKENS_PER_CALL       = 500
DISCOVERY_ALLOCATION  = float(os.environ.get("DISCOVERY_ALLOCATION", "0.15"))
EXPLORE_TOKEN_BUDGET  = int(MAX_DAILY_TOKENS * DISCOVERY_ALLOCATION)  # 750

# ── Cache TTL ──────────────────────────────────────────────────────────────────
CACHE_TTL_HOURS  = 72
DEDUP_WINDOW_DAYS = 60

_SOCIAL_ONTOLOGY_DOMAINS = {
    "reddit.com","facebook.com","linkedin.com","quora.com",
    "kaggle.com","instagram.com","twitter.com","x.com","youtube.com"
}

vertexai.init(project=PROJECT_ID, location="us-central1")


def parse_base_path(url: str) -> str:
    try:
        parsed  = urlparse(url if url.startswith("http") else f"https://{url}")
        domain  = (parsed.hostname or "").removeprefix("www.")
        if not domain:
            return "unknown"
        if any(domain.endswith(s) for s in _SOCIAL_ONTOLOGY_DOMAINS):
            segs = [s for s in parsed.path.split("/") if s]
            return "/".join([domain] + segs[:2])
        return domain
    except Exception:
        return "unknown"


def _scale_ui_score(final_score: float) -> int:
    """
    Maps triangulation score to UI score (0-10).
    1.8 (min exploit threshold) -> 7
    2.0 (both signals + neutral weight) -> 7
    2.6 (boosted ontology ~1.3) -> 9
    """
    return max(7, min(10, round(3.5 * final_score)))


class TriangulationEngine:
    def __init__(self, tenant_id: str, db):
        self.tenant_id = tenant_id
        self.db = db

    # ── Public entry point ────────────────────────────────────────────────────
    def run(self) -> int:
        """
        Full pipeline for one tenant.
        Phase 4: writes to predictive_cache, not leads collection directly.
        Includes discovery injection: 15% of token budget for explore domains.
        Returns number of cache entries written.
        """
        job_leads     = JobBoardIngestor().fetch()
        funding_leads = FundingIngestor().fetch()

        # Index by domain
        job_map     = {l["company_domain"]: l for l in job_leads     if l.get("company_domain")}
        funding_map = {l["company_domain"]: l for l in funding_leads if l.get("company_domain")}

        matched = set(job_map.keys()) & set(funding_map.keys())
        log.info(f"[{self.tenant_id}] Triangulate: {len(matched)} domains matched both signals")

        # ── Phase 4 Node 4: Discovery Injection ───────────────────────────────
        # Partition into exploit (standard) and explore (relaxed) buckets
        exploit_domains: List[str] = []
        explore_domains: List[str] = []

        for domain in matched:
            ontology_doc  = self.db.collection("ontology_map").document(domain).get()
            ontology_data = ontology_doc.to_dict() if ontology_doc.exists else {}
            baseline_weight = ontology_data.get("baseline_weight", 1.0)
            total_yield     = ontology_data.get("total_yield", 0)

            # Store metadata on domain for downstream use
            if baseline_weight < 1.0 or total_yield == 0:
                explore_domains.append((domain, baseline_weight, total_yield))
            else:
                exploit_domains.append((domain, baseline_weight, total_yield))

        log.info(f"[{self.tenant_id}] Exploit bucket: {len(exploit_domains)} | "
                 f"Explore bucket: {len(explore_domains)}")

        cache_written    = 0
        explore_tk_used  = 0  # in-memory exploration token counter

        # ── Process EXPLOIT domains (standard threshold 1.8) ─────────────────
        for domain, baseline_weight, _ in exploit_domains:
            final_score = 2.0 * baseline_weight
            if final_score < EXPLOIT_THRESHOLD:
                continue
            wrote = self._process_lead(
                domain=domain,
                job_data=job_map[domain],
                funding_data=funding_map[domain],
                final_score=final_score,
                is_exploration=False,
            )
            if wrote:
                cache_written += 1

        # ── Process EXPLORE domains (relaxed threshold 1.4, token-capped) ────
        for domain, baseline_weight, total_yield in explore_domains:
            final_score = 2.0 * baseline_weight if baseline_weight > 0 else 1.4

            if final_score < EXPLORE_THRESHOLD:
                log.info(f"[{self.tenant_id}] EXPLORE: {domain} scored {final_score:.2f} "
                         f"< {EXPLORE_THRESHOLD}, skip")
                continue

            # Exploration token budget cap: stop Gemini calls once 15% budget used
            if explore_tk_used >= EXPLORE_TOKEN_BUDGET:
                log.info(f"[{self.tenant_id}] EXPLORE: token budget exhausted "
                         f"({explore_tk_used}/{EXPLORE_TOKEN_BUDGET}), skipping {domain}")
                continue

            wrote = self._process_lead(
                domain=domain,
                job_data=job_map[domain],
                funding_data=funding_map[domain],
                final_score=final_score,
                is_exploration=True,
                explore_token_tracker=lambda t: setattr(self, "_last_explore_tk", t),
            )
            if wrote:
                cache_written += 1
                explore_tk_used += TOKENS_PER_CALL  # conservative estimate

        log.info(f"[{self.tenant_id}] Run complete. Cache entries written: {cache_written} "
                 f"(explore tokens used: {explore_tk_used})")
        return cache_written

    # ── Per-lead pipeline ─────────────────────────────────────────────────────
    def _process_lead(
        self, domain, job_data, funding_data, final_score,
        is_exploration=False, explore_token_tracker=None
    ) -> bool:
        # Dedup check (O(1) via autonomous_dedup collection)
        dedup_hash = hashlib.sha256(f"{self.tenant_id}_{domain}".encode()).hexdigest()
        dedup_ref  = self.db.collection("autonomous_dedup").document(dedup_hash)
        dedup_doc  = dedup_ref.get()

        if dedup_doc.exists:
            created_at = dedup_doc.to_dict().get("created_at")
            if created_at:
                cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=DEDUP_WINDOW_DAYS)
                if hasattr(created_at, "tzinfo") and created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=datetime.timezone.utc)
                if created_at >= cutoff:
                    tag = "EXPLORE" if is_exploration else "DEDUP"
                    log.info(f"[{self.tenant_id}] {tag}: {domain} seen within {DEDUP_WINDOW_DAYS}d, skip")
                    return False

        # Generate intelligence
        intent_signal, dossier_text, pain_point = self._generate_dossier(
            job_data, funding_data, domain, force_bypass=False
        )

        lead_id = dedup_hash
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        payload = {
            "id":                  lead_id,
            "source_url":          f"https://{domain}",
            "tenant_id":           self.tenant_id,
            "origin_engine":       "autonomous",
            "score":               _scale_ui_score(final_score),
            "intent_signal":       intent_signal,
            "pain_point":          pain_point,
            "dossier_text":        dossier_text,
            "company_name":        funding_data.get("company_name"),
            "hiring_intent_found": "Yes",
            "sourcing_vector":     "Autonomous/Signal Triangulation"
                                   + (" (Exploration)" if is_exploration else ""),
            "confidence_tier":     "High",
            "status":              "new",
            # 72h cache TTL: Router pops and moves to leads; stale entries auto-expire
            "expire_at":           now_utc + datetime.timedelta(hours=CACHE_TTL_HOURS),
            "createdAt":           firestore.SERVER_TIMESTAMP,
            "triangulation_score": round(final_score, 4),
            "is_exploration":      is_exploration,
        }

        ok = self._validate_and_cache(payload)
        if ok:
            dedup_ref.set({
                "tenant_id":      self.tenant_id,
                "company_domain": domain,
                "created_at":     now_utc,
            })
        return ok

    # ── Gemini micro-dossier (token-budgeted) ─────────────────────────────────
    def _generate_dossier(
        self, job_data, funding_data, domain, force_bypass=False
    ) -> Tuple[str, Optional[str], str]:
        company  = funding_data.get("company_name", domain)
        amount   = funding_data.get("amount_raised", 0)
        round_t  = funding_data.get("round", "Seed")
        job_role = job_data.get("job_title", "key roles")

        if force_bypass or not self._can_use_gemini():
            log.info(f"[{self.tenant_id}] Bypass Gemini for {domain}")
            intent_signal = (f"Recently secured {round_t} funding of ${amount:,} "
                             f"and actively hiring for {job_role}.")
            pain_point    = (f"Post-{round_t} scaling — likely needs infrastructure "
                             f"and tooling support for rapid team growth.")
            return intent_signal, None, pain_point

        prompt = f"""You are a B2B intelligence analyst. A company just raised funding AND is hiring.

COMPANY: {company}
DOMAIN: {domain}
FUNDING: {round_t} round, ${amount:,}
HIRING: Active posting for "{job_role}"

Respond ONLY in strict JSON:
{{
  "dossier_text": "3 sentences. Why this company is high-intent B2B right now.",
  "intent_signal": "1 sentence. The combined buying signal from these two data points.",
  "pain_point": "1 sentence. Most likely operational pain given their growth stage."
}}"""

        try:
            model    = GenerativeModel(GEMINI_MODEL)
            response = model.generate_content(
                prompt,
                generation_config=GenerationConfig(response_mime_type="application/json")
            )
            data        = json.loads(response.text)
            tokens_used = getattr(getattr(response, "usage_metadata", None),
                                  "total_token_count", TOKENS_PER_CALL)
            self._increment_token_usage(int(tokens_used))
            return (
                data.get("intent_signal", ""),
                data.get("dossier_text"),
                data.get("pain_point", ""),
            )
        except Exception as e:
            log.error(f"[{self.tenant_id}] Gemini failed for {domain}: {e}")
            intent_signal = f"Recently secured {round_t} funding and hiring for {job_role}."
            pain_point    = f"Post-funding growth company needs operational scaling support."
            return intent_signal, None, pain_point

    # ── Token bucket ──────────────────────────────────────────────────────────
    def _can_use_gemini(self) -> bool:
        user_ref = self.db.collection("users").document(self.tenant_id)
        user_doc = user_ref.get().to_dict() or {}
        now_utc  = datetime.datetime.now(datetime.timezone.utc)
        reset_at = user_doc.get("daily_reset_at")
        usage    = user_doc.get("daily_token_usage", 0)
        if reset_at is None or now_utc > reset_at:
            midnight = (now_utc + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            user_ref.set({"daily_token_usage": 0, "daily_reset_at": midnight}, merge=True)
            return True
        return usage < MAX_DAILY_TOKENS

    def _increment_token_usage(self, tokens: int):
        self.db.collection("users").document(self.tenant_id).set(
            {"daily_token_usage": firestore.Increment(tokens)}, merge=True
        )

    # ── Phase 4: Write to predictive_cache (NOT leads) ────────────────────────
    def _validate_and_cache(self, payload: dict) -> bool:
        """
        Validates against LeadPayload and writes to predictive_cache subcollection.
        The Router (in orchestrator) will later pop from here and move to leads.
        72h expire_at ensures stale intelligence auto-drops without a cron.
        """
        cache_ref = (
            self.db.collection("users")
            .document(self.tenant_id)
            .collection("predictive_cache")
            .document(payload["id"])
        )
        try:
            validated      = LeadPayload(**payload)
            firestore_dict = validated.to_firestore_dict()
            # System fields not in Pydantic model
            firestore_dict["expire_at"]         = payload.get("expire_at")
            firestore_dict["createdAt"]         = firestore.SERVER_TIMESTAMP
            firestore_dict["triangulation_score"] = payload.get("triangulation_score", 0)
            firestore_dict["is_exploration"]    = payload.get("is_exploration", False)
            cache_ref.set(firestore_dict, merge=True)
            log.info(f"[CACHE] Written to predictive_cache: {payload['id']} "
                     f"score={validated.score}, exploration={payload.get('is_exploration')}")

            # Ontology upsert
            base_path = parse_base_path(payload["source_url"])
            if base_path and base_path != "unknown":
                try:
                    self.db.collection("ontology_map").document(base_path).set(
                        {"base_path": base_path, "total_yield": firestore.Increment(1),
                         "baseline_weight": 1.0, "last_seen": firestore.SERVER_TIMESTAMP},
                        merge=True
                    )
                except Exception as oe:
                    log.warning(f"[ONTOLOGY] Upsert failed: {oe}")
            return True

        except ValidationError as ve:
            log.error(f"[CONTRACT] Schema violation for {payload.get('id')}: {ve}")
            payload_copy             = dict(payload)
            payload_copy["status"]   = "schema_violation"
            payload_copy["schema_error"] = str(ve)
            cache_ref.set(payload_copy, merge=True)
            return False
