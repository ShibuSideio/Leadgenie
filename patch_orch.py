import os

path = 'services/orchestrator/main.py'
with open(path, 'r', encoding='utf-8') as f:
    text = f.read()

target = """                if active_campaigns_count >= MAX_CHILD_CAMPAIGNS:
                    return jsonify({"error": f"Maximum of {MAX_CHILD_CAMPAIGNS} active product/service campaigns allowed per tenant."}), 403

                data['tenant_id'] = tenant_id"""

new_code = """                if active_campaigns_count >= MAX_CHILD_CAMPAIGNS:
                    return jsonify({"error": f"Maximum of {MAX_CHILD_CAMPAIGNS} active product/service campaigns allowed per tenant."}), 403

                # --- RLHF Telemetry Sync ---
                human_edited = data.get("human_edited", False)
                if human_edited:
                    product_name = data.get("name", "").strip()
                    orig_hook = data.pop("orig_hook", "")
                    orig_adv = data.pop("orig_adv", "")
                    target_angle_hook = data.pop("target_angle_hook", orig_hook)
                    target_angle_adv = data.pop("target_angle_adv", orig_adv)
                    data.pop("human_edited", None)
                    
                    if product_name:
                        doc_id = ''.join(c for c in product_name.lower() if c.isalnum() or c in ['-', '_'])[:100]
                        if doc_id:
                            print(f"[RLHF] Human-edited feedback received for product '{product_name}'. Capturing telemetry...")
                            try:
                                db.collection("market_trend_cache").document(doc_id).set({
                                    "market_trend_hook": target_angle_hook,
                                    "unfair_advantage": target_angle_adv,
                                    "updatedAt": firestore.SERVER_TIMESTAMP,
                                    "rlhf_source_tenant": tenant_id
                                }, merge=True)
                            except Exception as e:
                                print(f"[RLHF] Failed to capture telemetry to market_trend_cache: {e}")
                # ---------------------------

                data['tenant_id'] = tenant_id"""

if 'if active_campaigns_count >= MAX_CHILD_CAMPAIGNS:' in text:
    text = text.replace(target, new_code)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    print("Orchestrator patched successfully")
else:
    print("Target string not found in orchestrator")
