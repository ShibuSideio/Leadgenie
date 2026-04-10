import os

with open('services/digital-twin-engine/main.py', 'r', encoding='utf-8') as f:
    text = f.read()

if 'from firebase_admin import auth as firebase_auth, credentials, firestore' not in text:
    text = text.replace(
        'from firebase_admin import auth as firebase_auth, credentials',
        'from firebase_admin import auth as firebase_auth, credentials, firestore'
    )

with open('task_b.py', 'r', encoding='utf-8') as f2:
    task_b_code = f2.read()

if '_run_predictive_chain' not in text:
    target_str = '# =============================================================================\n# LAYER 5: MAIN ENDPOINT\n# ============================================================================='
    text = text.replace(target_str, task_b_code + '\n\n' + target_str)

phase_4_old = """    # ── Phase 4: Gemini Synthesis ─────────────────────────────────────────────
    try:
        prompt   = _build_gemini_prompt(root_domain, combined_text)
        gemini_result = _call_gemini(prompt)
    except TimeoutError:"""

phase_4_new = """    # ── Phase 4: Gemini Synthesis & Predictive Chain ─────────────────────────
    try:
        prompt   = _build_gemini_prompt(root_domain, combined_text)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f_core = pool.submit(_call_gemini, prompt)
            f_pred = pool.submit(_run_predictive_chain, root_domain, combined_text)
            
            gemini_result = f_core.result(timeout=7.0)
            try:
                predictive_campaigns = f_pred.result(timeout=7.0)
            except Exception as e:
                print(f"[DT] Predictive chain failed or timed out: {e}")
                predictive_campaigns = []
                
    except (TimeoutError, concurrent.futures.TimeoutError):"""

if 'Predictive Chain ──' not in text:
    text = text.replace(phase_4_old, phase_4_new)

payload_old = """            "targets":     normalised_targets,
            "detected_gl": detected_gl"""
payload_new = """            "targets":     normalised_targets,
            "recommended_campaigns": predictive_campaigns,
            "detected_gl": detected_gl"""

if 'recommended_campaigns' not in text:
    text = text.replace(payload_old, payload_new)

with open('services/digital-twin-engine/main.py', 'w', encoding='utf-8') as f:
    f.write(text)
print("Patch applied")
