"""
Orchestrator — Research Agent Engine (V24.0)

Allows tenants to create custom "research agents" that run on schedules.
Each agent converts a natural language prompt into search queries,
executes them via Serper, scores results against a linked persona,
and writes qualified leads to Firestore.

Agents run via Cloud Tasks dispatched by the agent-sweep cron job.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from google.cloud import firestore as fs

# Lazy imports to avoid circular dependencies
def _get_logger():
    try:
        from core.logging import get_logger
        return get_logger("orchestrator.agent_engine")
    except ImportError:
        import logging
        return logging.getLogger("orchestrator.agent_engine")

log = _get_logger()

# Schedule intervals in hours
_SCHEDULE_HOURS = {
    "daily": 24,
    "biweekly": 84,    # 3.5 days
    "weekly": 168,
}


def is_agent_due(agent: dict) -> bool:
    """Check if a research agent is due to run based on its schedule."""
    schedule = agent.get("schedule", "weekly")
    interval_hours = _SCHEDULE_HOURS.get(schedule, 168)
    last_ran = agent.get("last_ran_at")
    
    if not last_ran:
        return True  # Never ran before
    
    if hasattr(last_ran, 'timestamp'):
        # Firestore Timestamp
        last_ran_dt = last_ran.replace(tzinfo=timezone.utc) if last_ran.tzinfo is None else last_ran
    elif isinstance(last_ran, str):
        last_ran_dt = datetime.fromisoformat(last_ran)
    else:
        last_ran_dt = last_ran
    
    next_run = last_ran_dt + timedelta(hours=interval_hours)
    return datetime.now(timezone.utc) >= next_run


def compute_next_run(schedule: str) -> datetime:
    """Compute the next run time based on schedule."""
    interval_hours = _SCHEDULE_HOURS.get(schedule, 168)
    return datetime.now(timezone.utc) + timedelta(hours=interval_hours)


def run_agent(agent_id: str, agent: dict, db: fs.Client) -> dict:
    """Execute a research agent's search job.
    
    This function is called by the agent-sweep cron job.
    It generates queries from the agent's prompt, runs Serper searches,
    and writes qualified results as leads.
    
    Args:
        agent_id: The agent document ID.
        agent: The agent document data.
        db: Firestore client.
    
    Returns:
        Summary dict with results_count, leads_created, etc.
    """
    tenant_id = agent.get("tenant_id", "")
    prompt = agent.get("prompt", "")
    persona_id = agent.get("persona_id", "")
    max_results = agent.get("max_results", 10)
    
    if not prompt or not tenant_id:
        log.warning("agent_skip: agent=%s reason=missing_prompt_or_tenant", agent_id)
        return {"error": "Missing prompt or tenant_id"}
    
    log.info("agent_run_start: agent=%s tenant=%s prompt_len=%d",
             agent_id, tenant_id, len(prompt))
    
    # --- Step 1: Load persona context ---
    persona_bio = ""
    persona_keywords = ""
    if persona_id:
        try:
            persona_ref = db.collection("tenant_profiles").document(tenant_id) \
                            .collection("personas").document(persona_id)
            persona_doc = persona_ref.get()
            if persona_doc.exists:
                pd = persona_doc.to_dict()
                persona_bio = pd.get("bio", "")
                persona_keywords = pd.get("keywords", "")
        except Exception as exc:
            log.warning("agent_persona_load_failed: agent=%s err=%s", agent_id, exc)
    
    # --- Step 2: Generate search queries from natural language prompt ---
    # Use Gemini to convert the prompt into 3 search queries
    queries = _prompt_to_queries(prompt, persona_bio, persona_keywords)
    if not queries:
        log.warning("agent_no_queries: agent=%s", agent_id)
        return {"error": "Failed to generate queries from prompt"}
    
    # --- Step 3: Run Serper searches ---
    all_results = []
    try:
        serper_key = os.environ.get("SERPER_API_KEY", "")
        if not serper_key:
            # Try to load from Secret Manager
            try:
                from google.cloud import secretmanager
                sm = secretmanager.SecretManagerServiceClient()
                project = os.environ.get("PROJECT_ID", "lead-sniper-prod")
                name = f"projects/{project}/secrets/SERPER_API_KEY/versions/latest"
                response = sm.access_secret_version(request={"name": name})
                serper_key = response.payload.data.decode("UTF-8").strip()
            except Exception:
                log.error("agent_no_serper_key: agent=%s", agent_id)
                return {"error": "No Serper API key available"}
        
        import httpx
        # V27.4.0 residual Serper budget (project-wide)
        try:
            from shared.serper_budget import record_serper_spend  # type: ignore[import]
            from core.clients import get_db as _gdb  # type: ignore[import]
            _n_q = min(3, len(queries))
            if not record_serper_spend(
                _gdb(), amount=_n_q, residual=True,
                log=lambda m, **k: log.info("%s %s", m, k),
            ):
                log.warning("agent_serper_budget_blocked: agent=%s amount=%s", agent_id, _n_q)
                return {"error": "Serper residual daily budget exhausted", "results": []}
        except Exception as _bg_err:
            log.warning("agent_serper_budget_error: %s", _bg_err)
        for query in queries[:3]:
            try:
                resp = httpx.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
                    json={"q": query, "num": min(max_results, 10)},
                    timeout=8.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    organic = data.get("organic", [])
                    all_results.extend(organic)
            except Exception as exc:
                log.warning("agent_serper_error: agent=%s query=%s err=%s",
                           agent_id, query[:50], exc)
    except Exception as exc:
        log.error("agent_serper_fatal: agent=%s err=%s", agent_id, exc)
        return {"error": f"Serper search failed: {exc}"}
    
    # --- Step 4: Deduplicate and write results ---
    seen_domains = set()
    results_summary = []
    leads_created = 0
    
    for result in all_results[:max_results]:
        url = result.get("link", "")
        if not url:
            continue
        
        # Simple domain dedup
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc.lower()
        except Exception:
            domain = url
        
        if domain in seen_domains:
            continue
        seen_domains.add(domain)
        
        # Create a lightweight lead
        lead_hash = hashlib.sha256(f"{tenant_id}:{url}".encode()).hexdigest()[:16]
        lead_data = {
            "tenant_id": tenant_id,
            "url": url,
            "source_url": url,
            "title": result.get("title", ""),
            "domain": domain,
            "status": "new",
            "origin_engine": "research_agent",
            "agent_id": agent_id,
            "agent_name": agent.get("name", ""),
            "score": 0,  # Will be scored by pipeline on next sweep
            "createdAt": datetime.now(timezone.utc),
            "updatedAt": datetime.now(timezone.utc),
        }
        
        if persona_id:
            lead_data["persona_id"] = persona_id
        
        try:
            db.collection("leads").document(f"{tenant_id}_{lead_hash}").set(
                lead_data, merge=True
            )
            leads_created += 1
            results_summary.append({
                "url": url,
                "title": result.get("title", ""),
                "domain": domain,
            })
        except Exception as exc:
            log.warning("agent_lead_write_failed: agent=%s url=%s err=%s",
                       agent_id, url[:80], exc)
    
    # --- Step 5: Update agent metadata ---
    try:
        agent_ref = db.collection("tenant_profiles").document(tenant_id) \
                      .collection("agents").document(agent_id)
        agent_ref.update({
            "last_ran_at": datetime.now(timezone.utc),
            "next_run_at": compute_next_run(agent.get("schedule", "weekly")),
            "total_leads_found": fs.Increment(leads_created),
            "last_run_results": results_summary[:10],
            "updatedAt": datetime.now(timezone.utc),
        })
    except Exception as exc:
        log.warning("agent_metadata_update_failed: agent=%s err=%s", agent_id, exc)
    
    log.info("agent_run_complete: agent=%s leads_created=%d total_results=%d",
             agent_id, leads_created, len(all_results))
    
    return {
        "agent_id": agent_id,
        "queries_generated": len(queries),
        "results_found": len(all_results),
        "leads_created": leads_created,
    }


def _prompt_to_queries(prompt: str, bio: str, keywords: str) -> list[str]:
    """Convert a natural language agent prompt into 3 search queries.
    
    Uses Gemini 2.5 Flash to interpret the prompt and generate
    targeted search queries.
    """
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel, GenerationConfig
        
        project = os.environ.get("PROJECT_ID", "lead-sniper-prod")
        location = os.environ.get("LOCATION", "asia-south1")
        
        try:
            vertexai.init(project=project, location=location)
        except Exception:
            pass  # Already initialized
        
        model = GenerativeModel(
            "gemini-2.5-flash",
            system_instruction=(
                "You are a search query generator. Convert natural language "
                "research prompts into Google search queries. Return ONLY "
                "a JSON array of 3 search query strings. No markdown."
            ),
        )
        config = GenerationConfig(
            response_mime_type="application/json",
            temperature=0.3,
        )
        
        full_prompt = f"""Generate 3 Google search queries for this research task:

Task: {prompt}
Business context: {bio}
Keywords: {keywords}

Rules:
- Use Boolean operators (AND, OR, site:, -site:)
- Target forums, communities, and raw content (not marketing pages)
- Be specific to the task description
- Return a JSON array of 3 strings"""
        
        import concurrent.futures
        def _invoke():
            return model.generate_content(full_prompt, generation_config=config)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_invoke)
            response = future.result(timeout=7.0)
        
        import json
        queries = json.loads(response.text)
        if isinstance(queries, list):
            return [str(q) for q in queries[:3]]
        return []
    
    except Exception as exc:
        log.warning("prompt_to_queries_failed: err=%s", exc)
        # Fallback: use the prompt directly as a single query
        return [prompt[:256]] if prompt else []
