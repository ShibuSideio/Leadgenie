"""
engine.py - V16 Autonomous Engine: Triangulation, Dedup, Token Bucket, Gemini Dossier
=======================================================================================
TriangulationEngine.run() is the core loop for a single tenant:
  1. Ingest job + funding signals
  2. Cross-reference to find domains in BOTH sets
  3. Score via baseline_weight from ontology_map (Final = 2.0 * weight, threshold 1.8)
  4. Dedup against autonomous_dedup (60-day O(1) ledger)
  5. Token bucket check -> Gemini micro-dossier OR auto-generated intent_signal
  6. Contract write via LeadPayload + set(merge=True)
"""

import os
import json
import hashlib
import logging
import datetime
from urllib.parse import urlparse
from typing import Optional, Tuple

import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
from google.cloud import firestore
from pydantic import ValidationError

from models import LeadPayload
from ingestors import JobBoardIngestor, FundingIngestor

log = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("PROJECT_ID", "sideio-leads-v16")
GEMINI_MODEL = "gemini-2.5-flash"
SCORE_THRESHOLD = 1.8          # minimum triangulation score to promote lead
DEDUP_WINDOW_DAYS = 60         # autonomous_dedup TTL
MAX_DAILY_TOKENS = 5000        # per-tenant daily Gemini token budget
TOKENS_PER_CALL = 500          # conservative estimate per micro-dossier

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
    Maps triangulation score (threshold 1.8) to UI score (0-10).
    1.8  (min pass, neutral weight)  -> 7
    2.0  (both signals, weight=1.0)  -> 7
    2.6  (boosted weight ~1.3)       -> 9
    3.0+ (very high weight)          -> 10
    """
    return max(7, min(10, round(3.5 * final_score)))


class TriangulationEngine:
    def __init__(self, tenant_id: str, db):
        self.tenant_id = tenant_id
        self.db = db

    # ── Public entry point ────────────────────────────────────────────────────
    def run(self) -> int:
        """Execute full pipeline for this tenant. Returns number of leads written."""
        job_leads     = JobBoardIngestor().fetch()
        funding_leads = FundingIngestor().fetch()

        # Index by domain
        job_map     = {l["company_domain"]: l for l in job_leads     if l.get("company_domain")}
        funding_map = {l["company_domain"]: l for l in funding_leads if l.get("company_domain")}

        matched = set(job_map.keys()) & set(funding_map.keys())
        log.info(f"[{self.tenant_id}] Triangulate: {len(matched)} domains in both signals")

        leads_written = 0
        for domain in matched:
            # Ontology weight fetch
            ontology_doc = self.db.collection("ontology_map").document(domain).get()
            baseline_weight = (
                ontology_doc.to_dict().get("baseline_weight", 1.0)
                if ontology_doc.exists else 1.0
            )

            final_score = 2.0 * baseline_weight
            if final_score < SCORE_THRESHOLD:
                log.info(f"[{self.tenant_id}] {domain} scored {final_score:.2f} < {SCORE_THRESHOLD}, skip")
                continue

            wrote = self._process_lead(
                domain=domain,
                job_data=job_map[domain],
                funding_data=funding_map[domain],
                final_score=final_score,
                baseline_weight=baseline_weight,
            )
            if wrote:
                leads_written += 1

        log.info(f"[{self.tenant_id}] Run complete. Leads written: {leads_written}")
        return leads_written

    # ── Per-lead pipeline ─────────────────────────────────────────────────────
    def _process_lead(self, domain, job_data, funding_data, final_score, baseline_weight) -> bool:
        # 1. Dedup check (O(1) via autonomous_dedup collection)
        dedup_hash = hashlib.sha256(f"{self.tenant_id}_{domain}".encode()).hexdigest()
        dedup_ref  = self.db.collection("autonomous_dedup").document(dedup_hash)
        dedup_doc  = dedup_ref.get()

        if dedup_doc.exists:
            created_at = dedup_doc.to_dict().get("created_at")
            if created_at:
                cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=DEDUP_WINDOW_DAYS)
                # Firestore timestamps may have tzinfo; normalise
                if hasattr(created_at, "tzinfo") and created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=datetime.timezone.utc)
                if created_at >= cutoff:
                    log.info(f"[{self.tenant_id}] DEDUP: {domain} seen within {DEDUP_WINDOW_DAYS}d, skip")
                    return False

        # 2. Generate intelligence (Gemini or bypass)
        intent_signal, dossier_text, pain_point = self._generate_dossier(job_data, funding_data, domain)

        # 3. Build and validate payload
        lead_id = dedup_hash  # reuse same hash as doc ID (idempotent)
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        payload = {
            "id":                           lead_id,
            "source_url":                   f"https://{domain}",
            "tenant_id":                    self.tenant_id,
            "origin_engine":                "autonomous",
            "score":                        _scale_ui_score(final_score),
            "intent_signal":                intent_signal,
            "pain_point":                   pain_point,
            "dossier_text":                 dossier_text,
            "company_name":                 funding_data.get("company_name"),
            "hiring_intent_found":          "Yes",
            "sourcing_vector":              "Autonomous/Signal Triangulation",
            "confidence_tier":              "High",
            "status":                       "new",
            # DPDP TTL: auto-deleted after 90 days unless pushed to CRM
            "expire_at":                    now_utc + datetime.timedelta(days=90),
            "createdAt":                    firestore.SERVER_TIMESTAMP,
        }

        ok = self._validate_and_write(payload)
        if ok:
            # 4. Write dedup ledger
            dedup_ref.set({
                "tenant_id":      self.tenant_id,
                "company_domain": domain,
                "created_at":     now_utc,
            })
        return ok

    # ── Gemini micro-dossier (token-budgeted) ─────────────────────────────────
    def _generate_dossier(self, job_data, funding_data, domain) -> Tuple[str, Optional[str], str]:
        """
        Returns (intent_signal, dossier_text, pain_point).
        If daily token budget exhausted, bypasses Gemini and auto-generates text.
        """
        company  = funding_data.get("company_name", domain)
        amount   = funding_data.get("amount_raised", 0)
        round_t  = funding_data.get("round", "Seed")
        job_role = job_data.get("job_title", "key roles")

        # Token bucket check
        if not self._can_use_gemini():
            log.info(f"[{self.tenant_id}] Token budget exhausted — auto-generating dossier for {domain}")
            intent_signal = f"Recently secured {round_t} funding of ${amount:,} and actively hiring for {job_role}."
            pain_point    = f"Scaling operations post-{round_t} round; likely needs infrastructure and tooling support."
            return intent_signal, None, pain_point

        # Gemini path
        prompt = f"""You are a B2B intelligence analyst. A company just raised funding AND is hiring simultaneously.

COMPANY: {company}
DOMAIN: {domain}
FUNDING: {round_t} round, ${amount:,} raised
HIRING: Active job posting for "{job_role}"

Generate a concise intelligence dossier in strict JSON:
{{
  "dossier_text": "3 sentences max. Why this company is a high-intent B2B prospect right now.",
  "intent_signal": "1 sentence. The specific buying signal from these two data points combined.",
  "pain_point": "1 sentence. The most likely operational pain point given their growth stage."
}}"""

        try:
            model    = GenerativeModel(GEMINI_MODEL)
            response = model.generate_content(
                prompt,
                generation_config=GenerationConfig(response_mime_type="application/json")
            )
            data = json.loads(response.text)
            # Increment token usage
            tokens_used = getattr(getattr(response, "usage_metadata", None), "total_token_count", TOKENS_PER_CALL)
            self._increment_token_usage(int(tokens_used))

            return (
                data.get("intent_signal", ""),
                data.get("dossier_text"),
                data.get("pain_point", ""),
            )
        except Exception as e:
            log.error(f"[{self.tenant_id}] Gemini dossier failed for {domain}: {e}")
            intent_signal = f"Recently secured {round_t} funding and hiring for {job_role}."
            pain_point    = f"Post-funding growth company likely needs operational scale."
            return intent_signal, None, pain_point

    # ── Token bucket helpers ──────────────────────────────────────────────────
    def _can_use_gemini(self) -> bool:
        """Check + reset daily token bucket. Returns True if budget available."""
        user_ref  = self.db.collection("users").document(self.tenant_id)
        user_doc  = user_ref.get().to_dict() or {}
        now_utc   = datetime.datetime.now(datetime.timezone.utc)

        reset_at = user_doc.get("daily_reset_at")
        usage    = user_doc.get("daily_token_usage", 0)

        # Reset if past midnight UTC
        if reset_at is None or now_utc > reset_at:
            midnight = (now_utc + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            user_ref.set(
                {"daily_token_usage": 0, "daily_reset_at": midnight},
                merge=True
            )
            return True  # fresh bucket

        return usage < MAX_DAILY_TOKENS

    def _increment_token_usage(self, tokens: int):
        self.db.collection("users").document(self.tenant_id).set(
            {"daily_token_usage": firestore.Increment(tokens)},
            merge=True
        )

    # ── Contract write (universal set(merge=True)) ────────────────────────────
    def _validate_and_write(self, payload: dict) -> bool:
        """
        Validates payload against LeadPayload contract and writes to Firestore.
        Uses set(merge=True) — creates fresh docs for autonomous leads (no prior stub).
        On ValidationError: dead-letter with status=schema_violation.
        Also upserts ontology_map on success.
        """
        doc_ref = self.db.collection("leads").document(payload["id"])
        try:
            validated       = LeadPayload(**payload)
            firestore_dict  = validated.to_firestore_dict()
            # Inject system fields not in Pydantic model
            firestore_dict["expire_at"] = payload.get("expire_at")
            firestore_dict["createdAt"] = firestore.SERVER_TIMESTAMP
            doc_ref.set(firestore_dict, merge=True)
            log.info(f"[CONTRACT] V16 lead written: {payload['id']} "
                     f"(domain={payload.get('company_name')}, score={validated.score})")

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
                    log.warning(f"[ONTOLOGY] Upsert failed for {base_path}: {oe}")
            return True

        except ValidationError as ve:
            log.error(f"[CONTRACT] Schema violation for {payload.get('id')}: {ve}")
            dead = dict(payload)
            dead["status"]           = "schema_violation"
            dead["schema_error"]     = str(ve)
            doc_ref.set(dead, merge=True)
            return False
