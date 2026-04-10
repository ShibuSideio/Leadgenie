import os
import re

# 1. Patch app.js
with open('public/app.js', 'r', encoding='utf-8') as f:
    text = f.read()

# Edit the predictive card HTML
card_html_target = '''                        <label style="font-size:0.8rem; color:var(--text-muted); display: block;">Unfair Advantage</label>
                        <textarea id="c-adv-${idx}" class="fc-intent-input" style="min-height:60px; padding:8px; margin-bottom:12px; width: 100%; border: 1px solid #d1d5db; border-radius: 8px;">${(camp.unfair_advantage || '')}</textarea>
                        
                        <button class="primary-btn" style="width:100%; font-size:0.9rem; padding:8px; background:#10b981; border:none; border-radius: 20px; color:white; font-weight: 600; cursor: pointer;" onclick="window.deployPredictiveCard(${idx}, '${bProd}', '${bHook}', '${bAdv}')">Deploy Campaign</button>'''

card_html_new = '''                        <label style="font-size:0.8rem; color:var(--text-muted); display: block;">Unfair Advantage</label>
                        <textarea id="c-adv-${idx}" class="fc-intent-input" style="min-height:60px; padding:8px; margin-bottom:12px; width: 100%; border: 1px solid #d1d5db; border-radius: 8px;">${(camp.unfair_advantage || '')}</textarea>

                        <label style="font-size:0.8rem; color:var(--text-muted); display: block;">Target Location</label>
                        <input type="text" id="c-loc-${idx}" class="fc-intent-input" style="height:36px; padding:8px; margin-bottom:12px; width: 100%; border: 1px solid #d1d5db; border-radius: 8px;" placeholder="e.g. London, UK, Worldwide" value="${window._dtState?.extractedGl || ''}">
                        
                        <button class="primary-btn" style="width:100%; font-size:0.9rem; padding:8px; background:#10b981; border:none; border-radius: 20px; color:white; font-weight: 600; cursor: pointer;" onclick="window.deployPredictiveCard(${idx}, '${bProd}', '${bHook}', '${bAdv}')">Deploy Campaign</button>'''

text = text.replace(card_html_target, card_html_new)

# Edit deployPredictiveCard
deploy_target = '''window.deployPredictiveCard = function(idx, origProd, origHook, origAdv) {
    const prod = (document.getElementById('c-prod-' + idx)?.value || '').trim();
    const hook = (document.getElementById('c-hook-' + idx)?.value || '').trim();
    const adv  = (document.getElementById('c-adv-' + idx)?.value || '').trim();
    
    // basic diff via btoa
    const wasEdited = (btoa(prod.replace(/['"]/g, '')) !== origProd) || 
                      (btoa(hook.replace(/['"]/g, '')) !== origHook) || 
                      (btoa(adv.replace(/['"]/g, '')) !== origAdv);
                      
    document.getElementById('child-campaign-modal')?.classList.add('hidden');

    saveCampaignAction({
        name: prod,
        bio: 'CHILD_CAMPAIGN_OVERRIDE',
        keywords: (hook + ' | ' + adv).substring(0, 150),
        gl: '',
        location: '',
        target_urls: [],
        human_edited: wasEdited,
        target_angle_hook: hook,
        target_angle_adv: adv
    });
};'''

deploy_new = '''window.deployPredictiveCard = function(idx, origProd, origHook, origAdv) {
    const prod = (document.getElementById('c-prod-' + idx)?.value || '').trim();
    const hook = (document.getElementById('c-hook-' + idx)?.value || '').trim();
    const adv  = (document.getElementById('c-adv-' + idx)?.value || '').trim();
    const loc  = (document.getElementById('c-loc-' + idx)?.value || '').trim();
    
    if (!loc && loc.toLowerCase() !== 'worldwide') {
        showToast('Target Location is required.', 'error');
        return;
    }

    // basic diff via btoa
    const wasEdited = (btoa(prod.replace(/['"]/g, '')) !== origProd) || 
                      (btoa(hook.replace(/['"]/g, '')) !== origHook) || 
                      (btoa(adv.replace(/['"]/g, '')) !== origAdv);
                      
    document.getElementById('child-campaign-modal')?.classList.add('hidden');

    saveCampaignAction({
        name: prod,
        bio: 'CHILD_CAMPAIGN_OVERRIDE',
        keywords: '',
        campaign_focus: prod,
        pain_point: hook,
        unfair_advantage: adv,
        gl: '',
        location: loc, // Captured here
        target_urls: [],
        human_edited: wasEdited,
        target_angle_hook: hook,
        target_angle_adv: adv
    });
};'''

text = text.replace(deploy_target, deploy_new)

# Edit saveChildCampaign
save_cc_target = '''window.saveChildCampaign = function() {
    const focusEl = document.getElementById('cc-focus');
    const painEl = document.getElementById('cc-pain');
    const advEl = document.getElementById('cc-advantage');
    
    const focus = focusEl?.value.trim() || 'Custom Campaign';
    const pain = painEl?.value.trim() || '';
    const adv = advEl?.value.trim() || '';

    document.getElementById('child-campaign-modal')?.classList.add('hidden');

    // Guardrail 2: Route distinctly, DO NOT concat into keywords
    saveCampaignAction({
        name: focus,
        bio: 'CHILD_CAMPAIGN_OVERRIDE',
        keywords: '', // Clear legacy keywords reliance
        campaign_focus: focus,
        pain_point: pain,
        unfair_advantage: adv,
        gl: '',
        location: '',
        target_urls: []
    });
};'''

save_cc_new = '''window.saveChildCampaign = function() {
    const focusEl = document.getElementById('cc-focus');
    const locEl = document.getElementById('cc-location');
    const painEl = document.getElementById('cc-pain');
    const advEl = document.getElementById('cc-advantage');
    
    const focus = focusEl?.value.trim() || 'Custom Campaign';
    const loc = locEl?.value.trim() || '';
    const pain = painEl?.value.trim() || '';
    const adv = advEl?.value.trim() || '';

    if (!loc && loc.toLowerCase() !== 'worldwide') {
        showToast('Target Geography is required.', 'error');
        return;
    }

    document.getElementById('child-campaign-modal')?.classList.add('hidden');

    // Guardrail 2: Route distinctly, DO NOT concat into keywords
    saveCampaignAction({
        name: focus,
        bio: 'CHILD_CAMPAIGN_OVERRIDE',
        keywords: '', // Clear legacy keywords reliance
        campaign_focus: focus,
        pain_point: pain,
        unfair_advantage: adv,
        gl: '',
        location: loc,
        target_urls: []
    });
};'''

text = text.replace(save_cc_target, save_cc_new)

with open('public/app.js', 'w', encoding='utf-8') as f:
    f.write(text)

# 2. Patch orchestrator/main.py
with open('services/orchestrator/main.py', 'r', encoding='utf-8') as f:
    orch_text = f.read()

orch_target = '''            elif request.path == "/api/campaigns" and request.method == "POST":
                is_valid, status_code, err_msg = check_quota(tenant_id)
                if not is_valid:
                    return jsonify({"error": err_msg}), status_code

                # Hard limit N active product/service campaigns per tenant'''

orch_new = '''            elif request.path == "/api/campaigns" and request.method == "POST":
                is_valid, status_code, err_msg = check_quota(tenant_id)
                if not is_valid:
                    return jsonify({"error": err_msg}), status_code
                    
                # Schema Map: Location -> GL Logic
                loc_raw = (data.get('location') or '').strip().lower()
                gl_map = {
                    "usa": "us", "united states": "us", "uk": "uk", 
                    "united kingdom": "uk", "canada": "ca", "australia": "au",
                    "germany": "de", "singapore": "sg", "uae": "ae", 
                    "dubai": "ae", "india": "in"
                }
                
                # If explicit match, set GL. If not, Serper defaults to 'us' but 
                # loc_raw remains in 'location' string to be appended to Vertex Search Context.
                if loc_raw in gl_map:
                    data['gl'] = gl_map[loc_raw]
                elif loc_raw == "worldwide" or not loc_raw:
                    data['gl'] = "us" # default fallback
                else:
                    data['gl'] = "us" # custom cities fallback to US GL, append loc string elsewhere

                # Hard limit N active product/service campaigns per tenant'''

if orch_target in orch_text:
    orch_text = orch_text.replace(orch_target, orch_new)
    with open('services/orchestrator/main.py', 'w', encoding='utf-8') as f:
        f.write(orch_text)
    print("Orchestrator patched")
else:
    print("Orchestrator target not found")

print("Patch complete")
