import re

with open('public/app.js', 'r', encoding='utf-8') as f:
    content = f.read()

# Chunk 1: createLeadCard -> generateLeadInnerHtml with new fields
old_createLeadCard = """// Organic DOM Factory
function createLeadCard(docId, lead) {
    const card = document.createElement('div');
    card.className = 'lead-card';
    let urlHostname = 'Unknown URL';
    try { if (lead.url) urlHostname = new URL(lead.url).hostname; } catch(e){}
    
    const statusColor = lead.status === 'completed' ? 'var(--success)' : (lead.status === 'ignored' ? '#ef4444' : 'var(--text-muted)');
    
    let hiringIntent = lead.hiring_intent_found || '';
    let hiringBadge = '';
    if (hiringIntent === 'Yes') {
        hiringBadge = `<span style="font-size:0.75rem; background:#ecfdf5; color:#059669; padding:2px 6px; border-radius:4px; border:1px solid #a7f3d0">🟢 Hiring</span>`;
    }
    
    const techDict = {
        'stripe': 'Takes Online Payments',
        'wordpress': 'Active Content/Blog',
        'shopify': 'E-Commerce Store',
        'salesforce': 'Enterprise CRM',
        'hubspot': 'Marketing Automation',
        'google analytics': 'Tracks Analytics',
        'segment': 'Customer Data Platform',
        'intercom': 'Live Chat Support',
        'react': 'Modern Web App'
    };
    
    let techBadges = (lead.tech_stack_found && lead.tech_stack_found.length > 0) ? lead.tech_stack_found.map(tech => `<span style="font-size:0.75rem; background:transparent; color:#6b7280; padding:2px 6px; border-radius:4px; border:1px solid #e5e7eb">⚡ ${techDict[tech.toLowerCase()] || tech}</span>`).join('') : '';
    let exclusiveBadge = `<span style="font-size:0.75rem; background:#f3e8ff; color:#6b21a8; padding:2px 6px; border-radius:4px; border:1px solid #e9d5ff">🔒 Exclusive Lead</span>`;
    let competitorBadge = lead.competitor_match ? `<span style="font-size:0.75rem; background:#fee2e2; color:#b91c1c; padding:2px 6px; border-radius:4px; border:1px solid #fecaca">🎯 Competitor Intercept: ${lead.competitor_match}</span>` : '';

    card.innerHTML = `
        <div class="lead-header">
            <div>
                <strong><a href="${lead.url || '#'}" target="_blank" style="color: var(--text-main); text-decoration: none;">${urlHostname} ↗</a></strong> • ${lead.source || 'Organic Search'} 
                <span style="margin-left:8px; font-size:0.75rem; padding: 2px 6px; border-radius:4px; border: 1px solid ${statusColor}; color: ${statusColor}">${(lead.status || 'new').toUpperCase()}</span>
            </div>
            <div class="score">Score: ${lead.score || 0}/10</div>
        </div>
        <div class="pain-point">" ${lead.pain_point || 'Analyzing sentiment...'} "</div>
        <div class="premium-badges" style="margin-top: 8px; margin-bottom: 8px; font-weight: 500; display: flex; flex-wrap: wrap; gap: 6px; align-items: center;">
            ${exclusiveBadge}
            ${competitorBadge}
            ${hiringBadge}
            ${techBadges}
        </div>
        <div class="dm-draft">${lead.dm || 'Drafting variation...'}</div>
        <div class="contact-info" style="margin-top: 8px; margin-bottom: 8px; font-size: 0.85rem; color: var(--text-main); font-weight: 500;">
            ${lead.email ? `📧 <a href="mailto:${lead.email}" target="_blank" style="color:#2563eb; text-decoration:none;">${lead.email}</a> &nbsp;` : ''} 
            ${lead.phone ? `📞 <a href="tel:${lead.phone}" style="color:#2563eb; text-decoration:none;">${lead.phone}</a>` : ''}
            ${!lead.email && !lead.phone ? `<span style="color:var(--text-muted); font-style:italic;">No Contact Info Found</span>` : ''}
        </div>
        <div class="action-row" style="flex-wrap: wrap; gap: 8px; margin-top:12px; padding-top:12px; border-top: 1px solid var(--glass-border)">
            <button class="action-btn" onclick="copyMessageAndContact('${docId}', \`${(lead.dm || '').replace(/`/g, '\\\\`').replace(/'/g, "\\'")}\`)" title="Copy Message">📋 Copy Message</button>
            <button class="action-btn" onclick="pushToCRM('${docId}', \`${encodeURIComponent(JSON.stringify(lead)).replace(/'/g, "\\'")}\`)" style="color: #4f46e5; border-color: #c7d2fe; background: #e0e7ff;">☁️ Push to CRM</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'ignored')" title="Ignore Lead">🚫 Ignore</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'converted')" title="Lead Converted">🎯 Converted</button>
            <button class="action-btn" style="background:#f8fafc; color:var(--text-muted); border: 1px solid var(--glass-border);" onclick="viewLeadTimeline('${encodeURIComponent(JSON.stringify(lead.interactions || []))}')" title="Audit Log">🕒 View Timeline Logs</button>
        </div>
    `;
    return card;
}"""

new_generateLeadInnerHtml = """// Organic DOM Factory
function generateLeadInnerHtml(docId, lead) {
    let urlHostname = 'Unknown URL';
    try { if (lead.url) urlHostname = new URL(lead.url).hostname; } catch(e){}
    
    const statusColor = lead.status === 'completed' ? 'var(--success)' : (lead.status === 'ignored' ? '#ef4444' : 'var(--text-muted)');
    
    let hiringIntent = lead.hiring_intent_found || '';
    let hiringBadge = '';
    if (hiringIntent === 'Yes') {
        hiringBadge = `<span style="font-size:0.75rem; background:#ecfdf5; color:#059669; padding:2px 6px; border-radius:4px; border:1px solid #a7f3d0">🟢 Hiring</span>`;
    }
    
    const techDict = {
        'stripe': 'Takes Online Payments',
        'wordpress': 'Active Content/Blog',
        'shopify': 'E-Commerce Store',
        'salesforce': 'Enterprise CRM',
        'hubspot': 'Marketing Automation',
        'google analytics': 'Tracks Analytics',
        'segment': 'Customer Data Platform',
        'intercom': 'Live Chat Support',
        'react': 'Modern Web App'
    };
    
    let techBadges = (lead.tech_stack_found && lead.tech_stack_found.length > 0) ? lead.tech_stack_found.map(tech => `<span style="font-size:0.75rem; background:transparent; color:#6b7280; padding:2px 6px; border-radius:4px; border:1px solid #e5e7eb">⚡ ${techDict[tech.toLowerCase()] || tech}</span>`).join('') : '';
    let exclusiveBadge = `<span style="font-size:0.75rem; background:#f3e8ff; color:#6b21a8; padding:2px 6px; border-radius:4px; border:1px solid #e9d5ff">🔒 Exclusive Lead</span>`;
    let competitorBadge = lead.competitor_match ? `<span style="font-size:0.75rem; background:#fee2e2; color:#b91c1c; padding:2px 6px; border-radius:4px; border:1px solid #fecaca">🎯 Competitor Intercept: ${lead.competitor_match}</span>` : '';

    let targetExecHtml = (lead.decision_maker_name && lead.decision_maker_name !== 'Unknown') 
        ? `<div style="font-size:0.8rem; margin-top:8px;"><strong>Target Executive:</strong> ${lead.decision_maker_name} (${lead.decision_maker_title || 'Title Unknown'})</div>` 
        : '';
        
    let objectionHtml = (lead.primary_objection_hypothesis && lead.primary_objection_hypothesis !== 'Unknown')
        ? `<div style="background:#fef2f2; color:#991b1b; padding:8px; border-radius:6px; margin-top:8px; font-size:0.8rem; border-left:3px solid #ef4444;"><strong>💡 Strategic Hypothesis:</strong> ${lead.primary_objection_hypothesis}</div>`
        : '';
        
    let sizeBadge = (lead.company_size_tier && lead.company_size_tier !== 'Unknown')
        ? `<span style="font-size:0.75rem; background:#eff6ff; color:#1d4ed8; padding:2px 6px; border-radius:4px; border:1px solid #bfdbfe">🏢 ${lead.company_size_tier}</span>`
        : '';

    return `
        <div class="lead-header">
            <div>
                <strong><a href="${lead.url || '#'}" target="_blank" style="color: var(--text-main); text-decoration: none;">${urlHostname} ↗</a></strong> • ${lead.source || 'Organic Search'} 
                <span style="margin-left:8px; font-size:0.75rem; padding: 2px 6px; border-radius:4px; border: 1px solid ${statusColor}; color: ${statusColor}">${(lead.status || 'new').toUpperCase()}</span>
            </div>
            <div class="score">Score: ${lead.score || 0}/10</div>
        </div>
        ${targetExecHtml}
        <div class="pain-point">" ${lead.pain_point || 'Analyzing sentiment...'} "</div>
        ${objectionHtml}
        <div class="premium-badges" style="margin-top: 8px; margin-bottom: 8px; font-weight: 500; display: flex; flex-wrap: wrap; gap: 6px; align-items: center;">
            ${sizeBadge}
            ${exclusiveBadge}
            ${competitorBadge}
            ${hiringBadge}
            ${techBadges}
        </div>
        <div class="dm-draft">${lead.dm || 'Drafting variation...'}</div>
        <div class="contact-info" style="margin-top: 8px; margin-bottom: 8px; font-size: 0.85rem; color: var(--text-main); font-weight: 500;">
            ${lead.email ? `📧 <a href="mailto:${lead.email}" target="_blank" style="color:#2563eb; text-decoration:none;">${lead.email}</a> &nbsp;` : ''} 
            ${lead.phone ? `📞 <a href="tel:${lead.phone}" style="color:#2563eb; text-decoration:none;">${lead.phone}</a>` : ''}
            ${!lead.email && !lead.phone ? `<span style="color:var(--text-muted); font-style:italic;">No Contact Info Found</span>` : ''}
        </div>
        <div class="action-row" style="flex-wrap: wrap; gap: 8px; margin-top:12px; padding-top:12px; border-top: 1px solid var(--glass-border)">
            <button class="action-btn" onclick="copyMessageAndContact('${docId}', \`${(lead.dm || '').replace(/`/g, '\\\\`').replace(/'/g, "\\'")}\`)" title="Copy Message">📋 Copy Message</button>
            <button class="action-btn" onclick="pushToCRM('${docId}', \`${encodeURIComponent(JSON.stringify(lead)).replace(/'/g, "\\'")}\`)" style="color: #4f46e5; border-color: #c7d2fe; background: #e0e7ff;">☁️ Push to CRM</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'ignored')" title="Ignore Lead">🚫 Ignore</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'converted')" title="Lead Converted">🎯 Converted</button>
            <button class="action-btn" style="background:#f8fafc; color:var(--text-muted); border: 1px solid var(--glass-border);" onclick="viewLeadTimeline('${encodeURIComponent(JSON.stringify(lead.interactions || []))}')" title="Audit Log">🕒 View Timeline Logs</button>
        </div>
    `;
}"""

content = content.replace(old_createLeadCard, new_generateLeadInnerHtml)


# Chunk 2: renderLeads
old_renderLeads = """function renderLeads() {
    const filteredLeads = rawLeadsCache.filter(lead => {
        if (!['new', 'contacted', 'converted'].includes(lead.status || 'new')) return false;
        if (currentCampaignFilter !== 'all' && lead.campaign_id !== currentCampaignFilter) return false;
        return true;
    });
    
    if (filteredLeads.length === 0) {
        leadsList.innerHTML = `
            <div class="lead-card" style="text-align: center; padding: 40px; border: none; background: transparent; box-shadow: none;">
                <div style="font-size: 3rem; margin-bottom: 12px; opacity: 0.8;">⏳</div>
                <h3 style="color: var(--text-main); margin-bottom: 8px;">Hunting for leads...</h3>
                <p style="color: var(--text-muted); font-size: 0.95rem; line-height: 1.5;">
                    We are actively scanning the web for targets matching your criteria. Check back in a few minutes.
                </p>
            </div>
        `;
        return;
    }
    
    // Virtualization / Pagination Window
    const totalPages = Math.ceil(filteredLeads.length / window.leadsPerPage);
    if (window.currentPage > totalPages) window.currentPage = totalPages;
    if (window.currentPage < 1) window.currentPage = 1;
    
    const startIdx = (window.currentPage - 1) * window.leadsPerPage;
    const endIdx = startIdx + window.leadsPerPage;
    const paginatedLeads = filteredLeads.slice(startIdx, endIdx);
    
    leadsList.innerHTML = '';
    paginatedLeads.forEach(lead => leadsList.appendChild(createLeadCard(lead.id || lead.doc_id, lead)));
    
    // Inject Pagination Controls natively
    const paginationControls = document.createElement('div');
    paginationControls.className = 'pagination-controls';
    paginationControls.style = "display:flex; justify-content:space-between; align-items:center; margin-top:20px; padding:16px; background:var(--glass-bg); border-radius:12px; border:1px solid var(--glass-border);";
    
    paginationControls.innerHTML = `
        <button class="secondary-btn" onclick="changeLeadPage(-1)" ${window.currentPage === 1 ? 'disabled style="opacity:0.5; cursor:not-allowed;"' : ''}>← Previous</button>
        <span style="font-size:0.9rem; font-weight:500; color:var(--text-main);">Page ${window.currentPage} of ${totalPages} <span style="color:var(--text-muted);">(${filteredLeads.length} total)</span></span>
        <button class="primary-btn" onclick="changeLeadPage(1)" ${window.currentPage === totalPages ? 'disabled style="opacity:0.5; cursor:not-allowed;"' : ''}>Next →</button>
    `;
    leadsList.appendChild(paginationControls);
}"""

new_renderLeads = """let virtualObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
        if(entry.isIntersecting) {
            if(!entry.target.hasAttribute('data-rendered')) {
                const leadId = entry.target.getAttribute('data-lead-id');
                const lead = rawLeadsCache.find(l => (l.id || l.doc_id) === leadId);
                if (lead) {
                    entry.target.innerHTML = generateLeadInnerHtml(leadId, lead);
                    entry.target.setAttribute('data-rendered', 'true');
                    entry.target.style.height = 'auto';
                }
            }
        } else {
            if(entry.target.hasAttribute('data-rendered')) {
                const rect = entry.target.getBoundingClientRect();
                entry.target.style.height = `${Math.max(150, rect.height)}px`;
                entry.target.innerHTML = '';
                entry.target.removeAttribute('data-rendered');
            }
        }
    });
}, { rootMargin: "800px" });

function renderLeads() {
    const filteredLeads = rawLeadsCache.filter(lead => {
        if (!['new', 'contacted', 'converted'].includes(lead.status || 'new')) return false;
        if (currentCampaignFilter !== 'all' && lead.campaign_id !== currentCampaignFilter) return false;
        return true;
    });
    
    if (filteredLeads.length === 0) {
        leadsList.innerHTML = `
            <div class="lead-card" style="text-align: center; padding: 40px; border: none; background: transparent; box-shadow: none;">
                <div style="font-size: 3rem; margin-bottom: 12px; opacity: 0.8;">⏳</div>
                <h3 style="color: var(--text-main); margin-bottom: 8px;">Hunting for leads...</h3>
                <p style="color: var(--text-muted); font-size: 0.95rem; line-height: 1.5;">
                    We are actively scanning the web for targets matching your criteria. Check back in a few minutes.
                </p>
            </div>
        `;
        return;
    }
    
    leadsList.innerHTML = '';
    virtualObserver.disconnect();
    
    // Strict DOM Virtualization implementation
    filteredLeads.forEach(lead => {
         const wrapper = document.createElement('div');
         wrapper.className = 'lead-card';
         wrapper.style.minHeight = '180px';
         wrapper.id = lead.id || lead.doc_id;
         wrapper.setAttribute('data-lead-id', lead.id || lead.doc_id);
         
         leadsList.appendChild(wrapper);
         virtualObserver.observe(wrapper);
    });
}"""

# A small detail: because `createLeadCard` got replaced, we don't have to worry about replacing `leadsList.appendChild(createLeadCard(...))` inside paginatedLeads loop since we replace the entire `renderLeads` function!

content = content.replace(old_renderLeads, new_renderLeads)

# Chunk 3: copyMessageAndContact
old_copy = """window.copyMessageAndContact = function(docId, dm) {
    navigator.clipboard.writeText(dm).then(() => {
        showToast("Message Copied to Clipboard", "success");
        updateLeadStatus(docId, "contacted");
    }).catch(err => {
        console.error("Clipboard failed", err);
        showToast("Failed to copy", "error");
    });
};"""

new_copy = """window.copyMessageAndContact = function(docId, dm) {
    navigator.clipboard.writeText(dm).then(() => {
        showToast("Message Copied to Clipboard", "success");
        
        // Optimistic UI updates
        rawLeadsCache = rawLeadsCache.filter(l => (l.id || l.doc_id) !== docId);
        const cardEl = document.getElementById(docId);
        if(cardEl) {
            virtualObserver.unobserve(cardEl);
            cardEl.remove();
        }
        
        // Background DB Sync
        updateLeadStatus(docId, "contacted");
    }).catch(err => {
        console.error("Clipboard failed", err);
        showToast("Failed to copy", "error");
    });
};"""

content = content.replace(old_copy, new_copy)

# Chunk 4: Debounce fetchL0Telemetry
old_l0 = """window.l0TelemetryCache = { macro: {}, tenants: [], sortKey: 'leads', sortDesc: true };

window.fetchL0Telemetry = async function() {
    const tableBody = document.getElementById('l0-telemetry-table');
    try {"""

new_l0 = """window.lastL0FetchTime = 0;
window.l0TelemetryCache = { macro: {}, tenants: [], sortKey: 'leads', sortDesc: true };

window.fetchL0Telemetry = async function() {
    const now = Date.now();
    if (now - window.lastL0FetchTime < 30000 && window.l0TelemetryCache.tenants.length > 0) {
        console.log("L0 Telemetry debounced natively.");
        return; // debounce heartbeat
    }
    window.lastL0FetchTime = now;
    
    const tableBody = document.getElementById('l0-telemetry-table');
    try {"""

content = content.replace(old_l0, new_l0)

with open('public/app.js', 'w', encoding='utf-8') as f:
    f.write(content)
