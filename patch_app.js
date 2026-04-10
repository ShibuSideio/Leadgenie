import fs from 'fs';

const path = 'public/app.js';
let text = fs.readFileSync(path, 'utf-8');

const target = `window.openChildCampaignModal = function() {
    const aw = window.activeWallet || {};
    const ud = window.currentUserData || {};
    const allocated = Number(aw.allocated_credits || ud.allocated_credits || 0) || 0;
    const consumed  = Number(aw.consumed_credits  || ud.consumed_credits  || 0) || 0;
    const remaining = allocated - consumed;
    
    if (remaining <= 0) {
        showToast('Credits exhausted. Contact admin to reload.', 'error');
        return;
    }

    const modal = document.getElementById('child-campaign-modal');
    if (modal) {
        modal.classList.remove('hidden');
        document.getElementById('cc-name').value = '';
        document.getElementById('cc-desc').value = '';
    }
};`;

const newCode = `window.openChildCampaignModal = async function() {
    const aw = window.activeWallet || {};
    const ud = window.currentUserData || {};
    const allocated = Number(aw.allocated_credits || ud.allocated_credits || 0) || 0;
    const consumed  = Number(aw.consumed_credits  || ud.consumed_credits  || 0) || 0;
    const remaining = allocated - consumed;
    
    if (remaining <= 0) {
        showToast('Credits exhausted. Contact admin to reload.', 'error');
        return;
    }

    const modal = document.getElementById('child-campaign-modal');
    if (modal) {
        modal.classList.remove('hidden');
        const fallbackCont = document.getElementById('cc-custom-fallback-container');
        if(fallbackCont) fallbackCont.classList.add('hidden');
        const cardsEl = document.getElementById('cc-recommendation-cards');
        if(cardsEl) {
            cardsEl.innerHTML = '<p style="text-align:center; color:#6b7280;">Loading market intelligence...</p>';
            cardsEl.style.display = 'block';
        }

        const rawProfile = await fetchTenantProfile();
        let html = '';
        if (rawProfile && rawProfile.recommended_campaigns && rawProfile.recommended_campaigns.length > 0) {
            rawProfile.recommended_campaigns.forEach((camp, idx) => {
                const bProd = btoa((camp.product_name || '').replace(/['"]/g, ''));
                const bHook = btoa((camp.market_trend_hook || '').replace(/['"]/g, ''));
                const bAdv  = btoa((camp.unfair_advantage || '').replace(/['"]/g, ''));
                
                html += \`
                <div id="c-card-\${idx}" style="background: rgba(255,255,255,0.6); padding: 16px; border-radius: 12px; margin-bottom: 16px; border: 1px solid var(--glass-border); text-align: left;">
                    <div id="c-card-view-\${idx}">
                        <h4 style="margin:0 0 6px 0; color:var(--primary); font-size:1.1rem;">\${camp.product_name || 'Product'}</h4>
                        <p style="font-size:0.9rem; margin-bottom:12px; line-height: 1.4;"><strong style="color:#4f46e5;">Market Trend:</strong> \${camp.market_trend_hook || ''}<br><strong style="color:#4f46e5;">Advantage:</strong> \${camp.unfair_advantage || ''}</p>
                        <button class="primary-btn" style="width:100%; font-size:0.9rem; padding:8px;" onclick="window.editPredictiveCard(\${idx})">Review & Launch</button>
                    </div>
                    <div id="c-card-edit-\${idx}" class="hidden">
                        <label style="font-size:0.8rem; color:var(--text-muted); display: block;">Product Focus</label>
                        <input type="text" id="c-prod-\${idx}" class="fc-intent-input" style="height:36px; padding:8px; margin-bottom:8px; width: 100%; border: 1px solid #d1d5db; border-radius: 8px;" value="\${(camp.product_name || '').replace(/"/g, '&quot;')}">
                        
                        <label style="font-size:0.8rem; color:var(--text-muted); display: block;">Market Opportunity</label>
                        <textarea id="c-hook-\${idx}" class="fc-intent-input" style="min-height:60px; padding:8px; margin-bottom:8px; width: 100%; border: 1px solid #d1d5db; border-radius: 8px;">\${(camp.market_trend_hook || '')}</textarea>
                        
                        <label style="font-size:0.8rem; color:var(--text-muted); display: block;">Unfair Advantage</label>
                        <textarea id="c-adv-\${idx}" class="fc-intent-input" style="min-height:60px; padding:8px; margin-bottom:12px; width: 100%; border: 1px solid #d1d5db; border-radius: 8px;">\${(camp.unfair_advantage || '')}</textarea>
                        
                        <button class="primary-btn" style="width:100%; font-size:0.9rem; padding:8px; background:#10b981; border:none; border-radius: 20px; color:white; font-weight: 600; cursor: pointer;" onclick="window.deployPredictiveCard(\${idx}, '\${bProd}', '\${bHook}', '\${bAdv}')">Deploy Campaign</button>
                    </div>
                </div>
                \`;
            });
        } else {
            html = '<p style="text-align:center; color:#6b7280;">No predictive campaigns available. Use the custom fallback.</p>';
        }
        if(cardsEl) cardsEl.innerHTML = html;
        if(document.getElementById('cc-name')) document.getElementById('cc-name').value = '';
    }
};

window.editPredictiveCard = function(idx) {
    document.getElementById('c-card-view-' + idx).classList.add('hidden');
    document.getElementById('c-card-edit-' + idx).classList.remove('hidden');
};

window.deployPredictiveCard = function(idx, origProd, origHook, origAdv) {
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
};

window.showCcCustomFallback = function() {
    const r = document.getElementById('cc-recommendation-cards');
    if(r) r.style.display = 'none';
    const f = document.getElementById('cc-custom-fallback-container');
    if(f) f.classList.remove('hidden');
};`;

if (text.includes('window.openChildCampaignModal = function() {')) {
    text = text.replace(target, newCode);
    fs.writeFileSync(path, text, 'utf-8');
    console.log('Successfully patched app.js modal handler');
} else {
    console.log('Target string not found in app.js');
}
