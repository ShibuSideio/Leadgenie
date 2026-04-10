_PRODUCTS_SCHEMA = {
    "type": "ARRAY",
    "description": "JSON array of up to 3 distinct product/service names.",
    "items": {"type": "STRING"}
}

_TRENDS_SCHEMA = {
    "type": "ARRAY",
    "description": "Market trends for the requested products.",
    "items": {
        "type": "OBJECT",
        "properties": {
            "product_name": {"type": "STRING"},
            "market_trend_hook": {"type": "STRING"},
            "unfair_advantage": {"type": "STRING"}
        },
        "required": ["product_name", "market_trend_hook", "unfair_advantage"]
    }
}

@retry(wait=wait_exponential(multiplier=1, min=2, max=8), stop=stop_after_attempt(3), retry=retry_if_exception_type(ResourceExhausted))
def _run_predictive_chain(root_domain: str, text_blob: str) -> list[dict]:
    safe_blob = text_blob[:6_000]
    prompt_prod = f"Analyze domain {root_domain}: \n{safe_blob}\nReturn a JSON array of up to 3 distinct product/service names offered by this company."
    
    model = GenerativeModel("gemini-2.5-flash", system_instruction="You are a B2B analyst. Return strictly JSON.")
    conf_prod = GenerationConfig(response_mime_type="application/json", response_schema=_PRODUCTS_SCHEMA)
    
    def _invoke_prod():
        return model.generate_content(prompt_prod, generation_config=conf_prod)
        
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_invoke_prod)
        try:
            resp_prod = future.result(timeout=4.0)
        except concurrent.futures.TimeoutError:
            print("[DT] Products chain timed out")
            return []

    try:
        product_names = json.loads(resp_prod.text)
    except Exception as e:
        print(f"[DT] JSON decode error: {e}")
        return []

    if not isinstance(product_names, list):
        return []

    final_campaigns = []
    missing_products = []
    
    try:
        from firebase_admin import firestore
        db = firestore.client()
        for p in product_names:
            p_str = str(p).strip()
            if not p_str: continue
            
            doc_id = "".join(c for c in p_str.lower() if c.isalnum() or c in ['-', '_'])[:100]
            if not doc_id:
                missing_products.append(p_str)
                continue
                
            doc = db.collection("market_trend_cache").document(doc_id).get()
            if doc.exists:
                data = doc.to_dict()
                final_campaigns.append({
                    "product_name": p_str,
                    "market_trend_hook": data.get("market_trend_hook", ""),
                    "unfair_advantage": data.get("unfair_advantage", "")
                })
                print(f"[RLHF] Cache hit for {p_str}: {doc_id}")
            else:
                missing_products.append(p_str)
    except Exception as e:
        print(f"[RLHF] Cache access error: {e}")
        missing_products = [str(p).strip() for p in product_names if str(p).strip()]

    if missing_products:
        prompt_miss = f"""For the following products offered by {root_domain}: {missing_products}
Based on this context: {safe_blob}
Act as a Head of Growth. For each product, identify a current macro-economic trend, pain point, or market shift that makes it highly relevant right now. Also identify the unfair advantage.
Return a JSON array matching the schema."""
        conf_trends = GenerationConfig(response_mime_type="application/json", response_schema=_TRENDS_SCHEMA)
        
        def _invoke_miss():
            return model.generate_content(prompt_miss, generation_config=conf_trends)
            
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_invoke_miss)
            try:
                resp_miss = future.result(timeout=4.0)
                gen_trends = json.loads(resp_miss.text)
                if isinstance(gen_trends, list):
                    final_campaigns.extend(gen_trends)
                    print(f"[RLHF] Generated trends for {len(missing_products)} products")
            except Exception as e:
                print(f"[DT] Trends generation failed: {e}")

    return final_campaigns
