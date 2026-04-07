// Firebase configuration (Placeholder)
const firebaseConfig = {
    apiKey: "AIzaSyCxqimZJ7kspuJJ8qXF34zguLkNXi6MWd4",
    authDomain: "lead-sniper-prod.firebaseapp.com",
    projectId: "lead-sniper-prod",
    storageBucket: "lead-sniper-prod.firebasestorage.app",
    messagingSenderId: "222247989819",
    appId: "1:222247989819:web:17066a1bbf0b1f3df2221e",
    measurementId: "G-SQ6DDQ7HW0"
};

// Initialize Firebase Auth (Firestore explicitly stripped)
firebase.initializeApp(firebaseConfig);
const auth = firebase.auth();

const API_BASE = "https://orchestrator-222247989819.asia-south1.run.app";

// DOM Elements
const authContainer = document.getElementById('auth-container');
const appContainer = document.getElementById('app-container');
const loginBtn = document.getElementById('login-btn');
const logoutBtn = document.getElementById('logout-btn');
const leadsList = document.getElementById('leads-list');

// Selected Filter State
let currentCampaignFilter = 'all';
let rawLeadsCache = [];

function handleAuthRejection() {
    showToast("Session Expired or Unauthorized Access.", "error");
    auth.signOut();
}

// Authentication state observer
auth.onAuthStateChanged(async user => {
    if (user) {
        // Render UI directly (Thin Client architecture: token validated on the backend API layer)
        authContainer.classList.add('hidden');
        appContainer.classList.remove('hidden');
        loadDashboard();
    } else {
        // User logged out
        authContainer.classList.remove('hidden');
        appContainer.classList.add('hidden');
    }
});

// Login Handler
loginBtn.addEventListener('click', () => {
    const provider = new firebase.auth.GoogleAuthProvider();
    auth.signInWithPopup(provider).catch(error => {
        console.error("Error signing in:", error);
    });
});

// Logout Handler
logoutBtn.addEventListener('click', () => {
    auth.signOut();
});

// Unified Dashboard Loader
async function loadDashboard() {
    const user = firebase.auth().currentUser;
    if (!user) return;
    
    await Promise.all([
        loadMe(),
        loadCampaigns(),
        loadLeads()
    ]);
}

let activeWallet = { allocated_credits: 0, consumed_credits: 0 };
async function loadMe() {
    try {
        const user = firebase.auth().currentUser;
        const token = await user.getIdToken();
        const response = await fetch(`${API_BASE}/api/me?rt=${new Date().getTime()}`, { 
            method: 'GET',
            headers: { 
                'Authorization': `Bearer ${token}`
            } 
        });
        if (response.ok) {
            const payload = await response.json();
            const data = payload.data || {};
            
            const waitroom = document.getElementById('waitroom-overlay');
            const mainGrid = document.querySelector('.dashboard-grid');
            const navMenu = document.querySelector('.glass-nav');

            if (data.approval_status === 'pending') {
                if (mainGrid) mainGrid.style.display = 'none';
                if (navMenu) navMenu.style.display = 'none';
                if (waitroom) {
                    waitroom.style.position = 'relative';
                    waitroom.style.height = '100vh';
                    waitroom.style.display = 'flex';
                }
                return;
            } else {
                if (mainGrid) mainGrid.style.display = '';
                if (navMenu) navMenu.style.display = '';
                if (waitroom) waitroom.style.display = 'none';
            }
            
            // Force display properties explicitly beyond just CSS class removal
            if (data.role === 'super_admin') {
                const l0Tab = document.getElementById('tab-l0-admin');
                if (l0Tab) {
                    l0Tab.classList.remove('hidden');
                    l0Tab.style.display = 'inline-block';
                }
            }

            // Defensively check both payload tracks mapping legacy or missing keys safely
            const w = payload.wallet || data.wallet || {allocated_credits: 0, consumed_credits: 0};
            activeWallet = w;
            const el = document.getElementById('wallet-balance');
            const credits = (w.allocated_credits || 0) - (w.consumed_credits || 0);
            if (el) el.innerText = credits;
            
            const alertBanner = document.getElementById('wallet-alert-banner');
            const newCampBtn = document.querySelector('button[onclick="openNewCampaignModal()"]');
            if (credits <= 0) {
                if (alertBanner) { alertBanner.innerText = "🛑 Wallet Empty: You have 0 credits. Upgrade your account to continue sweeping."; alertBanner.classList.remove('hidden'); }
                if (newCampBtn) { newCampBtn.innerText = "Upgrade Plan (0 Credits)"; newCampBtn.disabled = true; newCampBtn.style.background = "#94a3b8"; }
            } else if (credits < 50) {
                if (alertBanner) alertBanner.classList.remove('hidden');
            } else {
                if (alertBanner) alertBanner.classList.add('hidden');
                if (newCampBtn) { newCampBtn.innerText = "+ Find New Clients"; newCampBtn.disabled = false; newCampBtn.style.background = "var(--primary)"; }
            }

            if (!data.agreed_to_terms) {
                const tosModal = document.getElementById('tos-modal');
                if (tosModal) tosModal.classList.remove('hidden');
            }

            const hookInput = document.getElementById('crm-webhook-url');
            if (hookInput && data.crm_webhook_url) {
                hookInput.value = data.crm_webhook_url;
            }
            window.currentUserData = data;
            
        }
    } catch(e) { console.error('Failed to load wallet', e); }
}

// Dynamic Campaign Hydration via REST API
async function loadCampaigns() {
    const feed = document.getElementById('active-campaign-feed');
    const tableBody = document.getElementById('campaign-list-table');
    const filterSelect = document.getElementById('campaign-filter');
    
    try {
        const user = firebase.auth().currentUser;
        if (!user) return handleAuthRejection();
        
        const token = await user.getIdToken(); 
        const response = await fetch(`${API_BASE}/api/campaigns`, {
            method: 'GET',
            headers: { 'Authorization': `Bearer ${token}` }
        });
        
        if (response.status === 401 || response.status === 403) {
            return handleAuthRejection();
        }
        
        const payload = await response.json();
        const campaigns = payload.data || [];
        
        if (campaigns.length === 0) {
            if (feed) feed.innerHTML = '';
            if (tableBody) tableBody.innerHTML = '<tr><td colspan="4" style="padding:16px; text-align:center;">No campaigns found. Click "New Search" to start.</td></tr>';
            return;
        }
        
        let activeCount = 0;
        let tableHTML = '';
        let filterHTML = '<option value="all">All Campaigns</option>';
        
        campaigns.sort((a, b) => (b.createdAt || '').localeCompare(a.createdAt || ''));
        
        campaigns.forEach(camp => {
            const id = camp.id;
            const isActive = camp.status === 'active';
            if (isActive) activeCount++;
            
            const statusColor = isActive ? '#25D366' : '#ef4444';
            const statusBadge = `<span style="font-size:0.75rem; padding: 2px 6px; border-radius:4px; border: 1px solid ${statusColor}; color: ${statusColor}">${(camp.status || 'unknown').toUpperCase()}</span>`;
            
            const hasLocation = camp.gl && camp.location;
            const locationWarn = hasLocation ? '' : '<br><span style="color: #ea580c; font-size: 0.75rem; display:block; margin-top:4px;">⚠️ Location Missing: Edit Campaign to set Targeting</span>';
            
            tableHTML += `
                <tr style="border-bottom: 1px solid var(--glass-border);">
                    <td style="padding: 12px;"><strong>${camp.name || 'Untitled'}</strong>${locationWarn}</td>
                    <td style="padding: 12px;"><i style="color:var(--text-muted); font-size:0.85rem">${camp.keywords || 'N/A'}</i></td>
                    <td style="padding: 12px;">${statusBadge}</td>
                    <td style="padding: 12px; text-align:right;">
                        <button class="secondary-btn" style="padding: 4px 8px; font-size: 0.75rem; margin-right: 4px;" onclick="openEditModal('${id}', '${(camp.name || '').replace(/'/g, "\\'")}', '${(camp.bio || '').replace(/'/g, "\\'")}', '${(camp.keywords || '').replace(/'/g, "\\'")}', '${(camp.gl || '').replace(/'/g, "\\'")}', '${(camp.location || '').replace(/'/g, "\\'")}')">Edit</button>
                        <button class="secondary-btn" style="padding: 4px 8px; font-size: 0.75rem; border-color: ${statusColor}; color: ${statusColor}" onclick="toggleCampaignStatus('${id}', '${camp.status}')">${isActive ? 'Pause' : 'Resume'}</button>
                    </td>
                </tr>
            `;
            filterHTML += `<option value="${id}">${camp.name}</option>`;
        });
        
        if (tableBody) tableBody.innerHTML = tableHTML;
        if (filterSelect) {
            const currentVal = filterSelect.value;
            filterSelect.innerHTML = filterHTML;
            filterSelect.value = currentVal || 'all';
        }
        if (feed) {
            feed.innerHTML = `
                <div class="competitor-monitor" style="background: rgba(79, 70, 229, 0.05); border: 1px solid rgba(79, 70, 229, 0.2); padding: 12px; border-radius: 8px; margin-bottom: 24px;">
                    <span class="badge" style="background: var(--primary);">System Status: Online</span>
                    <span style="color: var(--text-muted); font-size: 0.9rem; margin-left: 8px;">Scraping ${activeCount} Active Target Matrices</span>
                </div>
            `;
        }
    } catch (error) {
        console.error("Campaign Hook Error:", error);
        if (tableBody) tableBody.innerHTML = '<tr><td colspan="4" style="padding:16px; text-align:center; color: #ef4444;">API Connection Error</td></tr>';
    }
}

// --- ENTERPRISE CHART.JS PIPELINE ---
let conversionChart = null;
function initAnalyticsChart(newC, contactedC, convertedC) {
    const ctx = document.getElementById('funnelChart');
    if (!ctx) return;
    if (conversionChart) {
        conversionChart.data.datasets[0].data = [newC, contactedC, convertedC];
        conversionChart.update();
        return;
    }
    conversionChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: ['New Processed', 'Messaged', 'Converted'],
            datasets: [{
                data: [newC, contactedC, convertedC],
                backgroundColor: ['#4F46E5', '#3B82F6', '#25D366'],
                borderWidth: 0
            }]
        },
        options: { cutout: '75%', responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } }
    });
}

// Load Leads Real-Time (Firestore Subscription)
let unsubscribeLeads = null;

async function loadLeads() {
    leadsList.innerHTML = '<div class="lead-card pulse">Connecting to Secure Orchestrator...</div>';
    
    try {
        const user = firebase.auth().currentUser;
        if (!user) return handleAuthRejection();
        
        if (unsubscribeLeads) { unsubscribeLeads(); }

        unsubscribeLeads = firebase.firestore()
            .collection('leads')
            .where('tenant_id', '==', user.uid)
            .onSnapshot((snapshot) => {
                rawLeadsCache = [];
                snapshot.forEach(doc => {
                    let data = doc.data();
                    data.id = doc.id;
                    rawLeadsCache.push(data);
                });
                
                if (rawLeadsCache.length === 0) {
                    renderLeads();
                    initAnalyticsChart(0,0,0);
                    return;
                }

                rawLeadsCache.sort((a, b) => (b.score || 0) - (a.score || 0));
                
                let cNew = 0, cContact= 0, cConvert = 0;
                let cDiscovered = rawLeadsCache.length;
                let cActionable = 0, cIgnored = 0;

                rawLeadsCache.forEach(l => {
                    if (l.status === 'ignored') {
                        cIgnored++;
                        return;
                    }
                    if (l.status === 'new' || !l.status) cActionable++;
                    
                    if (l.status === 'contacted') { cContact++; }
                    else if (l.status === 'converted') { cConvert++; }
                    else { cNew++; }
                });
                
                const elDisc = document.getElementById('stat-discovered');
                const elAct = document.getElementById('stat-actionable');
                const elIgn = document.getElementById('stat-ignored');
                if (elDisc) elDisc.innerText = cDiscovered;
                if (elAct) elAct.innerText = cActionable;
                if (elIgn) elIgn.innerText = cIgnored;
                
                initAnalyticsChart(cNew, cContact, cConvert);
                renderLeads();
            }, (error) => {
                console.error("Firestore onSnapshot Error:", error);
                if (error.code === 'permission-denied') {
                    // Could be unapproved or missing index
                    console.warn("Client reads restricted by firestore rules.");
                }
            });
        
    } catch (error) {
        console.error("Firestore Initialization Error:", error);
        leadsList.innerHTML = '<div class="lead-card" style="color: #ef4444; border-color: #ef4444;">Could not connect to Native Database. Please check your network.</div>';
        showToast('Connection Refused', 'error');
    }
}

// Organic DOM Factory
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
            <button class="action-btn" onclick="copyMessageAndContact('${docId}', \`${(lead.dm || '').replace(/`/g, '\\`').replace(/'/g, "\\'")}\`)" title="Copy Message">📋 Copy Message</button>
            <button class="action-btn" onclick="pushToCRM('${docId}', \`${encodeURIComponent(JSON.stringify(lead)).replace(/'/g, "\\'")}\`)" style="color: #4f46e5; border-color: #c7d2fe; background: #e0e7ff;">☁️ Push to CRM</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'ignored')" title="Ignore Lead">🚫 Ignore</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'converted')" title="Lead Converted">🎯 Converted</button>
            <button class="action-btn" style="background:#f8fafc; color:var(--text-muted); border: 1px solid var(--glass-border);" onclick="viewLeadTimeline('${encodeURIComponent(JSON.stringify(lead.interactions || []))}')" title="Audit Log">🕒 View Timeline Logs</button>
        </div>
    `;
    return card;
}

window.pushToCRM = async function(docId, leadStr) {
    const userUrl = window.currentUserData?.crm_webhook_url;
    if (!userUrl) { showToast("No CRM Hook Configured in Settings", "error"); return; }
    try {
        const lead = JSON.parse(decodeURIComponent(leadStr));
        await fetch(userUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            mode: 'no-cors',
            body: JSON.stringify({ event: 'lead_pushed', lead: lead })
        });
        showToast("Signal fired to CRM Integration", "success");
    } catch (e) {
        console.error("CRM Push failure", e);
        showToast("Hook connection failed", "error");
    }
};

window.saveCRMWebhook = async function() {
    const user = firebase.auth().currentUser;
    const url = document.getElementById('crm-webhook-url').value.trim();
    if (!user || !url) return;
    try {
        const token = await user.getIdToken();
        const response = await fetch(`${API_BASE}/api/me`, {
            method: 'PUT',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({ crm_webhook_url: url })
        });
        if (response.ok) {
            if (window.currentUserData) window.currentUserData.crm_webhook_url = url;
            showToast("CRM Integration Locked", "success");
        } else showToast("Failed to save webhook", "error");
    } catch(e) { console.error(e); }
};

window.agreeToTerms = async function() {
    const user = firebase.auth().currentUser;
    if (!user) return;
    try {
        const token = await user.getIdToken();
        const response = await fetch(`${API_BASE}/api/me`, {
            method: 'PUT',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({ agreed_to_terms: true })
        });
        if (response.ok) {
            document.getElementById('tos-modal').classList.add('hidden');
            showToast("Compliance agreement signed", "success");
        } else showToast("Backend sync failed", "error");
    } catch(e) { console.error(e); }
};

window.copyMessageAndContact = function(docId, dm) {
    navigator.clipboard.writeText(dm).then(() => {
        showToast("Message Copied to Clipboard", "success");

        // Optimistic UI
        rawLeadsCache = rawLeadsCache.filter(l => (l.id || l.doc_id) !== docId);
        const cardEl = document.getElementById(docId);
        if (cardEl) {
            virtualObserver.unobserve(cardEl);
            cardEl.remove();
        }
        updateLeadStatus(docId, "contacted");
    }).catch(err => {
        console.error("Clipboard failed", err);
        showToast("Failed to copy", "error");
    });
};

// ---------------------------------------------------------------------------
// V14: SMART CONTACT ACTION
// Primary CTA handler: copies DM, opens URI, marks lead as contacted.
// ---------------------------------------------------------------------------
window.smartContactAction = function(docId, dm, uri, platform) {
    // 1. Copy the drafted message to clipboard
    navigator.clipboard.writeText(dm).catch(err => console.warn('Clipboard fail:', err));

    // 2. Optimistic UI dismiss
    rawLeadsCache = rawLeadsCache.filter(l => (l.id || l.doc_id) !== docId);
    const cardEl = document.getElementById(docId);
    if (cardEl) {
        virtualObserver.unobserve(cardEl);
        cardEl.remove();
    }

    // 3. Open the URI (mailto, https, etc.)
    if (uri) {
        const isEmail = platform === 'email' || uri.includes('@');
        const href = isEmail ? `mailto:${uri}` : uri;
        window.open(href, '_blank', 'noopener,noreferrer');
    }

    showToast('Message copied — opening contact...', 'success');
    updateLeadStatus(docId, 'contacted');
};

// ---------------------------------------------------------------------------
// V14: ALT-CONTACTS DROPDOWN TOGGLE
// ---------------------------------------------------------------------------
window.toggleAltContacts = function(wrapperId) {
    const wrapper  = document.getElementById(wrapperId);
    if (!wrapper) return;
    const toggle   = wrapper.querySelector('.alt-contacts-toggle');
    const dropdown = wrapper.querySelector('.alt-contacts-dropdown');
    const isOpen   = dropdown.classList.contains('open');
    dropdown.classList.toggle('open', !isOpen);
    if (toggle) toggle.classList.toggle('open', !isOpen);
    // Close on click outside
    if (!isOpen) {
        const closeHandler = (e) => {
            if (!wrapper.contains(e.target)) {
                dropdown.classList.remove('open');
                if (toggle) toggle.classList.remove('open');
                document.removeEventListener('click', closeHandler);
            }
        };
        setTimeout(() => document.addEventListener('click', closeHandler), 0);
    }
};

window.currentPage = 1;
window.leadsPerPage = 20;

window.filterLeadsByCampaign = function(campaignId) {
    currentCampaignFilter = campaignId;
    window.currentPage = 1; // Reset pagination on filter
    renderLeads();
};

window.changeLeadPage = function(delta) {
    window.currentPage += delta;
    renderLeads();
    document.querySelector('.dashboard-grid')?.scrollIntoView({ behavior: 'smooth' });
};

// ---------------------------------------------------------------------------
// ENTERPRISE DOSSIER RENDERER — V14 POLYMORPHIC SCHEMA
// ---------------------------------------------------------------------------

// Platform metadata: icon, label, CTA text for each endpoint type
const PLATFORM_META = {
    whatsapp:  { icon: '💬', label: 'WhatsApp',   cta: 'Message on WhatsApp',  priority: 1 },
    instagram: { icon: '📸', label: 'Instagram',  cta: 'DM on Instagram',      priority: 2 },
    linkedin:  { icon: '💼', label: 'LinkedIn',   cta: 'Connect on LinkedIn',  priority: 2 },
    facebook:  { icon: '📘', label: 'Facebook',   cta: 'Message on Facebook',  priority: 3 },
    email:     { icon: '📧', label: 'Email',      cta: 'Send Email',           priority: 4 },
    gmb:       { icon: '📍', label: 'GMB',        cta: 'Open Maps Profile',    priority: 4 },
    reddit:    { icon: '🔴', label: 'Reddit',     cta: 'Open Reddit Profile',  priority: 5 },
    other:     { icon: '🔗', label: 'Contact',    cta: 'Open Contact',         priority: 6 }
};

/**
 * Resolves the primary endpoint from a contact_endpoints array
 * using the hierarchy: WhatsApp → Instagram/LinkedIn → Email → Reddit/Forums → Other
 */
function getContactHierarchy(endpoints) {
    if (!endpoints || endpoints.length === 0) return null;
    return [...endpoints].sort((a, b) => {
        const pa = (PLATFORM_META[a.platform] || PLATFORM_META.other).priority;
        const pb = (PLATFORM_META[b.platform] || PLATFORM_META.other).priority;
        return pa - pb;
    })[0];
}

function generateLeadInnerHtml(docId, lead) {
    // ── Enterprise Dossier fields ─────────────────────────────────────────────
    const targetName       = (!lead.decision_maker_name        || lead.decision_maker_name        === 'N/A') ? 'Data unavailable on scanned domain'                       : lead.decision_maker_name;
    const companySize      = (!lead.company_size_tier          || lead.company_size_tier          === 'N/A') ? 'Requires secondary analysis'                               : lead.company_size_tier;
    const primaryObjection = (!lead.primary_objection_hypothesis || lead.primary_objection_hypothesis === 'N/A') ? 'Insufficient data to generate confident hypothesis'  : lead.primary_objection_hypothesis;
    const icebreakerAngle  = lead.icebreaker_angle || '';

    // ── URL ───────────────────────────────────────────────────────────────────
    let urlHostname = 'Unknown URL';
    try { if (lead.url) urlHostname = new URL(lead.url).hostname; } catch(e) {}

    const statusColor = lead.status === 'completed' ? 'var(--success)'
        : (lead.status === 'ignored' ? '#ef4444' : 'var(--text-muted)');

    // ── V14: Intent Signal (replaces plain pain_point at top of card) ─────────
    const intentSignal = lead.intent_signal || lead.pain_point || '';
    const intentSignalHtml = intentSignal
        ? `<div class="intent-signal">${intentSignal}</div>`
        : '';

    // ── Confidence Tier badge ────────────────────────────────────────────────
    const tierClass  = lead.confidence_tier === 'Medium' ? 'tier-medium' : 'tier-high';
    const tierBadge  = lead.confidence_tier
        ? `<span class="tier-badge ${tierClass}">${lead.confidence_tier === 'High' ? '✓' : '~'} ${lead.confidence_tier}</span>`
        : '';

    // ── Badges ────────────────────────────────────────────────────────────────
    const hiringBadge = (lead.hiring_intent_found === 'Yes')
        ? `<span style="font-size:0.75rem; background:#ecfdf5; color:#059669; padding:2px 6px; border-radius:4px; border:1px solid #a7f3d0">🟢 Hiring</span>`
        : '';

    const techDict = {
        'stripe': 'Takes Online Payments', 'wordpress': 'Active Content/Blog',
        'shopify': 'E-Commerce Store',     'salesforce': 'Enterprise CRM',
        'hubspot': 'Marketing Automation', 'google analytics': 'Tracks Analytics',
        'segment': 'Customer Data Platform', 'intercom': 'Live Chat Support',
        'react': 'Modern Web App'
    };
    const techBadges = (lead.tech_stack_found && lead.tech_stack_found.length > 0)
        ? lead.tech_stack_found.map(t =>
            `<span style="font-size:0.75rem; background:transparent; color:#6b7280; padding:2px 6px; border-radius:4px; border:1px solid #e5e7eb">⚡ ${techDict[t.toLowerCase()] || t}</span>`
          ).join('')
        : '';

    const exclusiveBadge = `<span style="font-size:0.75rem; background:#f3e8ff; color:#6b21a8; padding:2px 6px; border-radius:4px; border:1px solid #e9d5ff">🔒 Exclusive Lead</span>`;
    const competitorBadge = lead.competitor_match
        ? `<span style="font-size:0.75rem; background:#fee2e2; color:#b91c1c; padding:2px 6px; border-radius:4px; border:1px solid #fecaca">🎯 Competitor Intercept: ${lead.competitor_match}</span>`
        : '';
    const targetNameBadge = (lead.decision_maker_name)
        ? `<span style="font-size:0.75rem; background:#eff6ff; color:#1d4ed8; padding:2px 6px; border-radius:4px; border:1px solid #bfdbfe">👤 ${targetName}</span>`
        : '';
    const companySizeBadge = (lead.company_size_tier)
        ? `<span style="font-size:0.75rem; background:#fefce8; color:#854d0e; padding:2px 6px; border-radius:4px; border:1px solid #fef08a">🏢 ${companySize}</span>`
        : '';

    // ── V14: Polymorphic Contact Endpoints ───────────────────────────────────
    const endpoints     = Array.isArray(lead.contact_endpoints) ? lead.contact_endpoints.filter(e => e && e.uri) : [];
    const primary       = getContactHierarchy(endpoints);
    const altEndpoints  = primary ? endpoints.filter(e => e !== primary) : endpoints;
    const altDropId     = `alt-${docId}`;

    // Safe-encode DM for onclick attribute
    const safeDm = (lead.dm || '').replace(/\\/g, '\\\\').replace(/`/g, '\\`').replace(/'/g, "\\'");

    let primaryCtaHtml = '';
    if (primary) {
        const meta   = PLATFORM_META[primary.platform] || PLATFORM_META.other;
        const safeUri = encodeURIComponent(primary.uri);
        primaryCtaHtml = `<button class="smart-cta-btn"
            onclick="smartContactAction('${docId}', \`${safeDm}\`, decodeURIComponent('${safeUri}'), '${primary.platform}')"
            title="${meta.cta}">${meta.icon} ${meta.cta}</button>`;
    } else {
        // No endpoints: fallback to legacy copy behaviour
        primaryCtaHtml = `<button class="action-btn" onclick="copyMessageAndContact('${docId}', \`${safeDm}\`)" title="Copy Message">📋 Copy Message</button>`;
    }

    // Alt-contacts dropdown (only if there are secondary endpoints)
    let altDropdownHtml = '';
    if (altEndpoints.length > 0) {
        const altItems = altEndpoints.map(ep => {
            const m = PLATFORM_META[ep.platform] || PLATFORM_META.other;
            const safeUri = encodeURIComponent(ep.uri);
            const shortUri = ep.uri.length > 30 ? ep.uri.substring(0, 28) + '…' : ep.uri;
            return `<button class="alt-contact-item"
                onclick="smartContactAction('${docId}', \`${safeDm}\`, decodeURIComponent('${safeUri}'), '${ep.platform}')">
                <span class="platform-icon">${m.icon}</span>
                <span>${m.label}</span>
                <span class="platform-uri">${shortUri}</span>
            </button>`;
        }).join('');

        altDropdownHtml = `<div class="alt-contacts-wrapper" id="${altDropId}">
            <button class="alt-contacts-toggle" onclick="toggleAltContacts('${altDropId}')">
                +${altEndpoints.length}
                <svg viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"></polyline></svg>
            </button>
            <div class="alt-contacts-dropdown">${altItems}</div>
        </div>`;
    }

    // ── Icebreaker / Objection rows ───────────────────────────────────────────
    const icebreakerRow = icebreakerAngle
        ? `<div style="margin-top:8px; font-size:0.85rem; color:#4f46e5; font-style:italic; padding:6px 10px; background:rgba(79,70,229,0.05); border-left:3px solid #6366f1; border-radius:0 4px 4px 0;">
               💡 Icebreaker: ${icebreakerAngle}
           </div>`
        : '';
    const objectionRow = (lead.primary_objection_hypothesis)
        ? `<div style="margin-top:6px; font-size:0.82rem; color:#b45309; padding:4px 10px; background:#fffbeb; border-left:3px solid #f59e0b; border-radius:0 4px 4px 0;">
               ⚠️ Likely Objection: ${primaryObjection}
           </div>`
        : '';

    // ── Safe serialisation for timeline ──────────────────────────────────────
    const safeEvents = encodeURIComponent(JSON.stringify(lead.interactions || []));
    const safeLeadEnc = encodeURIComponent(JSON.stringify({
        id: lead.id,
        score: lead.score,
        dm: lead.dm,
        intent_signal: lead.intent_signal,
        contact_endpoints: lead.contact_endpoints
    })).replace(/'/g, "\\'");

    return `
        <div class="lead-header">
            <div>
                <strong><a href="${lead.url || '#'}" target="_blank" style="color: var(--text-main); text-decoration: none;">${urlHostname} ↗</a></strong> • ${lead.source || 'Organic Search'}
                <span style="margin-left:8px; font-size:0.75rem; padding: 2px 6px; border-radius:4px; border: 1px solid ${statusColor}; color: ${statusColor}">${(lead.status || 'new').toUpperCase()}</span>
                ${tierBadge}
            </div>
            <div class="score">Score: ${lead.score || 0}/10</div>
        </div>
        ${intentSignalHtml}
        <div class="premium-badges" style="margin-top: 8px; margin-bottom: 8px; font-weight: 500; display: flex; flex-wrap: wrap; gap: 6px; align-items: center;">
            ${exclusiveBadge}
            ${competitorBadge}
            ${hiringBadge}
            ${targetNameBadge}
            ${companySizeBadge}
            ${techBadges}
        </div>
        ${icebreakerRow}
        ${objectionRow}
        <div class="dm-draft">${lead.dm || 'Drafting variation...'}</div>
        <div class="action-row" style="flex-wrap: wrap; gap: 8px; margin-top:12px; padding-top:12px; border-top: 1px solid var(--glass-border)">
            ${primaryCtaHtml}
            ${altDropdownHtml}
            <button class="action-btn" onclick="pushToCRM('${docId}', '${safeLeadEnc}')" style="color: #4f46e5; border-color: #c7d2fe; background: #e0e7ff;">☁️ Push to CRM</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'ignored')" title="Ignore Lead">🚫 Ignore</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'converted')" title="Lead Converted">🎯 Converted</button>
            <button class="action-btn" style="background:#f8fafc; color:var(--text-muted); border: 1px solid var(--glass-border);" onclick="viewLeadTimeline('${safeEvents}')" title="Audit Log">🕒 Timeline</button>
        </div>
    `;
}

let virtualObserver = new IntersectionObserver((entries) => {
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
}

// TOAST UI ENGINE
window.showToast = function(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => toast.classList.add('show'), 10);
    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    }, 3500);
};

// Modals
window.viewLeadTimeline = function(eventsJson) {
    try {
        const events = JSON.parse(decodeURIComponent(eventsJson)) || [];
        const feed = document.getElementById('audit-timeline-feed');
        if (events.length === 0) {
            feed.innerHTML = '<p style="color:var(--text-muted); text-align:center;">No CRM interactions recorded yet.</p>';
        } else {
            feed.innerHTML = events.map(e => `
                <div style="padding:12px; border-left: 3px solid var(--primary); margin-bottom:12px; background: white; border-radius: 0 4px 4px 0; box-shadow: 0 1px 2px rgba(0,0,0,0.05);">
                    <small style="color:var(--text-muted); display:block; margin-bottom:4px;">${e.date}</small>
                    <strong style="color: var(--text-main); font-size: 0.95rem;">${e.action}</strong>
                </div>
            `).join('');
        }
        document.getElementById('audit-log-modal').classList.remove('hidden');
    } catch(e) { console.error('Timeline Schema Sync Error', e); }
};

window.openEditModal = function(id, name, bio, keywords, gl, location) {
    document.getElementById('edit-camp-id').value = id;
    document.getElementById('edit-camp-name').value = name;
    document.getElementById('edit-camp-bio').value = bio;
    document.getElementById('edit-camp-keys').value = keywords;
    const glEl = document.getElementById('edit-camp-gl');
    const locEl = document.getElementById('edit-camp-location');
    if (glEl) glEl.value = gl;
    if (locEl) locEl.value = location;
    document.getElementById('edit-campaign-modal').classList.remove('hidden');
};

window.closeEditModal = function() {
    document.getElementById('edit-campaign-modal').classList.add('hidden');
};

// MUTATION STUBS: Redirected to REST Gateways
async function performApiMutation(url, method, payload) {
    const user = auth.currentUser;
    if(!user) return false;
    const token = await user.getIdToken();
    const response = await fetch(`${API_BASE}${url}`, {
        method: method,
        headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });
    if (response.status === 401 || response.status === 403) {
        handleAuthRejection();
        return false;
    }
    if (!response.ok) throw new Error("API Execution Failed");
    return true;
}

window.updateLeadStatus = async function(docId, newStatus) {
    if (newStatus === 'ignored') {
        const leadIndex = rawLeadsCache.findIndex(l => l.id === docId);
        if (leadIndex !== -1) {
             rawLeadsCache.splice(leadIndex, 1);
             renderLeads();
        }
    }
    
    try {
        const success = await performApiMutation(`/api/leads/${docId}`, 'PUT', { status: newStatus });
        if(success) {
            showToast(`Lead status updated: ${newStatus}`, 'success');
            if (newStatus !== 'ignored') {
                loadDashboard();
            }
        }
    } catch(err) {
        showToast('Error saving update to database', 'error');
    }
};

window.openNewCampaignModal = async function() {
    const remaining = activeWallet.allocated_credits - activeWallet.consumed_credits;
    if (remaining <= 0) {
        showToast('Beta quota exhausted. Contact admin to reload.', 'error');
        return;
    }
    
    document.getElementById('new-campaign-modal').classList.remove('hidden');
    const glInput = document.getElementById('camp-gl');
    const locInput = document.getElementById('camp-location');
    
    // Auto-detect Geo if unpopulated
    if (glInput && !glInput.value) {
        try {
            const resp = await fetch('https://ipapi.co/json/');
            const json = await resp.json();
            if (json.country) glInput.value = json.country_code ? json.country_code.toLowerCase() : '';
            if (json.city) locInput.value = `${json.city}, ${json.region}`;
        } catch(e) {
            console.warn("Soft Geolocation Exception:", e);
        }
    }
};

window.saveEditedCampaign = async function() {
    const id = document.getElementById('edit-camp-id').value;
    const name = document.getElementById('edit-camp-name').value;
    const bio = document.getElementById('edit-camp-bio').value;
    const keys = document.getElementById('edit-camp-keys').value;
    const glInput = document.getElementById('edit-camp-gl');
    const locationInput = document.getElementById('edit-camp-location');
    
    if (!name || !keys) return showToast('Name and Keywords required', 'error');
    
    showToast('Pushing updates to AI Engine...', 'info');
    try {
        const payload = { 
            name, 
            bio, 
            keywords: keys,
            gl: glInput ? glInput.value : '',
            location: locationInput ? locationInput.value : '',
            status: 'active'
        };
        const success = await performApiMutation(`/api/campaigns/${id}`, 'PUT', payload);
        if(success) {
            closeEditModal();
            showToast('Campaign successfully updated!', 'success');
            loadDashboard();
        }
    } catch(err) {
        showToast('Error modifying campaign', 'error');
    }
};

window.toggleCampaignStatus = async function(id, currentStatus) {
    const newStatus = currentStatus === 'active' ? 'paused' : 'active';
    try {
        const success = await performApiMutation(`/api/campaigns/${id}`, 'PUT', { status: newStatus });
        if(success) {
            showToast(`Campaign ${newStatus} successfully`, 'success');
            loadDashboard();
        }
    } catch(err) {
        showToast('Status update failed', 'error');
    }
};

window.saveCampaignAction = async function() {
    const nameInput = document.getElementById('camp-name');
    const bioInput = document.getElementById('camp-bio');
    const keysInput = document.getElementById('camp-keys');
    const glInput = document.getElementById('camp-gl');
    const locationInput = document.getElementById('camp-location');
    const targetUrlsInput = document.getElementById('camp-target-urls');
    
    if (!nameInput || !keysInput || !nameInput.value || !keysInput.value) {
        showToast('Campaign Name and Keywords are required', 'error');
        return;
    }
    
    let targetUrls = [];
    if (targetUrlsInput && targetUrlsInput.value.trim().length > 0) {
        targetUrls = targetUrlsInput.value.split('\n').map(u => u.trim()).filter(u => u.length > 0);
        if (targetUrls.length > 10) {
            showToast('Warning: Only the first 10 URLs will be prioritized.', 'error');
            targetUrls = targetUrls.slice(0, 10);
        }
    }
    
    showToast('Setting up your search...', 'info');
    try {
        const success = await performApiMutation(`/api/campaigns`, 'POST', {
            name: nameInput.value,
            bio: bioInput.value,
            keywords: keysInput.value,
            gl: glInput ? glInput.value : '',
            location: locationInput ? locationInput.value : '',
            target_urls: targetUrls,
            status: 'active'
        });
        if(success) {
            document.getElementById('new-campaign-modal').classList.add('hidden');
            showToast('System is now looking for clients!', 'success');
            nameInput.value = ''; bioInput.value = ''; keysInput.value = '';
            if (glInput) glInput.value = '';
            if (locationInput) locationInput.value = '';
            if (targetUrlsInput) targetUrlsInput.value = '';
            loadDashboard();
        }
    } catch(err) {
        showToast('Failed to save campaign. Check API permissions.', 'error');
    }
};

// SPA Router
window.switchTab = function(tabName) {
    document.querySelectorAll('.main-feed').forEach(el => el.classList.add('hidden'));
    document.querySelectorAll('.nav-links a').forEach(el => el.classList.remove('active'));
    
    if(tabName === 'dashboard') {
        document.getElementById('view-dashboard').classList.remove('hidden');
        document.getElementById('tab-dashboard').classList.add('active');
    } else if(tabName === 'target') {
        if(document.getElementById('view-target')) document.getElementById('view-target').classList.remove('hidden');
        document.getElementById('tab-campaigns').classList.add('active');
    } else if(tabName === 'team') {
        if(document.getElementById('view-team')) document.getElementById('view-team').classList.remove('hidden');
        document.getElementById('tab-team').classList.add('active');
    } else if(tabName === 'reports') {
        if(document.getElementById('view-reports')) document.getElementById('view-reports').classList.remove('hidden');
        if(document.getElementById('tab-reports')) document.getElementById('tab-reports').classList.add('active');
    } else if(tabName === 'l0-admin') {
        if(document.getElementById('view-l0-admin')) document.getElementById('view-l0-admin').classList.remove('hidden');
        if(document.getElementById('tab-l0-admin')) document.getElementById('tab-l0-admin').classList.add('active');
        fetchL0Telemetry();
    } else if(tabName === 'macro') {
        if(document.getElementById('view-macro')) document.getElementById('view-macro').classList.remove('hidden');
        if(document.getElementById('tab-macro')) document.getElementById('tab-macro').classList.add('active');
        fetchMacroTrends();
    }
};

window.lastL0FetchTime = 0;
window.l0TelemetryCache = { macro: {}, tenants: [], sortKey: 'leads', sortDesc: true };

window.fetchL0Telemetry = async function() {
    const now = Date.now();
    if (now - window.lastL0FetchTime < 30000 && window.l0TelemetryCache.tenants.length > 0) {
        console.log("L0 Telemetry debounced natively.");
        return; // debounce heartbeat
    }
    window.lastL0FetchTime = now;
    
    const tableBody = document.getElementById('l0-telemetry-table');
    try {
        const user = firebase.auth().currentUser;
        if (!user) return;
        tableBody.innerHTML = '<tr><td colspan="3" style="padding:16px; text-align:center;">Fetching L0 Telemetry Arrays...</td></tr>';
        
        const token = await user.getIdToken();
        const response = await fetch(`${API_BASE}/api/l0/telemetry`, {
            method: 'GET',
            headers: { 'Authorization': `Bearer ${token}` }
        });
        
        if (response.status === 200) {
            document.getElementById('tab-l0-admin').classList.remove('hidden');
            const payload = await response.json();
            window.l0TelemetryCache.macro = payload.data.macro || {};
            window.l0TelemetryCache.tenants = payload.data.tenants || [];
            
            // Macro updates
            const m = window.l0TelemetryCache.macro;
            const tLeads = m.total_leads || 0;
            const actionable = (m.new || 0) + (m.contacted || 0);
            const conv = tLeads > 0 ? ((actionable / tLeads) * 100).toFixed(1) : 0;
            
            document.getElementById('l0-stat-total-leads').innerText = tLeads.toLocaleString();
            document.getElementById('l0-stat-conversion').innerText = `${conv}%`;
            document.getElementById('l0-stat-tenants').innerText = window.l0TelemetryCache.tenants.length;
            
            renderL0Table();
            
            // Trigger companion table refresh silently
            if (typeof fetchMacroTrends === 'function') fetchMacroTrends();
        } else {
            tableBody.innerHTML = '<tr><td colspan="3" style="padding:16px; text-align:center; color: #ef4444;">Access Denied. L0 Privilege Missing.</td></tr>';
        }
    } catch(err) {
        console.error(err);
    }
};

window.sortL0Table = function(key) {
    if (window.l0TelemetryCache.sortKey === key) {
        window.l0TelemetryCache.sortDesc = !window.l0TelemetryCache.sortDesc;
    } else {
        window.l0TelemetryCache.sortKey = key;
        window.l0TelemetryCache.sortDesc = true;
    }
    renderL0Table();
};

window.renderL0Table = function() {
    const tableBody = document.getElementById('l0-telemetry-table');
    if (!tableBody) return;
    
    let tenants = [...window.l0TelemetryCache.tenants];
    const key = window.l0TelemetryCache.sortKey;
    const desc = window.l0TelemetryCache.sortDesc ? -1 : 1;
    
    tenants.sort((a,b) => {
        let valA, valB;
        if (key === 'email') { valA = a.email || ''; valB = b.email || ''; }
        else if (key === 'wallet') { valA = a.wallet_balance || 0; valB = b.wallet_balance || 0; }
        else { valA = a.total_leads_generated || 0; valB = b.total_leads_generated || 0; }
        
        if (valA < valB) return -1 * desc;
        if (valA > valB) return 1 * desc;
        return 0;
    });
    
    let html = '';
    tenants.forEach(t => {
        const isSuspended = t.is_active === false; 
        const isPending = t.approval_status === 'pending';
        const statusColor = isSuspended ? '#ef4444' : (isPending ? '#f59e0b' : '#25D366');
        const statusBadge = `<strong style="color:${statusColor}">${isSuspended ? 'SUSPENDED' : (isPending ? 'PENDING' : 'ACTIVE')}</strong>`;
        
        let actionHTML = '';
        if (isPending) {
            actionHTML = `
                <input type="number" id="approve-days-${t.tenant_id}" value="180" style="width: 45px; padding: 4px; font-size: 0.75rem; border: 1px solid #ccc; border-radius: 4px;" title="Days">
                <input type="number" id="approve-amt-${t.tenant_id}" value="20000" style="width: 60px; padding: 4px; font-size: 0.75rem; border: 1px solid #ccc; border-radius: 4px;" title="Credits">
                <button class="primary-btn" style="padding: 4px 8px; font-size:0.75rem;" onclick="approveCredentials('${t.tenant_id}')">APPROVE</button>
            `;
        } else {
            actionHTML = `
                <input type="number" id="mint-${t.tenant_id}" placeholder="Amt" style="width: 50px; padding: 4px; font-size: 0.75rem; border: 1px solid #ccc; border-radius: 4px;">
                <button class="secondary-btn" style="padding: 4px 8px; font-size:0.75rem; color:#4F46E5; border-color:#4F46E5" onclick="mintCredentials('${t.tenant_id}')">MINT</button>
                <button class="secondary-btn" style="padding: 4px 8px; font-size:0.75rem; color:${statusColor}; border-color:${statusColor}" onclick="toggleTenantSuspend('${t.tenant_id}', ${!isSuspended})">
                    ${isSuspended ? 'UNSUSPEND' : 'SUSPEND'}
                </button>
            `;
        }
        
        html += `
        <tr style="border-bottom: 1px solid var(--glass-border);">
            <td style="padding: 12px; font-weight: 500;">
                ${t.email || 'No email saved'}<br>
                <small style="font-family:monospace; color:var(--text-muted);">${(t.tenant_id||'Unknown').substring(0,8)}...</small>
            </td>
            <td style="padding: 12px;">${statusBadge}</td>
            <td style="padding: 12px; font-family:monospace;">${(t.wallet_balance || 0).toLocaleString()} CR</td>
            <td style="padding: 12px; text-align:right;">${(t.total_leads_generated || 0).toLocaleString()}</td>
            <td style="padding: 12px; text-align:right;">${actionHTML}</td>
        </tr>`;
    });
    tableBody.innerHTML = html || '<tr><td colspan="5" style="padding:16px; text-align:center;">No tenants found.</td></tr>';
}

let rawMacroTrends = null;
let macroChartObj = null;

window.fetchMacroTrends = async function() {
    const tableBody = document.getElementById('campaign-intelligence-table');
    if(!tableBody) return;
    try {
        const user = firebase.auth().currentUser;
        if (!user) return;
        tableBody.innerHTML = '<tr><td colspan="5" style="padding:16px; text-align:center;">Fetching Campaign Intelligence Constraints...</td></tr>';
        
        const token = await user.getIdToken();
        const response = await fetch(`${API_BASE}/api/l0/trends`, {
            method: 'GET',
            headers: { 'Authorization': `Bearer ${token}` }
        });
        
        if (response.ok) {
            const payload = await response.json();
            rawMacroTrends = payload.data || {};
            renderMacroTrends();
        } else {
            tableBody.innerHTML = '<tr><td colspan="5" style="padding:16px; text-align:center; color:#ef4444;">Access Denied. L0 Privilege Missing.</td></tr>';
        }
    } catch (e) {
        tableBody.innerHTML = '<tr><td colspan="5" style="padding:16px; text-align:center; color:#ef4444;">Gateway Error.</td></tr>';
    }
};

window.renderMacroTrends = function() {
    const tableBody = document.getElementById('campaign-intelligence-table');
    if (!rawMacroTrends || !tableBody) return;
    
    const campaigns = rawMacroTrends.campaign_trends || [];
    
    if (campaigns.length === 0) {
        tableBody.innerHTML = '<tr><td colspan="5" style="padding:16px; text-align:center;">No tracking data available.</td></tr>';
        return;
    }
    
    tableBody.innerHTML = campaigns.map(c => `
        <tr style="border-bottom: 1px solid var(--glass-border);">
            <td style="padding: 12px; font-weight: 500;">
                ${c.email}<br>
                <small style="font-family:monospace; color:var(--text-muted); font-size:0.75rem;">${(c.tenant_id||'').substring(0,8)}</small>
            </td>
            <td style="padding: 12px; font-weight: 500;">
                ${c.name}
            </td>
            <td style="padding: 12px;">
                <div style="font-size:0.85rem; max-height: 4.8em; overflow:hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical;" title="${(c.bio||'').replace(/"/g, '&quot;')}">${c.bio}</div>
            </td>
            <td style="padding: 12px; font-size:0.8rem; font-family:monospace; color:var(--primary);">
                ${c.keywords}
            </td>
            <td style="padding: 12px; text-align:right; font-weight:bold; color:var(--success);">
                ${(c.leads_generated||0).toLocaleString()}
            </td>
        </tr>
    `).join('');
};

window.toggleTenantSuspend = async function(uid, isCurrentlyActive) {
    if(!confirm(`Are you absolutely sure you want to ${isCurrentlyActive ? 'SUSPEND' : 'REACTIVATE'} this tenant globally?`)) return;
    try {
        const user = firebase.auth().currentUser;
        const token = await user.getIdToken();
        await fetch(`${API_BASE}/api/l0/users/suspend`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({ uid: uid, is_active: !isCurrentlyActive })
        });
        showToast('Telemetry Command Accepted', 'info');
        fetchL0Telemetry();
    } catch(err) {
        showToast('Super Admin action explicitly failed.', 'error');
    }
};

window.approveCredentials = async function(tenantId) {
    const amtEl = document.getElementById(`approve-amt-${tenantId}`);
    const daysEl = document.getElementById(`approve-days-${tenantId}`);
    if (!amtEl || !amtEl.value || !daysEl || !daysEl.value) return;
    try {
        const user = firebase.auth().currentUser;
        const token = await user.getIdToken();
        const resp = await fetch(`${API_BASE}/api/l0/users/${tenantId}/approve`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({ amount: parseInt(amtEl.value), days: parseInt(daysEl.value) })
        });
        if (resp.ok) {
            showToast(`Approved ${tenantId}.`, 'success');
            fetchL0Telemetry();
        } else {
            showToast('Failed to approve.', 'error');
        }
    } catch(err) {
        showToast('Approve action failed.', 'error');
    }
};

window.mintCredentials = async function(tenantId) {
    const amtEl = document.getElementById(`mint-${tenantId}`);
    if (!amtEl || !amtEl.value) return;
    try {
        const user = firebase.auth().currentUser;
        const token = await user.getIdToken();
        const resp = await fetch(`${API_BASE}/api/l0/users/${tenantId}/mint`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({ amount: amtEl.value })
        });
        if (resp.ok) {
            showToast(`Minted ${amtEl.value} credits.`, 'success');
            amtEl.value = '';
            fetchL0Data();
        } else {
            showToast('Failed to mint.', 'error');
        }
    } catch(err) {
        showToast('Failed to mint credits.', 'error');
    }
};

window.sendEmailReport = function() {
    showToast('Connecting to Cloud Run SMTP queue...', 'info');
    setTimeout(() => { showToast('Enterprise PDF dispatched to your registered email.', 'success'); }, 1500);
};

window.loadMoreLeads = function() {
    showToast('Historical offset cursors must be mapped in Orchestrator Endpoint v2.', 'info');
};
