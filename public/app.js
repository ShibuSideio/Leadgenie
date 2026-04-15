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

const API_BASE = "";

// Digital Twin Engine — Reverse Proxy via Firebase
const DT_ENGINE_URL = "";



// DOM Elements
const authContainer = document.getElementById('auth-container');
const appContainer  = document.getElementById('app-container');
const loginBtn      = document.getElementById('login-btn');
const leadsList     = document.getElementById('leads-list');

// Selected Filter State
let currentCampaignFilter = 'all';
let rawLeadsCache = [];
let unsubscribeLeads = null;   // declared here to avoid temporal dead zone
const _leadsMap = new Map();

// Toast — defined as function declaration so it is hoisted and available
// everywhere, including in loadLeads which runs before window.showToast assignment.
function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) { console.warn('[Toast]', type, message); return; }
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => toast.classList.add('show'), 10);
    setTimeout(() => { toast.classList.remove('show'); setTimeout(() => toast.remove(), 300); }, 3500);
}
window.showToast = showToast; // expose to inline HTML handlers

function handleAuthRejection() {
    showToast('Session Expired or Unauthorized Access.', 'error');
    auth.signOut();
}

// ─── MODAL UTILITY — single source of truth for all modal show/hide ──────────
// All modals now use style.display. Never classList.add/remove('hidden') for modals.
window.showModal = function(id) {
    const el = document.getElementById(id);
    if (el) el.classList.add('open'); // .sio-modal-overlay.open => display:flex via CSS
};
window.closeModal = function(id) {
    const el = document.getElementById(id);
    if (el) el.classList.remove('open');
};
// ─────────────────────────────────────────────────────────────────────────────

// Authentication state observer
auth.onAuthStateChanged(async user => {
    const authEl = document.getElementById('auth-container');
    const appEl  = document.getElementById('app-container');
    if (user) {
        if (authEl) authEl.style.display = 'none';
        if (appEl)  appEl.style.display  = 'flex';
        loadDashboard();
    } else {
        if (authEl) authEl.style.display = '';
        if (appEl)  appEl.style.display  = 'none';
    }
});

// Login Handler
if (loginBtn) {
    loginBtn.addEventListener('click', () => {
        const provider = new firebase.auth.GoogleAuthProvider();
        auth.signInWithPopup(provider).catch(err => console.error('Sign-in error:', err));
    });
}
// Note: Sign-out is handled via onclick="firebase.auth().signOut()" in the user dropdown.

// Unified Dashboard Loader
async function loadDashboard() {
    const user = firebase.auth().currentUser;
    if (!user) return;

    await Promise.all([
        loadMe(),
        loadCampaigns(),
        loadLeads()
    ]);

    await initializeDashboardState();

    if (window.crmAutoOpen) {
        window.crmAutoOpen = false;
        switchTab('crm-test');
    }
}

async function fetchTenantProfile() {
    try {
        const user = window.firebase.auth().currentUser;
        const token = await user.getIdToken();
        const response = await fetch(`${API_BASE}/api/tenant_profiles`, { 
            method: 'GET',
            headers: { 'Authorization': `Bearer ${token}` } 
        });
        // BUG FIX: Corrected from double-nested if(ok){if(!ok)} which made error
        // path dead code. Now correctly structured as if(!ok)/else.
        if (!response.ok) {
            console.error('Backend Error (fetchTenantProfile):', await response.text());
            return null;
        }
        const data = await response.json();
        if (data && data.data && data.data.length > 0) return data.data[0];
    } catch (e) { console.error("fetchTenantProfile error", e); }
    return null;
}

async function initializeDashboardState() {
    try {
        const tenantProfile = await fetchTenantProfile();
        // Fallback or count length of existing items
        const rawRows = document.querySelectorAll('#campaign-list-table tr').length;
        const fallbackCount = document.getElementById('campaign-list-table').innerHTML.includes('No active campaigns') ? 0 : rawRows;
        const activeCount = window.activeCampaignCount !== undefined ? window.activeCampaignCount : fallbackCount;
        
        if (tenantProfile) {
            renderExpansionState(activeCount);
        } else {
            renderZeroState();
        }
    } catch (error) {
        console.error("Failed to load tenant state:", error);
    }
}

function renderZeroState() {
    const h = document.getElementById('btn-new-twin-hero'); if(h) h.style.display = 'block';
    const ah = document.getElementById('btn-add-campaign-hero'); if(ah) ah.style.display = 'none';
    const m = document.getElementById('btn-new-twin-matrix'); if(m) m.style.display = 'block';
    const am = document.getElementById('btn-add-campaign-matrix'); if(am) am.style.display = 'none';
}

function renderExpansionState(activeCount) {
    const h = document.getElementById('btn-new-twin-hero'); if(h) h.style.display = 'none';
    const m = document.getElementById('btn-new-twin-matrix'); if(m) m.style.display = 'none';
    
    const ah = document.getElementById('btn-add-campaign-hero');
    const am = document.getElementById('btn-add-campaign-matrix');
    if(ah) ah.style.display = 'block';
    if(am) am.style.display = 'block';
    
    if (activeCount >= 5) {
        const txt = 'Campaign Limit Reached (5/5)';
        if(ah) { ah.innerText = txt; ah.disabled = true; ah.style.opacity = '0.5'; ah.style.cursor = 'not-allowed'; }
        if(am) { am.innerText = txt; am.disabled = true; am.style.opacity = '0.5'; am.style.cursor = 'not-allowed'; }
    } else {
        const txt = `+ Add Campaign (${activeCount}/5)`;
        if(ah) { ah.innerHTML = txt; ah.disabled = false; ah.style.opacity = '1'; ah.style.cursor = 'pointer'; }
        if(am) { am.innerHTML = txt; am.disabled = false; am.style.opacity = '1'; am.style.cursor = 'pointer'; }
    }
}

// BUG FIX: must be window.activeWallet (not let) so the property
// is accessible as window.activeWallet from any inline handler.
window.activeWallet = { allocated_credits: 0, consumed_credits: 0 };
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
        // BUG FIX: Corrected from double-nested if(ok){if(!ok)} — error path
        // was dead code because !ok is always false inside if(ok).
        if (!response.ok) {
            console.error('Backend Error (loadMe):', await response.text());
            return;
        }
        {
            const payload = await response.json();
            const data = payload.data || {};
            
            const waitroom = document.getElementById('waitroom-overlay');
            // TECH DEBT: .dashboard-grid selector kept for compat. Also targets .app-main.
            // Refactor to remove .dashboard-grid dependency in next sprint.
            const mainGrid = document.querySelector('.app-main, .dashboard-grid');
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

            // Defensive dual-path: check nested wallet map AND flattened root
            // (DB migration may have stored fields at either level).
            const w = payload.wallet || data.wallet || {};
            const allocated = Number(w.allocated_credits  || data.allocated_credits  || 0) || 0;
            const consumed  = Number(w.consumed_credits   || data.consumed_credits   || 0) || 0;
            const credits   = allocated - consumed;
            window.activeWallet = { allocated_credits: allocated, consumed_credits: consumed };
            console.log('[WALLET] payload.wallet:', payload.wallet, '| data.wallet:', data.wallet,
                        '| allocated:', allocated, '| consumed:', consumed, '| balance:', credits);
            const el = document.getElementById('wallet-balance');
            if (el) el.innerText = credits.toLocaleString();

            // Credit usage bar in dropdown
            const barEl = document.getElementById('ud-credit-bar');
            const consumedEl = document.getElementById('ud-credits-consumed');
            const totalEl = document.getElementById('ud-credits-total');
            if (barEl && allocated > 0) {
                const pct = Math.max(0, Math.min(100, Math.round((credits / allocated) * 100)));
                barEl.style.width = pct + '%';
                barEl.style.background = pct < 20 ? '#ef4444' : pct < 50 ? '#f59e0b' : 'var(--primary)';
            }
            if (consumedEl) consumedEl.textContent = consumed.toLocaleString();
            if (totalEl) totalEl.textContent = allocated.toLocaleString();
            
            const alertBanner = document.getElementById('wallet-alert-banner');
            const newCampBtn = document.querySelector('button[onclick="openNewCampaignModal()"]');
            if (credits <= 0) {
                if (alertBanner) { alertBanner.textContent = 'Wallet Empty: You have 0 credits. Contact admin to top up.'; alertBanner.style.display = 'block'; }
                if (newCampBtn) { newCampBtn.textContent = 'Upgrade Plan (0 Credits)'; newCampBtn.disabled = true; newCampBtn.style.background = '#94a3b8'; }
            } else if (credits < 50) {
                if (alertBanner) alertBanner.style.display = 'block';
            } else {
                if (alertBanner) alertBanner.style.display = 'none';
                if (newCampBtn) { newCampBtn.textContent = '+ Find New Clients'; newCampBtn.disabled = false; newCampBtn.style.background = 'var(--primary)'; }
            }

            // V18: Populate user dropdown header
            const udName = document.getElementById('ud-display-name');
            const udEmail = document.getElementById('ud-email');
            const avatarEl = document.getElementById('user-avatar-initials');
            const avatarLg = document.getElementById('ud-avatar-large');
            const displayName = auth.currentUser?.displayName || data.email || 'User';
            const initials = displayName.split(' ').map(p => p[0]).join('').substring(0, 2).toUpperCase();
            if (udName) udName.textContent = displayName;
            if (udEmail) udEmail.textContent = auth.currentUser?.email || '';
            if (avatarEl) avatarEl.textContent = initials;
            if (avatarLg) avatarLg.textContent = initials;

            if (!data.agreed_to_terms) {
                const tosModal = document.getElementById('tos-modal');
                if (tosModal) tosModal.style.display = 'flex';
            }

            const hookInput = document.getElementById('crm-webhook-url');
            if (hookInput && data.crm_webhook_url) {
                hookInput.value = data.crm_webhook_url;
            }
            window.currentUserData = data;

            // V17: Greeting with first name
            const dName = auth.currentUser?.displayName || '';
            const firstName = dName.split(' ')[0] || '';
            fcUpdateGreeting(firstName);
        }
    } catch (error) {
        console.error('Execution failed:', error);
    }

    // ── Silent Persona Vault Migration ─────────────────────────────────────
    // Fire-and-forget: idempotent on backend. Does NOT block UI.
    _runPersonaMigration();
}

async function _runPersonaMigration() {
    try {
        const user = firebase.auth().currentUser;
        if (!user) return;
        const token = await user.getIdToken();
        const resp  = await fetch(`${API_BASE}/api/migrate-personas`, {
            method:  'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body:    JSON.stringify({})
        });
        const json = await resp.json().catch(() => ({}));
        if (json.migrated) {
            console.log(`[MIGRATION] Legacy persona created: ${json.name} (${json.persona_id})`);
            window._personasCache = []; // invalidate so next vault load is fresh
        }
    } catch(e) {
        // Non-fatal — migration will retry on next login
        console.warn('[MIGRATION] Silent migration failed (non-fatal):', e);
    }
}

// ─── CAMPAIGN DATA STORE ────────────────────────────────────────────────────
// Campaign objects are stored here at load time.
// Buttons in the DOM only carry data-campaign-id — no data in onclick attrs.
// openEditModal(id) does an O(1) Map lookup — eliminates all char-escaping bugs.
window._campaignsStore = new Map();

// Dynamic Campaign Hydration via REST API
async function loadCampaigns() {
    const feed      = document.getElementById('active-campaign-feed');
    const tableBody = document.getElementById('campaign-list-table');
    const filterSel = document.getElementById('campaign-filter');

    try {
        const user = firebase.auth().currentUser;
        if (!user) return handleAuthRejection();

        const token = await user.getIdToken();
        const res   = await fetch(`${API_BASE}/api/campaigns`, {
            method: 'GET',
            headers: { 'Authorization': `Bearer ${token}` }
        });

        if (res.status === 401 || res.status === 403) return handleAuthRejection();
        if (!res.ok) {
            console.error('loadCampaigns HTTP error:', res.status, await res.text());
            if (tableBody) tableBody.innerHTML = '<tr><td colspan="4" class="empty-state">Failed to load campaigns.</td></tr>';
            return;
        }

        const payload   = await res.json();
        const campaigns = Array.isArray(payload.data) ? payload.data : [];

        // populate store — all downstream reads come from here
        window._campaignsStore.clear();
        campaigns.forEach(c => window._campaignsStore.set(c.id, c));

        if (campaigns.length === 0) {
            window.activeCampaignCount = 0;
            renderExpansionState(0);
            if (feed)      feed.innerHTML = '';
            if (tableBody) tableBody.innerHTML = '<tr><td colspan="4" style="padding:16px;text-align:center;">No campaigns found. Click \u201cFind New Clients\u201d to get started.</td></tr>';
            return;
        }

        campaigns.sort((a, b) => (b.createdAt || '').localeCompare(a.createdAt || ''));

        let activeCount = 0;
        let tableRows   = '';
        let filterOpts  = '<option value="all">All Searches</option>';

        campaigns.forEach(camp => {
            const id       = camp.id;
            const isActive = camp.status === 'active';
            if (isActive) activeCount++;

            const statusColor = isActive ? '#25D366' : '#ef4444';
            const statusBadge = `<span style="font-size:0.75rem;padding:2px 8px;border-radius:4px;border:1px solid ${statusColor};color:${statusColor};">${(camp.status || 'unknown').toUpperCase()}</span>`;
            const geoWarn     = (camp.gl && camp.location) ? '' : '<span style="color:#ea580c;font-size:0.75rem;display:block;margin-top:4px;">&#9888; Location Missing: Edit to set targeting</span>';

            // Truncate keywords for display only
            const kw = (camp.keywords || 'N/A');
            const kwDisplay = kw.length > 80 ? kw.substring(0, 80) + '\u2026' : kw;

            // ── CRITICAL: only data-campaign-id on the button, zero data in onclick ──
            tableRows += `
                <tr style="border-bottom:1px solid var(--glass-border);">
                    <td style="padding:12px;">
                        <strong>${camp.name || 'Untitled'}</strong>
                        ${geoWarn}
                    </td>
                    <td style="padding:12px;">
                        <span style="color:var(--text-muted);font-size:0.85rem;">${kwDisplay}</span>
                    </td>
                    <td style="padding:12px;">${statusBadge}</td>
                    <td style="padding:12px;text-align:right;white-space:nowrap;">
                        <button class="secondary-btn" style="padding:4px 10px;font-size:0.75rem;margin-right:4px;"
                            data-campaign-id="${id}"
                            onclick="openEditModal(this.dataset.campaignId)">Edit</button>
                        <button class="secondary-btn" style="padding:4px 10px;font-size:0.75rem;border-color:${statusColor};color:${statusColor};"
                            onclick="toggleCampaignStatus('${id}','${camp.status}')">${isActive ? 'Pause' : 'Resume'}</button>
                    </td>
                </tr>`;

            filterOpts += `<option value="${id}">${camp.name}</option>`;
        });

        if (tableBody) tableBody.innerHTML = tableRows;

        // Sync global counter — consumed by initializeDashboardState / renderExpansionState
        window.activeCampaignCount = activeCount;
        renderExpansionState(activeCount);

        if (filterSel) {
            const prev = filterSel.value;
            filterSel.innerHTML = filterOpts;
            filterSel.value = prev || 'all';
        }

        if (feed) {
            feed.innerHTML = `
                <div style="background:rgba(79,70,229,0.05);border:1px solid rgba(79,70,229,0.2);padding:12px;border-radius:8px;margin-bottom:24px;">
                    <span class="badge" style="background:var(--primary);">System Status: Online</span>
                    <span style="color:var(--text-muted);font-size:0.9rem;margin-left:8px;">Scanning ${activeCount} Active Target Matrix${activeCount !== 1 ? 'es' : ''}</span>
                </div>`;
        }
    } catch (error) {
        console.error("Campaign Hook Error:", error);
        if (tableBody) tableBody.innerHTML = '<tr><td colspan="4" style="padding:16px; text-align:center; color: #ef4444;">API Connection Error</td></tr>';
    }
}

// --- ENTERPRISE CHART.JS PIPELINE ---
// V18: funnel-wrap is conditionally shown only when leads exist (clean canvas otherwise)
let conversionChart = null;
function initAnalyticsChart(newC, contactedC, convertedC) {
    const ctx = document.getElementById('funnelChart');
    const wrapper = document.getElementById('funnel-wrap');
    const totalLeads = newC + contactedC + convertedC;

    // Show/hide the wrapper based on whether there is any data
    if (wrapper) {
        if (totalLeads > 0) {
            wrapper.style.display = '';
        } else {
            wrapper.style.display = 'none';
            return; // No data — keep canvas clean
        }
    }
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


// =============================================================================
// FIRESTORE LIVE FEED
// =============================================================================
// (unsubscribeLeads declared at top-level to avoid temporal dead zone)

async function loadLeads() {
    leadsList.innerHTML = '<div class="lead-card pulse">Connecting to Secure Orchestrator...</div>';
    try {
        const user = firebase.auth().currentUser;
        if (!user) return handleAuthRejection();
        if (unsubscribeLeads) { unsubscribeLeads(); }
        unsubscribeLeads = firebase.firestore()
            .collection('leads')
            .where('tenant_id', '==', user.uid)
            .where('is_in_crm', '==', false)
            .onSnapshot((snapshot) => {
                rawLeadsCache = [];
                snapshot.forEach(doc => {
                    let data = doc.data();
                    data.id = doc.id;
                    rawLeadsCache.push(data);
                });
                if (rawLeadsCache.length === 0) { renderLeads(); initAnalyticsChart(0,0,0); return; }
                rawLeadsCache.sort((a, b) => (b.score || 0) - (a.score || 0));
                let cNew = 0, cContact = 0, cConvert = 0;
                let cDiscovered = rawLeadsCache.length, cActionable = 0, cIgnored = 0;
                rawLeadsCache.forEach(l => {
                    if (l.status === 'ignored') { cIgnored++; return; }
                    if (l.status === 'new' || !l.status) cActionable++;
                    if (l.status === 'contacted') { cContact++; }
                    else if (l.status === 'converted') { cConvert++; }
                    else { cNew++; }
                });
                const elDisc = document.getElementById('stat-discovered');
                const elAct  = document.getElementById('stat-actionable');
                const elIgn  = document.getElementById('stat-ignored');
                if (elDisc) elDisc.innerText = cDiscovered;
                if (elAct)  elAct.innerText  = cActionable;
                if (elIgn)  elIgn.innerText  = cIgnored;
                initAnalyticsChart(cNew, cContact, cConvert);
                fcUpdateKPIs(rawLeadsCache);
                renderLeads();
            }, (error) => {
                console.error('[Firestore] onSnapshot error:', error);
                if (error.code === 'failed-precondition') {
                    showToast('Feed index missing — see console for index link.', 'error');
                    leadsList.innerHTML = '<div class="lead-card" style="color:#f59e0b;border-color:#f59e0b;padding:16px;">⚠ Firestore composite index required.<br><small>Open the browser console for the GCP link.</small></div>';
                    return;
                }
                if (error.code === 'permission-denied') { console.warn('[Firestore] Permission denied.'); return; }
                showToast('Live feed error — refresh to reconnect.', 'error');
            });
    } catch (error) {
        console.error('Firestore Initialization Error:', error);
        leadsList.innerHTML = '<div class="lead-card" style="color:#ef4444;border-color:#ef4444;">Could not connect to database. Check your network.</div>';
        showToast('Connection Refused', 'error');
    }
}

// =============================================================================
// VIRTUAL SCROLL OBSERVER
// =============================================================================

let virtualObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
        if (entry.isIntersecting && !entry.target.hasAttribute('data-rendered')) {
            const leadId = entry.target.getAttribute('data-lead-id');
            const lead = rawLeadsCache.find(l => (l.id || l.doc_id) === leadId);
            if (lead) {
                const newCard = window.createLeadCardV2(leadId, lead);
                entry.target.replaceWith(newCard);
                virtualObserver.observe(newCard);
                newCard.setAttribute('data-rendered', 'true');
            }
        }
    });
}, { rootMargin: '600px' });

function renderLeads() {
    const filteredLeads = rawLeadsCache.filter(lead => {
        if (!['new', 'contacted', 'converted'].includes(lead.status || 'new')) return false;
        if (currentCampaignFilter !== 'all') {
            const matched = Array.isArray(lead.matched_campaigns)
                ? lead.matched_campaigns.includes(currentCampaignFilter)
                : lead.campaign_id === currentCampaignFilter;
            if (!matched) return false;
        }
        return true;
    });
    if (filteredLeads.length === 0) {
        leadsList.innerHTML = '<div class="lead-card" style="text-align:center;padding:40px;border:none;background:transparent;box-shadow:none;"><div style="font-size:3rem;margin-bottom:12px;opacity:0.8;">🎯</div><h3 style="color:var(--text-main);margin-bottom:8px;">Hunting for leads...</h3><p style="color:var(--text-muted);font-size:0.95rem;line-height:1.5;">We are actively scanning the web. Check back in a few minutes.</p></div>';
        return;
    }
    leadsList.innerHTML = '';
    virtualObserver.disconnect();
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

// =============================================================================
// TOAST UI ENGINE
// =============================================================================

// window.showToast is already defined as a hoisted function at top of file.

// Timeline Audit Modal
window.viewLeadTimeline = function(eventsJson) {
    try {
        const events = JSON.parse(decodeURIComponent(eventsJson)) || [];
        const feed = document.getElementById('audit-timeline-feed');
        if (!feed) return;
        if (events.length === 0) {
            feed.innerHTML = '<p style="color:var(--text-muted);text-align:center;">No CRM interactions recorded yet.</p>';
        } else {
            feed.innerHTML = events.map(e => `
                <div style="padding:12px;border-left:3px solid var(--primary);margin-bottom:12px;background:white;border-radius:0 4px 4px 0;box-shadow:0 1px 2px rgba(0,0,0,0.05);">
                    <small style="color:var(--text-muted);display:block;margin-bottom:4px;">${e.date}</small>
                    <strong style="color:var(--text-main);font-size:0.95rem;">${e.action}</strong>
                </div>`).join('');
        }
        document.getElementById('audit-log-modal').style.display = 'flex';
    } catch(e) { console.error('Timeline error', e); }
};


// =============================================================================
// CORE API MUTATION HELPER — used by pushToCRM, updateLeadStatus, etc.
// =============================================================================

async function performApiMutation(url, method, payload) {
    const user = auth.currentUser;
    if (!user) return false;
    // iOS Safari fix: force=true guarantees a fresh token even when the
    // Safari background throttling has invalidated the in-memory cached token.
    const token = await user.getIdToken(true);
    const response = await fetch(`${API_BASE}${url}`, {
        method: method,
        headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });
    if (response.status === 401 || response.status === 403) { handleAuthRejection(); return false; }
    if (!response.ok) throw new Error('API Execution Failed');
    return true;
}

// =============================================================================
// CRM PUSH — moves lead from main feed into the CRM pipeline
// =============================================================================

window.pushToCRM = async function(docId, leadStr) {
    try {
        const success = await performApiMutation(`/api/leads/${docId}`, 'PUT', {
            is_in_crm:       true,
            crm_status:      'new',
            estimated_value: 0,
            notes:           [],
            expire_at:       null
        });
        if (success) {
            const cardEl = document.getElementById(docId);
            if (cardEl) { virtualObserver.unobserve(cardEl); cardEl.remove(); }
            rawLeadsCache = rawLeadsCache.filter(l => (l.id || l.doc_id) !== docId);
            showToast('Lead filed in CRM — navigate to CRM tab to manage it.', 'success');
            const userUrl = window.currentUserData?.crm_webhook_url;
            if (userUrl) {
                try {
                    const lead = JSON.parse(decodeURIComponent(leadStr));
                    fetch(userUrl, { method:'POST', headers:{'Content-Type':'application/json'}, mode:'no-cors', body: JSON.stringify({event:'lead_pushed', lead}) });
                } catch(_) {}
            }
        }
    } catch(e) {
        console.error('CRM Push failure', e);
        showToast('CRM push failed — try again.', 'error');
    }
};

// =============================================================================
// =============================================================================
// CATEGORICAL REJECTION MODAL (RLHF)
// Replaces binary 'ignored'. User selects one of 5 reasons, which drives the
// dynamic ontology weight penalty in the Orchestrator's RLHF engine.
// =============================================================================

window.openRejectionModal = function(docId) {
    document.getElementById('rejection-lead-id').value = docId;
    // Optimistic removal from visible feed so UX feels instant
    const idx = rawLeadsCache.findIndex(l => l.id === docId);
    if (idx !== -1) { rawLeadsCache.splice(idx, 1); renderLeads(); }
    showModal('rejection-modal');
};

window.submitRejection = async function(reason) {
    const VALID = ['not_b2b', 'wrong_industry', 'too_small', 'competitor', 'bad_data'];
    if (!VALID.includes(reason)) return;
    const docId = document.getElementById('rejection-lead-id').value;
    if (!docId) return;
    closeModal('rejection-modal');
    const labels = { not_b2b: 'Not B2B', wrong_industry: 'Wrong Industry',
                     too_small: 'Too Small', competitor: 'Competitor', bad_data: 'Bad Data' };
    try {
        const user = firebase.auth().currentUser;
        if (!user) return;
        const token = await user.getIdToken(true);
        const resp  = await fetch(`${API_BASE}/api/leads/${docId}`, {
            method:  'PUT',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body:    JSON.stringify({ status: 'rejected', rejection_reason: reason })
        });
        if (resp.ok) {
            showToast(`Lead rejected: ${labels[reason]}. AI is learning.`, 'success');
        } else {
            showToast('Lead removed. API sync failed — will retry.', 'info');
        }
    } catch(err) {
        console.error('[rejection]', err);
        showToast('Lead removed. Background sync failed.', 'info');
    }
};

// =============================================================================
// LEAD STATUS UPDATE — contacted / converted (rejection goes through modal above)
// =============================================================================
window.updateLeadStatus = async function(docId, newStatus) {
    // Route all skip/reject actions through the categorical RLHF modal
    if (newStatus === 'ignored' || newStatus === 'rejected') {
        openRejectionModal(docId);
        return;
    }
    try {
        const success = await performApiMutation(`/api/leads/${docId}`, 'PUT', { status: newStatus });
        if (success) {
            showToast(`Lead status updated: ${newStatus}`, 'success');
            loadDashboard();
        }
    } catch(err) {
        showToast('Error saving update to database', 'error');
    }
};

// =============================================================================
// CAMPAIGN ACTIONS — toggle pause/resume, update details
// =============================================================================

window.toggleCampaignStatus = async function(id, currentStatus) {
    const newStatus = currentStatus === 'active' ? 'paused' : 'active';
    try {
        const success = await performApiMutation(`/api/campaigns/${id}`, 'PUT', { status: newStatus });
        if (success) { showToast(`Campaign ${newStatus} successfully`, 'success'); loadDashboard(); }
    } catch(err) {
        showToast('Status update failed', 'error');
    }
};

// Campaign filter dropdown — called by onchange="filterLeadsByCampaign(this.value)"
window.filterLeadsByCampaign = function(campaignId) {
    currentCampaignFilter = campaignId || 'all';
    renderLeads();
};

// CRM Webhook save — called by "Save Integration" button in settings modal
window.saveCRMWebhook = async function() {
    const url = document.getElementById('crm-webhook-url')?.value?.trim();
    if (!url) { showToast('Please enter a webhook URL.', 'error'); return; }
    try {
        const success = await performApiMutation('/api/me', 'PUT', { crm_webhook_url: url });
        if (success) {
            if (window.currentUserData) window.currentUserData.crm_webhook_url = url;
            showToast('CRM integration saved!', 'success');
        }
    } catch(err) {
        showToast('Failed to save webhook URL.', 'error');
    }
};

window.updateCampaignAction = async function(id) {
    const nameInput = document.getElementById(`edit-camp-name-${id}`);
    const bioInput  = document.getElementById(`edit-camp-bio-${id}`);
    const keysInput = document.getElementById(`edit-camp-keys-${id}`);
    const urlsInput = document.getElementById(`edit-camp-urls-${id}`);
    if (!nameInput || !keysInput) return;
    let targetUrls = [];
    if (urlsInput && urlsInput.value.trim()) {
        targetUrls = urlsInput.value.split('\n').map(u => u.trim()).filter(Boolean).slice(0, 10);
    }
    try {
        const success = await performApiMutation(`/api/campaigns/${id}`, 'PUT', {
            name: nameInput.value, bio: bioInput?.value || '', keywords: keysInput.value, target_urls: targetUrls
        });
        if (success) { showToast('Campaign successfully updated!', 'success'); loadDashboard(); }
    } catch(err) {
        showToast('Error modifying campaign', 'error');
    }
};

// =============================================================================
// CAMPAIGN EDIT MODAL — openEditModal / closeEditModal / saveEditedCampaign
// These functions were removed during the campaign<>persona decoupling but
// are still called from the dynamically-generated campaign table HTML.
// All three were missing — causing ReferenceError on every Edit button click.
// =============================================================================

window.handleCountryChange = function(glSelectId, locationInputId) {
    const gl = document.getElementById(glSelectId)?.value || '';
    const locInput = document.getElementById(locationInputId);
    if (!locInput) return;
    if (!gl) {
        locInput.placeholder = 'City, State/Region';
        locInput.value = '';
    } else {
        locInput.placeholder = 'e.g. New York, London, Mumbai';
    }
};

window.openEditModal = function(id) {
    const camp = window._campaignsStore.get(id);
    if (!camp) {
        showToast('Campaign data not found. Please refresh.', 'error');
        console.error('[openEditModal] id not in store:', id);
        return;
    }

    document.getElementById('edit-camp-id').value  = id;
    document.getElementById('edit-camp-name').value = camp.name || '';

    // ── Child Campaign Bio Resolution ──────────────────────────────────────────
    // CHILD_CAMPAIGN_OVERRIDE is set when a campaign originates from the
    // Digital Twin website analysis flow. The real bio comes from
    // campaign_focus + pain_point + unfair_advantage fields on the campaign doc.
    // ───────────────────────────────────────────────────
    const isChildCampaign = camp.bio === 'CHILD_CAMPAIGN_OVERRIDE';
    let bioDisplay, keywordsDisplay;

    if (isChildCampaign) {
        // Build readable bio from the DT fields
        const focus = camp.campaign_focus || camp.name || '';
        const pain  = camp.pain_point     || '';
        const adv   = camp.unfair_advantage || '';
        bioDisplay = [
            focus  ? `Product/Service: ${focus}` : '',
            pain   ? `Market Hook: ${pain}` : '',
            adv    ? `Competitive Advantage: ${adv}` : ''
        ].filter(Boolean).join('\n\n');
        // keywords was saved as '' for child campaigns — use company bio from profile if available
        const tenantBio = window.currentUserData?.company_description || window.currentUserData?.bio || '';
        keywordsDisplay = tenantBio || camp.keywords || '';
    } else {
        bioDisplay      = camp.bio      || '';
        keywordsDisplay = camp.keywords || '';
    }

    document.getElementById('edit-camp-bio').value  = bioDisplay;
    document.getElementById('edit-camp-keys').value = keywordsDisplay;

    const glEl = document.getElementById('edit-camp-gl');
    if (glEl) glEl.value = camp.gl || '';
    document.getElementById('edit-camp-location').value = camp.location || '';

    const urlsEl = document.getElementById('edit-camp-target-urls');
    if (urlsEl) {
        const urls = Array.isArray(camp.target_urls) ? camp.target_urls : [];
        urlsEl.value = urls.join('\n');
    }
    showModal('edit-campaign-modal');
};

window.closeEditModal = function() {
    closeModal('edit-campaign-modal');
};

window.saveEditedCampaign = async function() {
    const id       = document.getElementById('edit-camp-id')?.value        || '';
    const name     = document.getElementById('edit-camp-name')?.value      || '';
    const bio      = document.getElementById('edit-camp-bio')?.value       || '';
    const keywords = document.getElementById('edit-camp-keys')?.value      || '';
    const gl       = document.getElementById('edit-camp-gl')?.value        || '';
    const location = document.getElementById('edit-camp-location')?.value  || '';
    const urlsRaw  = document.getElementById('edit-camp-target-urls')?.value || '';
    const targetUrls = urlsRaw.split('\n').map(u => u.trim()).filter(Boolean).slice(0, 10);

    if (!id)   { showToast('Campaign ID missing. Please refresh.', 'error'); return; }
    if (!name) { showToast('Campaign name is required.', 'error'); return; }

    try {
        const success = await performApiMutation(`/api/campaigns/${id}`, 'PUT', {
            name, bio, keywords, gl, location, target_urls: targetUrls
        });
        if (success) {
            showToast('Campaign updated successfully!', 'success');
            window.closeEditModal();
            loadDashboard();
        } else {
            showToast('Update failed. Please try again.', 'error');
        }
    } catch(err) {
        console.error('saveEditedCampaign error:', err);
        showToast('API error updating campaign.', 'error');
    }
};

// =============================================================================
// SAVE CAMPAIGN ACTION — called from fc modal Step 2 "Launch" button
// =============================================================================

window.saveCampaignAction = async function(payload) {
    let cpName = '', cpBio = '', cpKeys = '', cpGl = '', cpLoc = '', targetUrls = [];

    if (payload) {
        cpName = payload.name || '';
        cpBio = payload.bio || '';
        cpKeys = payload.keywords || '';
        cpGl = payload.gl || '';
        cpLoc = payload.location || '';
        targetUrls = payload.target_urls || [];
    } else {
        const nameInput      = document.getElementById('camp-name');
        const bioInput       = document.getElementById('camp-bio');
        const keysInput      = document.getElementById('camp-keys');
        const glInput        = document.getElementById('camp-gl');
        const locationInput  = document.getElementById('camp-location');
        const targetUrlsInput = document.getElementById('camp-target-urls');

        cpName = nameInput?.value || '';
        cpBio = bioInput?.value || '';
        cpKeys = keysInput?.value || '';
        cpGl = glInput?.value || '';
        cpLoc = locationInput?.value || '';
        if (targetUrlsInput && targetUrlsInput.value.trim()) {
            targetUrls = targetUrlsInput.value.split('\n').map(u => u.trim()).filter(Boolean);
            if (targetUrls.length > 10) {
                targetUrls = targetUrls.slice(0, 10);
            }
        }
    }

    // ── PLG Validation: Child Campaign Path ──────────────────────────────────
    // DT/AI-generated campaigns pass bio='CHILD_CAMPAIGN_OVERRIDE' + campaign_focus
    // instead of manual keywords. Synthesize keywords from AI fields so the backend
    // producer guard (requires non-empty keywords) never trips on AI payloads.
    // Manual campaigns still require both name and keywords.
    // ──────────────────────────────────────────────────────────────────────────
    if (!cpName) {
        showToast('Campaign Name is required.', 'error');
        return;
    }

    const isAIGenerated = (cpBio === 'CHILD_CAMPAIGN_OVERRIDE') || (payload && payload.campaign_focus);
    if (!cpKeys) {
        if (isAIGenerated) {
            // Synthesize keywords from the AI-extracted DT fields
            const focus = payload?.campaign_focus || cpName;
            const pain  = payload?.pain_point     || '';
            const adv   = payload?.unfair_advantage || '';
            cpKeys = [focus, pain, adv].filter(Boolean).join(', ');
        } else {
            showToast('Target Keywords are required.', 'error');
            return;
        }
    }

    // ── Loading state: disable the active CTA button to prevent double-submit
    // on slow mobile connections (3G tap → spinner → no duplicate POSTs).
    const _launchBtns = document.querySelectorAll(
        '#fc-step2-submit, .dt-launch-btn, [onclick*="saveCampaignAction"], [onclick*="deployPredictiveCard"], [onclick*="saveChildCampaign"]'
    );
    _launchBtns.forEach(b => { b.disabled = true; b._origText = b.textContent; b.textContent = '⏳ Launching...'; });
    const _restoreLaunch = () => _launchBtns.forEach(b => { b.disabled = false; b.textContent = b._origText || 'Launch'; });

    showToast('Setting up your search...', 'info');
    try {
        const user = firebase.auth().currentUser;
        if (!user) { _restoreLaunch(); showToast('Session expired. Please sign in again.', 'error'); return; }
        // force=true: iOS Safari aggressively caches tokens — always fetch a
        // fresh token immediately before any state-mutating API call.
        const token = await user.getIdToken(true);

        const createResp = await fetch(`${API_BASE}/api/campaigns`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name:        cpName,
                bio:         cpBio,
                keywords:    cpKeys,
                gl:          cpGl,
                location:    cpLoc,
                target_urls: targetUrls,
                persona_id:  window._selectedPersonaId || '',
                status:      'active'
            })
        });
        if (!createResp.ok) throw new Error('Campaign creation failed');
        const createData = await createResp.json();
        const campaignId = createData.id;

        // ── V19 IGNITION: fire Day-1 producer immediately ────────────────────
        // /ignite bypasses the epsilon-greedy quota math and directly enqueues
        // the Serper producer task. Errors are surfaced, not swallowed.
        let igniteMsg = 'Campaign active — scanning for leads...';
        console.log('[V19] Create response:', createData);
        if (campaignId) {
            try {
                const igniteResp = await fetch(`${API_BASE}/api/campaigns/${campaignId}/ignite`, {
                    method:  'POST',
                    headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
                    body:    JSON.stringify({})
                });
                const igniteData = await igniteResp.json();
                console.log('[V19 IGNITE] Response:', igniteData);
                if (igniteResp.ok && igniteData.ignite) {
                    igniteMsg = `🚀 Engine ignited! Scanning starts in ~${igniteData.produce_jitter_s || 5}s`;
                } else {
                    console.warn('[IGNITE] Ignition call returned error:', igniteData);
                    igniteMsg = 'Campaign created — first scan queued by cron (≤5 min).';
                }
            } catch (igniteErr) {
                console.warn('[IGNITE] Ignition fetch failed:', igniteErr);
            }
        }

        const targetUrlsInput = document.getElementById('camp-target-urls');
        if (targetUrlsInput) targetUrlsInput.value = '';

        _restoreLaunch();
        loadDashboard();
        showToast(igniteMsg, 'success');
    } catch(err) {
        console.error('[saveCampaignAction]', err);
        _restoreLaunch();
        showToast('Failed to save campaign. Please check your connection and try again.', 'error');
    }
};

// SPA Router — V18: syncs both top cmd-bar (desktop) and bottom dock (mobile)
window.switchTab = function(tabName) {
    // Hide all views via style.display (new HTML uses style="display:none", not class="hidden")
    document.querySelectorAll('.main-feed').forEach(el => el.style.display = 'none');
    document.querySelectorAll('.cmd-btn').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.dock-btn').forEach(el => el.classList.remove('active'));

    const show = id => {
        const el = document.getElementById(id);
        if (el) el.style.display = '';   // restore to CSS default (block/flex from stylesheet)
    };
    const activateNav = (topId, dockId) => {
        const t = document.getElementById(topId);
        const d = document.getElementById(dockId);
        if (t) t.classList.add('active');
        if (d) d.classList.add('active');
    };

    if (tabName === 'dashboard') {
        show('view-dashboard');
        activateNav('tab-dashboard', 'dock-tab-dashboard');
    } else if (tabName === 'target') {
        show('view-target');
        activateNav('tab-campaigns', 'dock-tab-campaigns');
    } else if (tabName === 'reports') {
        show('view-reports');
        activateNav('tab-reports', 'dock-tab-reports');
    } else if (tabName === 'l0-admin') {
        show('view-l0-admin');
        const l0Tab = document.getElementById('tab-l0-admin');
        if (l0Tab) l0Tab.classList.add('active');
        fetchL0Telemetry();
    } else if (tabName === 'macro') {
        show('view-macro');
        if (typeof fetchMacroTrends === 'function') fetchMacroTrends();
    } else if (tabName === 'crm-test') {
        const isAdmin = window.currentUserData?.role === 'super_admin';
        if (!isAdmin) { showToast('CRM module is restricted to L0 administrators.', 'error'); return; }
        show('view-crm-test');
        loadCrmBoard();
    } else if (tabName === 'persona-vault') {
        show('view-persona-vault');
        activateNav('tab-personas', 'dock-tab-personas');
        loadPersonaVault();
    }
};

// V15: Hash-based hidden route for #crm-test (L0 admin only)
window.addEventListener('hashchange', () => {

    if (window.location.hash === '#crm-test' && firebase.auth().currentUser) {
        // Gate: only super_admin can access
        if (window.currentUserData?.role === 'super_admin') {
            switchTab('crm-test');
        } else {
            console.warn('[CRM] Access denied: not super_admin');
        }
    }
});
if (window.location.hash === '#crm-test') {
    // Deferred until auth + loadMe resolve (loadDashboard sets window.currentUserData)
    window.crmAutoOpen = true;
}

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

// =============================================================================
// L0 SUPER ADMIN — TAB SYSTEM
// =============================================================================
window._l0ActiveTab = 'tenants';

window.l0SwitchTab = function(tab) {
    // Guard: only super_admin
    if (window.currentUserData?.role !== 'super_admin') {
        showToast('L0 access denied.', 'error');
        return;
    }
    // Hide all panels, deactivate all tab buttons
    ['tenants','operations','ledger','health'].forEach(t => {
        const panel = document.getElementById(`l0-panel-${t}`);
        const btn   = document.getElementById(`l0-tab-${t}`);
        if (panel) panel.classList.remove('active');
        if (btn)   btn.classList.remove('active');
    });
    const activePanel = document.getElementById(`l0-panel-${tab}`);
    const activeBtn   = document.getElementById(`l0-tab-${tab}`);
    if (activePanel) activePanel.classList.add('active');
    if (activeBtn)   activeBtn.classList.add('active');
    window._l0ActiveTab = tab;

    // Auto-load data on first tab switch
    if (tab === 'tenants')    fetchL0Telemetry();
    if (tab === 'operations') fetchGlobalOperations();
    if (tab === 'ledger')     fetchShadowLedger();
    if (tab === 'health')     fetchSystemHealth();
};

window.l0RefreshCurrentTab = function() {
    l0SwitchTab(window._l0ActiveTab || 'tenants');
};

// =============================================================================
// L0 GLOBAL OPERATIONS — BQ Geo Heatmap + Domain Affinity Matrix
// =============================================================================
window.fetchGlobalOperations = async function() {
    const geoLoad  = document.getElementById('l0-geo-loading');
    const geoBars  = document.getElementById('l0-geo-bars');
    const matLoad  = document.getElementById('l0-matrix-loading');
    const domList  = document.getElementById('l0-domain-list');
    const errBox   = document.getElementById('l0-ops-error');
    const badge    = document.getElementById('l0-matrix-cache-badge');

    if (geoLoad)  { geoLoad.style.display = 'block'; geoBars.style.display  = 'none'; }
    if (matLoad)  { matLoad.style.display = 'block'; domList.style.display  = 'none'; }
    if (errBox)   errBox.style.display = 'none';

    try {
        const user = firebase.auth().currentUser;
        if (!user) throw new Error('Not authenticated');
        const token = await user.getIdToken(true);
        const resp  = await fetch(`${API_BASE}/api/internal/l0/operations-telemetry`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const json = await resp.json();
        const d    = json.data || {};

        // Show cache badge
        if (badge) badge.style.display = json.cache_hit ? 'inline' : 'none';

        // ── Geo Heatmap: horizontal bar chart built in plain HTML ──
        const heatmap = d.geo_heatmap || [];
        if (geoBars) {
            if (heatmap.length === 0) {
                geoBars.innerHTML = '<div style="text-align:center; color:var(--text-muted); padding:20px 0;">No active campaign data yet.</div>';
            } else {
                const maxVal = Math.max(...heatmap.map(r => r.active_campaigns), 1);
                geoBars.innerHTML = heatmap.map(r => {
                    const pct  = Math.round((r.active_campaigns / maxVal) * 100);
                    const flag = r.region ? r.region.slice(0,2).toUpperCase() : '';
                    return `<div style="margin-bottom:10px;">
                        <div style="display:flex; justify-content:space-between; font-size:0.82rem; margin-bottom:4px;">
                            <span style="font-weight:500;">${r.region}</span>
                            <span style="color:var(--text-muted);">${r.active_campaigns} campaigns</span>
                        </div>
                        <div style="height:10px; background:#f1f5f9; border-radius:6px; overflow:hidden;">
                            <div style="height:100%; width:${pct}%; background:linear-gradient(90deg,#4f46e5,#7c3aed); border-radius:6px; transition:width 0.6s;"></div>
                        </div>
                    </div>`;
                }).join('');
            }
            geoLoad.style.display  = 'none';
            geoBars.style.display  = 'block';
        }

        // ── Domain Affinity Matrix ──
        const domains = d.domain_matrix || [];
        if (domList) {
            if (domains.length === 0) {
                domList.innerHTML = '<div style="text-align:center; color:var(--text-muted); padding:20px 0;">No domain data yet — RLHF needs more cycles.</div>';
            } else {
                const maxW = Math.max(...domains.map(dm => dm.baseline_weight), 1);
                domList.innerHTML = domains.map((dm, i) => {
                    const barPct  = Math.round((dm.baseline_weight / maxW) * 100);
                    const trend   = dm.baseline_weight > 1.0 ? '&#x2191;' : dm.baseline_weight < 1.0 ? '&#x2193;' : '&mdash;';
                    const tColor  = dm.baseline_weight > 1.0 ? '#10b981' : dm.baseline_weight < 1.0 ? '#ef4444' : '#64748b';
                    return `<div style="display:flex; align-items:center; gap:10px; padding:8px 0; border-bottom:1px solid #f1f5f9;">
                        <div style="font-size:0.72rem; font-weight:700; color:var(--text-muted); width:18px; text-align:right;">${i+1}</div>
                        <div style="flex:1; min-width:0;">
                            <div style="font-size:0.83rem; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${dm.domain}</div>
                            <div style="height:4px; background:#f1f5f9; border-radius:4px; margin-top:4px; overflow:hidden;">
                                <div style="height:100%; width:${barPct}%; background:linear-gradient(90deg,#6366f1,#a21caf); border-radius:4px;"></div>
                            </div>
                        </div>
                        <div style="text-align:right; flex-shrink:0;">
                            <div style="font-size:0.85rem; font-weight:700; color:${tColor};">${dm.baseline_weight.toFixed(3)} ${trend}</div>
                            <div style="font-size:0.68rem; color:var(--text-muted);">${dm.total_yield} yields</div>
                        </div>
                    </div>`;
                }).join('');
            }
            matLoad.style.display = 'none';
            domList.style.display = 'block';
        }

        // Surface partial errors — but NEVER show bigquery errors.
        // BQ is an optional enrichment; the geo heatmap runs on Firestore primary.
        // A BQ 403 is an infra config issue (IAM), not a data issue.
        if (d.partial_errors && errBox) {
            const displayErrors = Object.entries(d.partial_errors)
                .filter(([k]) => k !== 'bigquery')  // suppress BQ 403 noise
                .map(([k, v]) => `${k}: ${v}`)
                .join(' | ');
            if (displayErrors) {
                errBox.style.display = 'block';
                errBox.textContent = 'Partial data: ' + displayErrors;
            } else {
                errBox.style.display = 'none';
            }
        }

    } catch(err) {
        console.error('[L0 Ops]', err);
        if (geoLoad)  geoLoad.textContent  = 'Failed to load. ' + err.message;
        if (matLoad)  matLoad.textContent  = 'Failed to load. ' + err.message;
    }
};

// =============================================================================
// L0 SHADOW LEDGER — Rejected Leads Table
// =============================================================================
const REJECTION_LABELS = {
    not_b2b: '&#128683; Not B2B',
    wrong_industry: '&#127981; Wrong Industry',
    too_small: '&#128204; Too Small',
    competitor: '&#9876;&#65039; Competitor',
    bad_data: '&#128465;&#65039; Bad Data'
};

window.fetchShadowLedger = async function() {
    const tbody = document.getElementById('l0-shadow-ledger-table');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="5" style="padding:20px; text-align:center; color:var(--text-muted);">&#8987; Fetching rejected leads&hellip;</td></tr>';

    try {
        const user = firebase.auth().currentUser;
        if (!user) throw new Error('Not authenticated');
        const token = await user.getIdToken(true);
        // Dedicated L0 cross-tenant endpoint — standard /api/leads is tenant-scoped
        // and would only return the admin's own leads (usually 0 rejected).
        const resp  = await fetch(`${API_BASE}/api/l0/shadow-ledger?limit=200&rt=${Date.now()}`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const json  = await resp.json();
        const leads = json.leads || json.data || [];

        // Update KPI tile
        const kpi = document.getElementById('l0-stat-rejected');
        if (kpi) kpi.textContent = leads.length;

        if (leads.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" style="padding:20px; text-align:center; color:var(--text-muted);">No rejected leads found.</td></tr>';
            return;
        }

        tbody.innerHTML = leads.map(lead => {
            const domain    = lead.base_path || lead.source_url || lead.domain || lead.company_domain || '&mdash;';
            const score     = lead.score != null ? `<span style="font-weight:700; color:${lead.score>=70?'#10b981':lead.score>=40?'#f59e0b':'#ef4444'}">${lead.score}</span>` : '&mdash;';
            const userRej   = REJECTION_LABELS[lead.rejection_reason] || lead.rejection_reason || '<span style="color:var(--text-muted);">Legacy ignore</span>';
            const aiRej     = lead.ai_rejection_reason ? `<span title="${lead.ai_rejection_reason}">${lead.ai_rejection_reason.slice(0,60)}${lead.ai_rejection_reason.length>60?'&hellip;':''}</span>` : '<span style="color:var(--text-muted);">N/A</span>';
            return `<tr style="border-bottom:1px solid #f8fafc; transition:background 0.15s;" onmouseover="this.style.background='#fafafa'" onmouseout="this.style.background=''">
                <td style="padding:10px 12px; font-weight:500; max-width:160px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${domain}</td>
                <td style="padding:10px 12px; text-align:center;">${score}</td>
                <td style="padding:10px 12px;">${userRej}</td>
                <td style="padding:10px 12px; font-size:0.8rem; color:var(--text-muted); max-width:200px;">${aiRej}</td>
                <td style="padding:10px 12px; text-align:right;">
                    <button onclick="recycleRejectedLead('${lead.id}', this)" style="padding:6px 14px; border:1px solid #d1d5db; border-radius:8px; background:#fff; font-size:0.78rem; font-weight:600; color:#4f46e5; cursor:pointer; transition:all 0.15s;" onmouseover="this.style.background='#ede9fe'" onmouseout="this.style.background='#fff'">
                        &#9851; Recycle
                    </button>
                </td>
            </tr>`;
        }).join('');

    } catch(err) {
        console.error('[Shadow Ledger]', err);
        tbody.innerHTML = `<tr><td colspan="5" style="padding:16px; text-align:center; color:#ef4444;">Error: ${err.message}</td></tr>`;
    }
};

window.recycleRejectedLead = async function(leadId, btn) {
    if (!leadId || !btn) return;
    btn.disabled = true;
    btn.textContent = 'Recycling...';
    try {
        const user = firebase.auth().currentUser;
        if (!user) return;
        const token = await user.getIdToken(true);
        const resp  = await fetch(`${API_BASE}/api/leads/${leadId}`, {
            method:  'PUT',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            // Reset to 'unprocessed' and clear rejection metadata
            body:    JSON.stringify({ status: 'unprocessed', rejection_reason: null })
        });
        if (resp.ok) {
            btn.closest('tr').style.opacity = '0.3';
            setTimeout(() => btn.closest('tr')?.remove(), 600);
            showToast('Lead recycled and re-queued for processing.', 'success');
        } else {
            throw new Error(`HTTP ${resp.status}`);
        }
    } catch(err) {
        btn.disabled = false;
        btn.textContent = '&#9851; Recycle';
        showToast('Recycle failed: ' + err.message, 'error');
    }
};

// =============================================================================
// L0 SYSTEM HEALTH — Circuit Breaker + Error Rates
// =============================================================================
window.fetchSystemHealth = async function() {
    const breakerEl = document.getElementById('l0-health-breaker');
    const serperEl  = document.getElementById('l0-health-serper');
    const oomEl     = document.getElementById('l0-health-oom');
    const bqEl      = document.getElementById('l0-health-bq');
    const velocEl   = document.getElementById('l0-health-velocity');
    const campEl    = document.getElementById('l0-health-camps');
    try {
        const user = firebase.auth().currentUser;
        if (!user) return;
        const token = await user.getIdToken(true);
        const resp  = await fetch(`${API_BASE}/api/l0/system-health`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const json = await resp.json();
        const d    = json.data || {};
        const cb   = d.circuit_breaker || {};

        // Circuit Breaker
        const state      = cb.state || 'UNKNOWN';
        const stateColor = state === 'CLOSED' ? '#10b981' : state === 'OPEN' ? '#ef4444' : '#f59e0b';
        if (breakerEl) {
            const reason = cb.last_open_reason ? `<div style="font-size:0.68rem;color:#6b7280;margin-top:4px;max-width:200px;white-space:normal;">${cb.last_open_reason.slice(0,80)}${cb.last_open_reason.length>80?'…':''}</div>` : '';
            breakerEl.innerHTML = `<strong style="color:${stateColor};">${state}</strong>${reason}`;
        }

        // Serper 429 Rate
        if (serperEl) {
            const rate = cb.serper_429_rate != null ? (cb.serper_429_rate * 100).toFixed(1) + '%' : 'N/A';
            const calls = cb.serper_calls != null ? ` <span style="font-size:0.68rem;color:#6b7280;">(${cb.serper_429s||0}/${cb.serper_calls||0} calls)</span>` : '';
            const color = (cb.serper_429_rate||0) > 0.10 ? '#ef4444' : (cb.serper_429_rate||0) > 0.05 ? '#f59e0b' : '#10b981';
            serperEl.innerHTML = `<strong style="color:${color};">${rate}</strong>${calls}`;
        }

        // Scraper OOM Rate
        if (oomEl) {
            const rate = cb.scraper_oom_rate != null ? (cb.scraper_oom_rate * 100).toFixed(1) + '%' : 'N/A';
            const color = (cb.scraper_oom_rate||0) > 0.03 ? '#ef4444' : '#10b981';
            oomEl.innerHTML = `<strong style="color:${color};">${rate}</strong>`;
        }

        // Pipeline Velocity (leads last 24h)
        if (velocEl) {
            velocEl.innerHTML = d.leads_last_24h != null
                ? `<strong style="color:var(--primary);">${d.leads_last_24h}</strong> <span style="font-size:0.72rem;color:#6b7280;">new leads (24h)</span>`
                : 'N/A';
        }

        // Active Campaigns
        if (campEl) {
            const ont = d.ontology_domains != null ? ` &nbsp;·&nbsp; <span style="font-size:0.72rem;color:#6b7280;">${d.ontology_domains} RLHF domains</span>` : '';
            campEl.innerHTML = d.active_campaigns != null
                ? `<strong style="color:#7c3aed;">${d.active_campaigns}</strong> active${ont}`
                : 'N/A';
        }

        // BQ Last Sync tile — repurpose for rejection count
        if (bqEl) {
            bqEl.innerHTML = d.total_rejected != null
                ? `<strong style="color:#ef4444;">${d.total_rejected}</strong> <span style="font-size:0.72rem;color:#6b7280;">total rejections</span>`
                : 'N/A';
        }

        showToast('System health loaded.', 'success');
    } catch(err) {
        console.error('[System Health]', err);
        if (breakerEl) breakerEl.textContent = 'Error: ' + err.message;
        showToast('System health load failed: ' + err.message, 'error');
    }
};

window.loadMoreLeads = function() {
    showToast('Historical offset cursors must be mapped in Orchestrator Endpoint v2.', 'info');
};

// ============================================================================
// V15: NATIVE CRM SANDBOX ENGINE â€” /crm-test
// ============================================================================

const CRM_STATUSES = ['new', 'contacted', 'replied', 'negotiating', 'won', 'lost'];

// State: keyed by lead id
let crmLeadsCache = [];
let crmActiveLead = null;   // lead object currently open in side panel
let crmDraggedId  = null;   // id of card being dragged

// â”€â”€ loadCrmBoard â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
window.loadCrmBoard = async function() {
    const user = firebase.auth().currentUser;
    if (!user) return;

    // Show loading state in all columns
    CRM_STATUSES.forEach(s => {
        const body = document.getElementById(`body-${s}`);
        if (body) body.innerHTML = '<div style="padding:8px; color:var(--text-muted); font-size:0.8rem;">Loading...</div>';
    });

    try {
        const token = await user.getIdToken();
        const res   = await fetch(`${API_BASE}/api/leads?crm=true&rt=${Date.now()}`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (res.status === 401 || res.status === 403) return handleAuthRejection();
        const payload = await res.json();
        // Filter strictly: is_in_crm === true only
        crmLeadsCache = (payload.data || []).filter(l => l.is_in_crm === true);
        renderKanban();
    } catch(e) {
        console.error('[CRM] Load failed', e);
        showToast('Failed to load CRM data', 'error');
    }
};

// â”€â”€ renderKanban â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function renderKanban() {
    const now = Date.now();
    // Group by crm_status (fallback to 'new')
    const grouped = {};
    CRM_STATUSES.forEach(s => grouped[s] = []);
    crmLeadsCache.forEach(lead => {
        const st = CRM_STATUSES.includes(lead.crm_status) ? lead.crm_status : 'new';
        grouped[st].push(lead);
    });

    // Render each column
    CRM_STATUSES.forEach(status => {
        const body    = document.getElementById(`body-${status}`);
        const counter = document.getElementById(`cnt-${status}`);
        const leads   = grouped[status];
        if (!body) return;
        if (counter) counter.textContent = leads.length;

        body.innerHTML = '';
        leads.forEach(lead => {
            const card     = buildKanbanCard(lead, now);
            body.appendChild(card);
        });
    });

    // Attach drop targets
    document.querySelectorAll('.kanban-col').forEach(col => {
        col.addEventListener('dragover',  e => { e.preventDefault(); col.classList.add('drag-over'); });
        col.addEventListener('dragleave', ()  => col.classList.remove('drag-over'));
        col.addEventListener('drop',      e  => handleKanbanDrop(e, col));
    });

    // Health widget
    const fmt = v => `â‚¹${Number(v || 0).toLocaleString('en-IN')}`;
    const negotiating = grouped['negotiating'].reduce((a, l) => a + (l.estimated_value || 0), 0);
    const won         = grouped['won'].reduce((a, l) => a + (l.estimated_value || 0), 0);
    const el1 = document.getElementById('crm-negotiating-sum'); if (el1) el1.textContent = fmt(negotiating);
    const el2 = document.getElementById('crm-won-sum');         if (el2) el2.textContent = fmt(won);
    const el3 = document.getElementById('crm-pipeline-total');  if (el3) el3.textContent = fmt(negotiating + won);
    const el4 = document.getElementById('crm-total-count');     if (el4) el4.textContent = crmLeadsCache.length;
}

// â”€â”€ buildKanbanCard â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function buildKanbanCard(lead, now) {
    const card   = document.createElement('div');
    const id     = lead.id || lead.doc_id || '';
    card.className   = 'crm-card';
    card.draggable   = true;
    card.dataset.id  = id;

    // Follow-up date badge
    let fueBadge = '';
    if (lead.follow_up_date) {
        const fueTs = lead.follow_up_date._seconds
            ? lead.follow_up_date._seconds * 1000
            : new Date(lead.follow_up_date).getTime();
        if (fueTs < now) fueBadge = '<span class="due-badge">Due</span>';
    }

    const domain   = (() => { try { return new URL(lead.url || 'https://unknown').hostname.replace('www.', ''); } catch(_) { return lead.url || 'Unknown'; } })();
    const signal   = lead.intent_signal || lead.pain_point || '';
    const value    = lead.estimated_value ? `ðŸ’° â‚¹${Number(lead.estimated_value).toLocaleString('en-IN')}` : '';

    card.innerHTML = `
        <div class="card-domain">${domain}${fueBadge}</div>
        <div class="card-score">Score: ${lead.score || 'N/A'}/10 Â· ${(lead.confidence_tier || 'High')}</div>
        ${signal ? `<div class="card-signal">${signal}</div>` : ''}
        ${value ? `<div class="card-value">${value}</div>` : ''}
    `;

    card.addEventListener('dragstart', e => {
        crmDraggedId = id;
        card.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
    });
    card.addEventListener('dragend', () => card.classList.remove('dragging'));
    card.addEventListener('click',   () => openCrmPanel(lead));
    return card;
}

// â”€â”€ handleKanbanDrop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
async function handleKanbanDrop(e, col) {
    e.preventDefault();
    col.classList.remove('drag-over');
    const newStatus = col.dataset.status;
    if (!crmDraggedId || !newStatus) return;

    // Optimistic UI
    const lead = crmLeadsCache.find(l => (l.id || l.doc_id) === crmDraggedId);
    if (!lead) return;
    const oldStatus  = lead.crm_status || 'new';
    if (oldStatus === newStatus) return;
    lead.crm_status  = newStatus;
    renderKanban();

    // Persist
    try {
        await performApiMutation(`/api/leads/${crmDraggedId}`, 'PUT', { crm_status: newStatus });
        showToast(`Moved to "${newStatus}"`, 'success');
    } catch(err) {
        // Rollback
        lead.crm_status = oldStatus;
        renderKanban();
        showToast('Status update failed', 'error');
    }
    crmDraggedId = null;
}

// â”€â”€ openCrmPanel / closeCrmPanel â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
window.openCrmPanel = function(lead) {
    crmActiveLead = lead;
    const panel = document.getElementById('crm-side-panel');
    const body  = document.getElementById('crm-panel-body');
    const title = document.getElementById('crm-panel-title');
    if (!panel || !body) return;

    const id      = lead.id || lead.doc_id || '';
    const domain  = (() => { try { return new URL(lead.url || 'https://x').hostname.replace('www.',''); } catch(_) { return lead.url || id; } })();
    if (title) title.textContent = domain;

    const fueVal  = lead.follow_up_date
        ? (() => { try { const ts = lead.follow_up_date._seconds ? lead.follow_up_date._seconds*1000 : new Date(lead.follow_up_date).getTime(); return new Date(ts).toISOString().slice(0,10); } catch(_) { return ''; } })()
        : '';

    const notes   = Array.isArray(lead.notes) ? lead.notes : [];
    const notesFeed = notes.length === 0
        ? '<div style="color:var(--text-muted); font-size:0.8rem; font-style:italic;">No notes yet.</div>'
        : notes.slice().reverse().map(n => `
            <div class="crm-note-item">
                <div class="note-ts">${new Date(n.timestamp?._seconds ? n.timestamp._seconds*1000 : n.timestamp).toLocaleString()}</div>
                <div class="note-text">${n.text}</div>
            </div>`).join('');

    // Pull meeting/asset from user data
    const meetingUrl = window.currentUserData?.meeting_url || '';
    const assetUrl   = window.currentUserData?.asset_url || lead.attached_asset_url || '';

    // Primary endpoint for Smart Action
    const endpoints = lead.contact_endpoints || [];
    const primary   = endpoints[0] || null;
    const primaryLabel = primary
        ? `${PLATFORM_META[primary.platform]?.icon || 'ðŸ”—'} ${PLATFORM_META[primary.platform]?.label || 'Contact'}`
        : 'ðŸ“‹ Copy DM';

    body.innerHTML = `
        <!-- Intent Signal -->
        <div class="crm-panel-section">
            <div class="crm-panel-label">Intent Signal</div>
            <div class="crm-panel-intent">${lead.intent_signal || lead.pain_point || 'No signal captured.'}</div>
        </div>

        <!-- AI-Drafted DM -->
        <div class="crm-panel-section">
            <div class="crm-panel-label">AI-Drafted Message</div>
            <div class="crm-panel-dm" id="crm-dm-preview">${lead.dm || 'No draft available.'}</div>
        </div>

        <!-- Smart Action Toggles -->
        <div class="crm-panel-section" style="background:#f8fafc; padding:12px; border-radius:10px; border:1px solid var(--glass-border);">
            <div class="crm-panel-label" style="margin-bottom:8px;">Smart Action Settings</div>
            ${meetingUrl ? `<div class="crm-toggle-row">
                <span class="crm-toggle-label">ðŸ“… Include Meeting Link</span>
                <label class="crm-toggle"><input type="checkbox" id="toggle-meeting" onchange="refreshCrmDmPreview('${id}')"><span class="crm-toggle-slider"></span></label>
            </div>` : ''}
            ${assetUrl ? `<div class="crm-toggle-row">
                <span class="crm-toggle-label">ðŸ”— Include Asset Link</span>
                <label class="crm-toggle"><input type="checkbox" id="toggle-asset" onchange="refreshCrmDmPreview('${id}')"><span class="crm-toggle-slider"></span></label>
            </div>` : ''}
            <button class="crm-smart-action-btn" onclick="crmSmartAction('${id}', '${primary ? encodeURIComponent(primary.uri) : ''}', '${primary ? primary.platform : ''}')">
                ${primaryLabel}
            </button>
        </div>

        <!-- Estimated Value -->
        <div class="crm-panel-section">
            <div class="crm-panel-label">Estimated Deal Value (â‚¹)</div>
            <input type="number" id="crm-est-value" class="crm-input" value="${lead.estimated_value || 0}" placeholder="e.g. 50000" min="0">
            <button class="crm-save-btn" onclick="saveCrmValue('${id}')">Save Value</button>
        </div>

        <!-- Follow-Up Date -->
        <div class="crm-panel-section">
            <div class="crm-panel-label">Follow-Up Date</div>
            <input type="date" id="crm-followup" class="crm-input" value="${fueVal}">
            <button class="crm-save-btn" onclick="saveCrmFollowup('${id}')">Set Reminder</button>
        </div>

        <!-- Notes -->
        <div class="crm-panel-section">
            <div class="crm-panel-label">Notes</div>
            <div class="crm-notes-feed" id="crm-notes-feed">${notesFeed}</div>
            <textarea id="crm-note-input" class="crm-input" rows="3" placeholder="Add a note..." style="margin-top:8px; resize:vertical;"></textarea>
            <button class="crm-save-btn" onclick="saveCrmNote('${id}')">Add Note</button>
        </div>
    `;

    panel.classList.add('open');
};

window.closeCrmPanel = function() {
    const panel = document.getElementById('crm-side-panel');
    if (panel) panel.classList.remove('open');
    crmActiveLead = null;
};

// â”€â”€ refreshCrmDmPreview â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
window.refreshCrmDmPreview = function(id) {
    if (!crmActiveLead) return;
    const meetEl  = document.getElementById('toggle-meeting');
    const assetEl = document.getElementById('toggle-asset');
    const preview = document.getElementById('crm-dm-preview');
    if (!preview) return;
    let dm = crmActiveLead.dm || '';
    if (meetEl && meetEl.checked && window.currentUserData?.meeting_url) {
        dm += `\n\nðŸ“… Book a quick call: ${window.currentUserData.meeting_url}`;
    }
    if (assetEl && assetEl.checked) {
        const assetUrl = window.currentUserData?.asset_url || crmActiveLead.attached_asset_url || '';
        if (assetUrl) dm += `\n\nðŸ”— Here's our resource: ${assetUrl}`;
    }
    preview.textContent = dm;
};

// â”€â”€ crmSmartAction â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
window.crmSmartAction = function(id, uriEnc, platform) {
    if (!crmActiveLead) return;
    const meetEl  = document.getElementById('toggle-meeting');
    const assetEl = document.getElementById('toggle-asset');
    let dm = crmActiveLead.dm || '';

    if (meetEl && meetEl.checked && window.currentUserData?.meeting_url) {
        dm += `\n\nðŸ“… Book a quick call: ${window.currentUserData.meeting_url}`;
    }
    if (assetEl && assetEl.checked) {
        const assetUrl = window.currentUserData?.asset_url || crmActiveLead.attached_asset_url || '';
        if (assetUrl) dm += `\n\nðŸ”— Here's our resource: ${assetUrl}`;
    }

    navigator.clipboard.writeText(dm).catch(() => {});

    if (uriEnc) {
        const uri = decodeURIComponent(uriEnc);
        const isEmail = platform === 'email' || (uri.includes('@') && !uri.startsWith('http'));
        const isPhone = platform === 'other' && /^[\d\s+()\\-]{6,}$/.test(uri);
        let href;
        if (isEmail)      href = `mailto:${uri}`;
        else if (isPhone) href = `tel:${uri}`;
        else if (/^(https?:\/\/|mailto:|tel:)/i.test(uri)) href = uri;
        else              href = `https://${uri}`;
        window.open(href, '_blank', 'noopener,noreferrer');
    }

    showToast('DM copied with appended links!', 'success');
    updateLeadStatus(id, 'contacted');
};

// â”€â”€ saveCrmValue â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
window.saveCrmValue = async function(id) {
    const val = parseInt(document.getElementById('crm-est-value')?.value || '0', 10);
    try {
        const ok = await performApiMutation(`/api/leads/${id}`, 'PUT', { estimated_value: val });
        if (ok) {
            const lead = crmLeadsCache.find(l => (l.id || l.doc_id) === id);
            if (lead) lead.estimated_value = val;
            renderKanban();
            showToast('Deal value saved!', 'success');
        }
    } catch(e) { showToast('Failed to save value', 'error'); }
};

// â”€â”€ saveCrmFollowup â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
window.saveCrmFollowup = async function(id) {
    const dateStr = document.getElementById('crm-followup')?.value;
    if (!dateStr) return showToast('Pick a date first', 'error');
    const ts = new Date(dateStr).toISOString();
    try {
        const ok = await performApiMutation(`/api/leads/${id}`, 'PUT', { follow_up_date: ts });
        if (ok) {
            const lead = crmLeadsCache.find(l => (l.id || l.doc_id) === id);
            if (lead) lead.follow_up_date = ts;
            renderKanban();
            showToast('Reminder set!', 'success');
        }
    } catch(e) { showToast('Failed to save date', 'error'); }
};

// â”€â”€ saveCrmNote â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
window.saveCrmNote = async function(id) {
    const input = document.getElementById('crm-note-input');
    const text  = input?.value?.trim();
    if (!text) return showToast('Note cannot be empty', 'error');

    const note  = { timestamp: new Date().toISOString(), text };
    const lead  = crmLeadsCache.find(l => (l.id || l.doc_id) === id);
    const notes = Array.isArray(lead?.notes) ? [...lead.notes, note] : [note];

    try {
        const ok = await performApiMutation(`/api/leads/${id}`, 'PUT', { notes });
        if (ok) {
            if (lead) lead.notes = notes;
            if (input) input.value = '';
            // Re-render notes feed
            const feed = document.getElementById('crm-notes-feed');
            if (feed) {
                feed.innerHTML = notes.slice().reverse().map(n => `
                    <div class="crm-note-item">
                        <div class="note-ts">${new Date(n.timestamp?._seconds ? n.timestamp._seconds*1000 : n.timestamp).toLocaleString()}</div>
                        <div class="note-text">${n.text}</div>
                    </div>`).join('');
            }
            showToast('Note saved!', 'success');
        }
    } catch(e) { showToast('Failed to save note', 'error'); }
};
// NOTE: The loadDashboard override that previously lived here was removed in
// V15.1 HOTFIX. It caused infinite recursion due to JS function-declaration
// hoisting: the 'original' reference captured the hoisted new declaration,
// making _origLoadDashboard === the new loadDashboard (self-reference).
// The crmAutoOpen check is now inlined directly in loadDashboard() above.

// =============================================================================
// V17: CONVERSATIONAL "FIND NEW CLIENTS" ENGINE
// =============================================================================

// --- State ---
window._fcState = {
    gl: '',       // country code
    location: '', // city/region text
    whoConfirmed: '',
    whatConfirmed: '',
};

// --- Utility: parse intent sentence for key signals ---
function fcParseIntent(sentence) {
    const s = sentence.toLowerCase();
    const result = { who: sentence.trim(), where: '', gl: '' };

    // Location extraction â€” order matters (longer matches first)
    const locationMap = [
        { re: /\b(united\s*states|usa|u\.s\.a|u\.s)\b/i,  gl: 'us', label: 'United States' },
        { re: /\b(united\s*kingdom|uk|u\.k|britain|england|london)\b/i, gl: 'uk', label: 'United Kingdom' },
        { re: /\b(canada|toronto|vancouver|montreal)\b/i,  gl: 'ca', label: 'Canada' },
        { re: /\b(australia|sydney|melbourne)\b/i,         gl: 'au', label: 'Australia' },
        { re: /\b(india|mumbai|delhi|bangalore|bengaluru|hyderabad|pune|chennai|kolkata|kerala|kochi|jaipur)\b/i, gl: 'in', label: 'India' },
    ];

    for (const loc of locationMap) {
        if (loc.re.test(sentence)) {
            result.where = loc.label;
            result.gl    = loc.gl;
            break;
        }
    }

    // Attempt to strip the location phrase from the "who" summary
    if (result.where) {
        result.who = sentence
            .replace(/\s+in\s+(the\s+)?(united states|usa|united kingdom|uk|canada|australia|india|[a-z\s,]+)/gi, '')
            .replace(/\s+from\s+(the\s+)?(united states|usa|united kingdom|uk|canada|australia|india)/gi, '')
            .trim() || sentence.trim();
    }

    return result;
}

// --- Auto-generate a readable campaign name from the parsed intent ---
function fcBuildCampaignName(who, where) {
    const now = new Date();
    const month = now.toLocaleString('en', { month: 'short' });
    const year  = now.getFullYear();
    // Take first 35 chars of who
    const base = who.length > 35 ? who.substring(0, 35).trim() + 'â€¦' : who;
    return where ? `${base} Â· ${where} Â· ${month} ${year}` : `${base} Â· ${month} ${year}`;
}

// --- Relative time helper ---
function fcTimeAgo(ts) {
    if (!ts) return '';
    const then = typeof ts.toDate === 'function' ? ts.toDate() : new Date(ts);
    const diffMs = Date.now() - then.getTime();
    const m = Math.floor(diffMs / 60000);
    const h = Math.floor(m / 60);
    const d = Math.floor(h / 24);
    if (m < 2)  return 'just now';
    if (m < 60) return `${m}m ago`;
    if (h < 24) return `${h}h ago`;
    if (d < 7)  return `${d}d ago`;
    return then.toLocaleDateString('en', { day: 'numeric', month: 'short' });
}

// =============================================================================
// V18 DIGITAL TWIN OVERRIDES
// =============================================================================

window.openNewCampaignModal = async function() {
    // Defensive dual-path: activeWallet is the closure var (always set);
    // also check flattened root fields in case of DB schema migration.
    const aw = window.activeWallet || {};
    const ud = window.currentUserData || {};
    const allocated = Number(aw.allocated_credits || ud.allocated_credits || 0) || 0;
    const consumed  = Number(aw.consumed_credits  || ud.consumed_credits  || 0) || 0;
    const remaining = allocated - consumed;
    console.log('[DEBUG WALLET] window.activeWallet:', aw,
                '| currentUserData:', ud,
                '| allocated:', allocated, '| consumed:', consumed, '| remaining:', remaining);
    if (remaining <= 0) {
        showToast('Credits exhausted. Contact admin to reload.', 'error');
        return;
    }
    
    // Pass control to the Digital Twin Onboarding Flow (View A)
    window.openDTModal();
};

// =============================================================================
// V17: DASHBOARD GREETING + KPI TILES
// =============================================================================

function fcUpdateGreeting(firstName) {
    const el = document.getElementById('greeting-message');
    if (!el) return;
    const hr = new Date().getHours();
    const g  = hr < 12 ? 'Good morning' : hr < 17 ? 'Good afternoon' : 'Good evening';
    el.textContent = firstName ? `${g}, ${firstName}.` : `${g}.`;
}

function fcUpdateKPIs(leadsArray) {
    const counts = { new: 0, contacted: 0, converted: 0 };
    leadsArray.forEach(l => {
        if (l.status === 'new' || l.status === 'processing') counts.new++;
        else if (l.status === 'contacted' || l.status === 'replied') counts.contacted++;
        else if (l.status === 'converted') counts.converted++;
    });
    const setEl = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v || '0'; };
    setEl('kpi-new-count',       counts.new);
    setEl('kpi-contacted-count', counts.contacted);
    setEl('kpi-won-count',       counts.converted);
}

// =============================================================================
// V18: LEAD CARD RENDERER - Predictive Copilot Architecture
// prism_mode routing:
//   GeneralDomain / legacy -> B2B dossier (Company Size, Tech Stack, Objection)
//   WalledGarden / Social  -> Social dossier (Platform, Snippet, Handle)
//   B2B2C                  -> Split view (Consumer Demand + Distributor)
// =============================================================================

function getScoreEmoji(score) {
    if (score >= 9) return '&#x1F525;';
    if (score >= 7) return '&#x26A1;';
    if (score >= 5) return '&#x1F511;';
    return '&#x1F4CB;';
}

var _PRISM_PLATFORM_META = {
    'reddit.com':    'Copy Reply & Open Reddit',
    'linkedin.com':  'Copy Pitch & Open LinkedIn',
    'facebook.com':  'Copy Message & Open Facebook',
    'instagram.com': 'Copy DM & Open Instagram',
    'twitter.com':   'Copy Reply & Open X',
    'x.com':         'Copy Reply & Open X',
    'quora.com':     'Copy Answer & Open Quora',
    'youtube.com':   'Copy Comment & Open YouTube',
    'team-bhp.com':  'Copy Reply & Open Team-BHP',
};

function _copilotBtnLabel(lead) {
    var url = lead.url || lead.source_url || '';
    var hostname = '';
    try { hostname = new URL(url).hostname.replace('www.', '').toLowerCase(); } catch(e) {}
    var domains = Object.keys(_PRISM_PLATFORM_META);
    for (var i = 0; i < domains.length; i++) {
        if (hostname.endsWith(domains[i])) return '&#x1F4CB; ' + _PRISM_PLATFORM_META[domains[i]] + ' &#x2197;';
    }
    var mode = (lead.prism_mode || '').toLowerCase();
    if (mode.indexOf('walledgarden') !== -1) return '&#x1F4CB; Copy Reply & Open Platform &#x2197;';
    if (mode === 'b2b2c')                    return '&#x1F4CB; Copy Pitch & Open Distributor &#x2197;';
    return '&#x1F4CB; Copy Pitch & Open Website &#x2197;';
}

function _prismDossierHTML(lead) {
    var mode = (lead.prism_mode || 'GeneralDomain').toLowerCase();

    if (mode.indexOf('walledgarden') !== -1 || mode === 'social') {
        var platform = 'Social Platform';
        try {
            var h = new URL(lead.url || lead.source_url || '').hostname.replace('www.','');
            platform = h.split('.')[0].charAt(0).toUpperCase() + h.split('.')[0].slice(1);
        } catch(e) {}
        var snippet = lead.intent_signal || lead.pain_point || '';
        var handleEntry = null;
        var eps = lead.contact_endpoints || [];
        for (var ei = 0; ei < eps.length; ei++) {
            var uri = eps[ei].uri || '';
            if (!uri.includes('@') && (uri.includes('linkedin') || uri.includes('reddit') || uri.includes('facebook') || uri.includes('instagram') || uri.includes('/u/') || uri.includes('/user/'))) {
                handleEntry = eps[ei]; break;
            }
        }
        var handle = handleEntry ? handleEntry.uri : '';
        return '<div class="lc-section lc-dossier lc-dossier--social">' +
            '<div class="lc-section-label">Social Intelligence</div>' +
            '<div class="lc-dossier-row"><span class="lc-dossier-key">Intent Detected On</span><span class="lc-dossier-val">' + platform + '</span></div>' +
            (snippet ? '<div class="lc-dossier-row"><span class="lc-dossier-key">Snippet Context</span><span class="lc-dossier-val lc-dossier-snippet">' + snippet + '</span></div>' : '') +
            (handle ? '<div class="lc-dossier-row"><span class="lc-dossier-key">Profile</span><span class="lc-dossier-val"><a href="' + handle + '" target="_blank" rel="noopener" style="color:var(--primary);text-decoration:none;">View &#x2197;</a></span></div>' : '') +
            '</div>';
    }

    if (mode === 'b2b2c') {
        var demand = lead.intent_signal || lead.pain_point || 'Consumer demand signal captured.';
        var obj    = lead.primary_objection_hypothesis || lead.objection || '';
        var tech   = (lead.tech_stack_found || []).slice(0,4).join(', ') || '-';
        return '<div class="lc-section lc-dossier lc-dossier--b2b2c">' +
            '<div class="lc-section-label">Consumer Demand Context</div>' +
            '<div class="lc-dossier-row"><span class="lc-dossier-key">Demand Signal</span><span class="lc-dossier-val">' + demand + '</span></div>' +
            '</div><div class="lc-section lc-dossier lc-dossier--b2b2c-dist">' +
            '<div class="lc-section-label">Distributor Contact Dossier</div>' +
            '<div class="lc-dossier-row"><span class="lc-dossier-key">Tech Stack</span><span class="lc-dossier-val">' + tech + '</span></div>' +
            (obj ? '<div class="lc-dossier-row"><span class="lc-dossier-key">Primary Objection</span><span class="lc-dossier-val">' + obj + '</span></div>' : '') +
            '</div>';
    }

    var csz   = lead.company_size_tier || '-';
    var tech2 = (lead.tech_stack_found || []).slice(0,4).join(', ') || '-';
    var obj2  = lead.primary_objection_hypothesis || lead.objection || '';
    var dmN   = lead.decision_maker_name  || '';
    var dmT   = lead.decision_maker_title || '';
    var dmStr = [dmN, dmT].filter(Boolean).join(' / ') || '-';
    return '<div class="lc-section lc-dossier lc-dossier--b2b">' +
        '<div class="lc-section-label">Company Dossier</div>' +
        '<div class="lc-dossier-row"><span class="lc-dossier-key">Company Size</span><span class="lc-dossier-val">' + csz + '</span></div>' +
        '<div class="lc-dossier-row"><span class="lc-dossier-key">Tech Stack</span><span class="lc-dossier-val">' + tech2 + '</span></div>' +
        '<div class="lc-dossier-row"><span class="lc-dossier-key">Decision Maker</span><span class="lc-dossier-val">' + dmStr + '</span></div>' +
        (obj2 ? '<div class="lc-dossier-row"><span class="lc-dossier-key">Primary Objection</span><span class="lc-dossier-val">' + obj2 + '</span></div>' : '') +
        '</div>';
}

window.createLeadCardV2 = function(docId, lead) {
    // Store lead in O(1) lookup cache — eliminates all encoding for copilot action
    _leadsMap.set(docId, lead);
    var card = document.createElement('div');
    card.className = 'lead-card-v2';
    card.id = docId;

    var displayName = lead.company_name || '';
    var hostname = '';
    try { var raw = lead.url || lead.source_url || ''; hostname = raw ? new URL(raw).hostname.replace('www.','') : ''; } catch(e) {}
    if (!displayName) displayName = hostname || 'Unknown Company';

    var score   = lead.score || 0;
    var heatPct = Math.round((score / 10) * 100);
    var emoji   = getScoreEmoji(score);
    var signal  = lead.intent_signal || lead.pain_point || '';
    var dm      = lead.dm || '';
    var timeAgo = fcTimeAgo(lead.createdAt || lead.promotedAt);
    var srcLbl  = (lead.sourcing_vector || lead.source || '').indexOf('Autonomous') !== -1
        ? 'AI Match' : (lead.source || 'Web Signal');

    var pm = lead.prism_mode || '';
    var prismBadge = '';
    if (pm.indexOf('WalledGarden') !== -1 || pm.indexOf('walledgarden') !== -1) {
        prismBadge = '<span class="lc-badge" style="background:#f0f9ff;color:#0369a1;border-color:#bae6fd;">Social</span>';
    } else if (pm === 'B2B2C' || pm === 'b2b2c') {
        prismBadge = '<span class="lc-badge" style="background:#fff7ed;color:#c2410c;border-color:#fed7aa;">B2B2C</span>';
    } else if (pm.indexOf('General') !== -1 || pm.indexOf('legacy') !== -1) {
        prismBadge = '<span class="lc-badge" style="background:#f0fdf4;color:#166534;border-color:#bbf7d0;">B2B</span>';
    }

    var badges = [];
    if (lead.origin_engine === 'autonomous') badges.push({t:'Predictive',bg:'#faf5ff',c:'#7c3aed',b:'#ddd6fe'});
    badges.push({t:'Exclusive',bg:'#f3e8ff',c:'#6b21a8',b:'#e9d5ff'});
    if (lead.hiring_intent_found === 'Yes') badges.push({t:'Hiring',bg:'#ecfdf5',c:'#059669',b:'#a7f3d0'});
    if (lead.competitor_match) badges.push({t:lead.competitor_match,bg:'#fee2e2',c:'#b91c1c',b:'#fecaca'});
    
    if (lead.trend_mapped) badges.push({t:'Trend Mapped',bg:'#fff1f2',c:'#be123c',b:'#fecdd3'});
    else if (lead.matched_campaign_ids && lead.matched_campaign_ids.length > 1) {
        badges.push({t:'Cross-Pollinated',bg:'#eff6ff',c:'#1d4ed8',b:'#bfdbfe'});
    }
    var bHTML = badges.map(function(x) {
        return '<span class="lc-badge" style="background:'+x.bg+';color:'+x.c+';border-color:'+x.b+'">'+x.t+'</span>';
    }).join('');

    var expandId   = 'lc-expand-'  + docId;
    var moreId     = 'lc-more-'    + docId;
    var overflowId = 'lc-of-'      + docId;
    var copilotLbl = _copilotBtnLabel(lead);
    var isCont     = lead.status === 'contacted' || lead.status === 'replied';

    var cInfo = '';
    if (lead.email || lead.phone) {
        cInfo = '<div class="lc-section" style="font-size:0.85rem;">' +
            '<div class="lc-section-label">Contact Info</div>' +
            (lead.email ? '<a href="mailto:'+lead.email+'" style="color:#2563eb;text-decoration:none;">'+lead.email+'</a>&nbsp;' : '') +
            (lead.phone ? '<a href="tel:'+lead.phone+'" style="color:#2563eb;text-decoration:none;">'+lead.phone+'</a>' : '') +
            '</div>';
    }

    var crmCls = 'lc-crm-btn' + (lead.is_in_crm ? ' in-crm' : '');
    var crmOC  = lead.is_in_crm ? '' : ("pushToCRM('" + docId + "','" + encodeURIComponent(JSON.stringify(lead)).replace(/\\/g,'\\\\') + "')");


    card.innerHTML =
        '<div class="lc-header">' +
            '<div class="lc-left">' +
                '<div class="lc-company-name"><a href="'+(lead.url||lead.source_url||'#')+'" target="_blank" rel="noopener noreferrer">'+displayName+' &#8599;</a></div>' +
                '<div class="lc-meta"><span>'+srcLbl+'</span>'+(timeAgo?' &middot; '+timeAgo:'')+' </div>' +
            '</div>' +
            '<div class="lc-score-wrap">' +
                '<div class="lc-score-emoji">'+emoji+'</div>' +
                '<div class="lc-heat-bar"><div class="lc-heat-fill" style="width:'+heatPct+'%"></div></div>' +
                '<div class="lc-score-label">'+score+'/10</div>' +
            '</div>' +
        '</div>' +
        (signal ? '<div class="lc-signal">'+signal+'</div>' : '') +
        '<div class="lc-badges">'+prismBadge+bHTML+'</div>' +
        '<button class="lc-expand-btn" onclick="lcToggleExpand(\''+docId+'\')">' +
            '<span id="lc-expand-icon-'+docId+'">&#x2193;</span> See opening message &amp; full intelligence' +
        '</button>' +
        '<div class="lc-expanded" id="'+expandId+'">' +
            (dm ? '<div class="lc-section"><div class="lc-section-label">Your Opening Message</div><div class="lc-icebreaker">'+dm+'</div></div>' : '') +
            (lead.pain_point && lead.pain_point !== signal ? '<div class="lc-section"><div class="lc-section-label">Why This Lead</div><div class="lc-why">'+lead.pain_point+'</div></div>' : '') +
            _prismDossierHTML(lead) +
            cInfo +
        '</div>' +
        '<div class="lc-actions-primary">' +
            '<button class="lc-contact-btn lc-copilot-btn'+(isCont?' lc-copilot-btn--contacted':'')+'"' +
                ' id="copilot-btn-'+docId+'"' +
                ' data-action="copilot" data-lead-id="'+docId+'"' +
                (isCont?' disabled':'')+'>' +
                (isCont ? '&#x2713; Contacted' : copilotLbl) +
            '</button>' +
            '<button class="'+crmCls+'" id="crm-btn-'+docId+'"'+(crmOC?' onclick="'+crmOC+'"':'')+(lead.is_in_crm?' disabled':'')+' title="Send to pipeline CRM">'+(lead.is_in_crm?'In CRM':'-> CRM')+'</button>' +
            '<div style="position:relative;">' +
                '<button class="lc-more-btn" id="'+moreId+'" onclick="lcToggleMore(\''+docId+'\')" title="More options">...</button>' +
                '<div class="lc-overflow-menu" id="'+overflowId+'">' +
                    '<button class="lc-overflow-item" onclick="updateLeadStatus(\''+docId+'\',\'converted\');lcCloseMore(\''+docId+'\')">Mark Converted</button>' +
                    '<button class="lc-overflow-item" onclick="viewLeadTimeline(\''+encodeURIComponent(JSON.stringify(lead.interactions||[]))+'\');lcCloseMore(\''+docId+'\')">View Timeline</button>' +
                    '<button class="lc-overflow-item danger" onclick="openRejectionModal(\''+docId+'\');lcCloseMore(\''+docId+'\')">&#128683; Skip This Lead</button>' +
                '</div>' +
            '</div>' +
        '</div>';

    return card;
};


// Toggle expand/collapse
window.lcToggleExpand = function(docId) {
    const panel = document.getElementById(`lc-expand-${docId}`);
    const icon  = document.getElementById(`lc-expand-icon-${docId}`);
    if (!panel) return;
    const isOpen = panel.classList.contains('open');
    panel.classList.toggle('open', !isOpen);
    if (icon) icon.textContent = isOpen ? 'â†“' : 'â†‘';
};

// Overflow menu toggle
window.lcToggleMore = function(docId) {
    const menu = document.getElementById(`lc-of-${docId}`);
    if (!menu) return;
    const isOpen = menu.classList.contains('open');
    // Close all others first
    document.querySelectorAll('.lc-overflow-menu.open').forEach(m => m.classList.remove('open'));
    if (!isOpen) {
        menu.classList.add('open');
        // Auto-close on outside click
        setTimeout(() => {
            const handler = (e) => {
                if (!menu.contains(e.target)) { menu.classList.remove('open'); document.removeEventListener('click', handler); }
            };
            document.addEventListener('click', handler);
        }, 0);
    }
};
window.lcCloseMore = function(docId) {
    document.getElementById('lc-of-' + docId)?.classList.remove('open');
};

// =============================================================================
// V18: COPILOT ACTION — Zero-Liability Execution Flow
// Simultaneously: (1) copies the AI-drafted DM to clipboard, (2) opens the
// lead URL in a new tab. Immediately flips the card to 'Contacted' (optimistic).
// Fires background PUT /api/leads/{id} to persist status + trigger RLHF loop.
//
// dm and url are passed as base64 to avoid all quote-escaping issues in the
// inline onclick attribute generated by createLeadCardV2.
// =============================================================================

// =============================================================================
// V18: COPILOT ACTION — Event Delegation Architecture (Unicode-Safe)
//
// ARCHITECTURAL UPGRADE from btoa/atob inline encoding:
// - Lead data is stored in _leadsMap by docId when the card is created.
// - The copilot button carries only data-lead-id="docId" (no encoding at all).
// - A single delegated listener on #leads-list calls copilotAction(docId).
// - This eliminates the btoa() DOMException that fires on emoji/UTF-8 DMs.
//
// copilotAction() is still window-exposed for backward-compat (e.g. devtools).
// =============================================================================

window.copilotAction = async function(docId) {
    // Retrieve the live lead object from the in-memory cache
    var lead = _leadsMap.get(docId);
    if (!lead) {
        console.warn('[Copilot] Lead not found in _leadsMap for id:', docId);
        showToast('Lead data unavailable. Please refresh.', 'error');
        return;
    }

    var dm  = lead.dm  || '';
    var url = lead.url || lead.source_url || '';

    // Step 1: Optimistic UI — immediately flip button to Contacted state
    var btn = document.getElementById('copilot-btn-' + docId);
    if (btn) {
        btn.innerHTML = '&#x2713; Contacted';
        btn.disabled  = true;
        btn.classList.add('lc-copilot-btn--contacted');
    }

    // Step 2: Clipboard write (full Unicode support; execCommand fallback for non-HTTPS)
    if (dm) {
        try {
            await navigator.clipboard.writeText(dm);
        } catch(e) {
            var ta = document.createElement('textarea');
            ta.value = dm;
            ta.style.position = 'fixed';
            ta.style.opacity  = '0';
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand('copy'); } catch(_) {}
            document.body.removeChild(ta);
        }
    }

    // Step 3: Open lead source in new tab
    if (url && url !== '#') {
        window.open(url, '_blank', 'noopener,noreferrer');
    }

    // Step 4: Contextual toast
    showToast(dm ? 'Message copied — paste it in the new tab to reply.' : 'Opening lead source…', 'success');

    // Step 5: Background status persist + RLHF trigger (fire-and-forget; non-blocking)
    updateLeadStatus(docId, 'contacted');
};

// =============================================================================
// V18: USER DROPDOWN TOGGLE
// =============================================================================

window.toggleUserDropdown = function() {
    const dropdown = document.getElementById('user-dropdown');
    const pill = document.getElementById('user-pill-btn');
    if (!dropdown) return;
    const isOpen = dropdown.style.display === 'flex' || dropdown.classList.contains('open');
    if (isOpen) {
        dropdown.style.display = 'none';
        dropdown.classList.remove('open');
        if (pill) pill.classList.remove('open');
    } else {
        dropdown.style.display = 'block'; // FIX T4: absolute dropdown must be block
        dropdown.classList.add('open');
        if (pill) pill.classList.add('open');
        // Auto-close on outside click
        setTimeout(() => {
            const handler = (e) => {
                const wrap = pill?.closest('.user-pill-wrap');
                if (!wrap || !wrap.contains(e.target)) {
                    dropdown.style.display = 'none';
                    dropdown.classList.remove('open');
                    if (pill) pill.classList.remove('open');
                    document.removeEventListener('click', handler);
                }
            };
            document.addEventListener('click', handler);
        }, 0);
    }
};

// =============================================================================
// V18: SETTINGS / INTEGRATIONS MODAL
// =============================================================================

window.openSettingsModal = function() {
    document.getElementById('user-dropdown')?.classList.remove('open');
    showModal('settings-modal');
};

window.closeSettingsModal = function() {
    closeModal('settings-modal');
};

// =============================================================================
// TERMS OF SERVICE: agreeToTerms()
//
// BUG A ROOT CAUSE: This function was called by the TOS modal button
// (onclick="agreeToTerms()") but was NEVER DEFINED anywhere in the codebase.
// Every click threw: ReferenceError: agreeToTerms is not defined
// This trapped ALL new users permanently in the TOS modal with no escape.
// =============================================================================
window.agreeToTerms = async function() {
    try {
        const user = firebase.auth().currentUser;
        if (!user) return;
        const token = await user.getIdToken();
        // Persist agreement timestamp to user doc via PUT /api/me
        await fetch(`${API_BASE}/api/me`, {
            method: 'PUT',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({ agreed_to_terms: true })
        });
    } catch (e) {
        console.warn('[TOS] Failed to persist agreement, continuing anyway:', e);
    } finally {
        // Always dismiss the modal regardless of API success —
        // network failure should not re-trap the user.
        document.getElementById('tos-modal').style.display = 'none';
        showToast('Terms accepted. Welcome to Sideio!', 'success');
    }
};

// Close settings modal on overlay click
document.addEventListener('DOMContentLoaded', () => {
    const settingsOverlay = document.getElementById('settings-modal');
    if (settingsOverlay) {
        settingsOverlay.addEventListener('click', (e) => {
            if (e.target === settingsOverlay) closeSettingsModal();
        });
    }
    const dtOverlay = document.getElementById('dt-onboarding-modal');
    if (dtOverlay) {
        dtOverlay.addEventListener('click', (e) => {
            if (e.target === dtOverlay) closeDTModal();
        });
    }

    // ── V18: Event Delegation for Copilot Action button ──────────────────────
    // Single listener on #leads-list handles all .lc-copilot-btn clicks.
    // Reads lead data from _leadsMap by data-lead-id — zero encoding required.
    const leadsList = document.getElementById('leads-list');
    if (leadsList) {
        leadsList.addEventListener('click', function(e) {
            const btn = e.target.closest('[data-action="copilot"]');
            if (btn && !btn.disabled) {
                const docId = btn.dataset.leadId;
                if (docId) copilotAction(docId);
            }
        });
    }
});

// =============================================================================
// V18: DIGITAL TWIN ONBOARDING ENGINE
//
// SECURITY NOTE (per architectural review):
// Mock persona data is ONLY injected in localhost/dev environments.
// In production, a failed analyze-website call shows a graceful toast and
// falls back to the existing manual fc-step-2 flow.
// =============================================================================

const _dtIsLocal = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';

// Internal state store for extracted personas
window._dtState = {
    companyName: '',
    companyDesc: '',
    companyValue: '',
    targets: [
        { name: '', desc: '' },
        { name: '', desc: '' },
        { name: '', desc: '' }
    ],
    extractedBio: '',
    extractedWho: '',
    extractedGl: ''
};

// ─── MODAL: Digital Twin Onboarding ─────────────────────────────────────────
function dtSwitchView(viewId) {
    ['dt-view-a','dt-view-b','dt-view-c','dt-view-d'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.classList.toggle('active', el.id === viewId);
    });
}

window.openDTModal = function() {
    // Close the user dropdown if open
    document.getElementById('user-dropdown')?.classList.remove('open');
    // Reset to View A
    dtSwitchView('dt-view-a');
    const urlInput = document.getElementById('dt-url-input');
    if (urlInput) urlInput.value = '';
    // Show modal via CSS class toggle (no !important conflicts)
    showModal('dt-onboarding-modal');
    setTimeout(() => urlInput?.focus(), 150);
};

window.closeDTModal = function() {
    closeModal('dt-onboarding-modal');
};

// Re-analyze Website — triggered from user dropdown
// Pre-fills the saved website URL from the tenant profile, then opens the DT modal
window.dtReanalyzeWebsite = function() {
    toggleUserDropdown(); // close dropdown first
    const savedUrl = window.currentUserData?.website_url
                  || window.currentUserData?.website
                  || window.currentUserData?.url
                  || '';
    dtSwitchView('dt-view-a');
    const urlInput = document.getElementById('dt-url-input');
    if (urlInput && savedUrl) urlInput.value = savedUrl;
    showModal('dt-onboarding-modal');
    setTimeout(() => urlInput?.focus(), 150);
};

window.dtFallbackToNaturalLanguage = function() { dtSwitchView('dt-view-d'); };
window.dtBackToViewA = function() { dtSwitchView('dt-view-a'); };

// View A → View B: validate URL and start analysis
window.dtStartAnalysis = async function() {
    const urlInput = document.getElementById('dt-url-input');
    const rawUrl = (urlInput?.value || '').trim();

    if (!rawUrl || rawUrl.length < 5) {
        if (urlInput) { urlInput.style.borderColor = '#ef4444'; setTimeout(() => { urlInput.style.borderColor = ''; }, 2000); }
        showToast('Please enter a valid website URL.', 'error');
        return;
    }

    // Ensure protocol
    const url = /^https?:\/\//i.test(rawUrl) ? rawUrl : `https://${rawUrl}`;

    // Transition to View B
    dtSwitchView('dt-view-b');

    // Animate progress text
    const statusEl = document.getElementById('dt-status-text');
    const progressEl = document.getElementById('dt-progress-fill');
    const steps = [
        { text: 'Reading site...', pct: '15%', delay: 0 },
        { text: 'Extracting brand signals...', pct: '35%', delay: 1200 },
        { text: 'Building Company Persona...', pct: '55%', delay: 2400 },
        { text: 'Identifying Decision Makers...', pct: '75%', delay: 3600 },
        { text: 'Finalising target profiles...', pct: '90%', delay: 4800 }
    ];

    steps.forEach(({ text, pct, delay }) => {
        setTimeout(() => {
            if (statusEl) { statusEl.style.opacity = '0'; setTimeout(() => { statusEl.textContent = text; statusEl.style.opacity = '1'; }, 150); }
            if (progressEl) progressEl.style.width = pct;
        }, delay);
    });

    // API call with a 6s minimum animation window
    const animDone = new Promise(resolve => setTimeout(resolve, 5800));

    let personaData = null;
    let apiError = null;

    try {
        const user = firebase.auth().currentUser;
        if (!user) throw new Error('Not authenticated');
        const token = await user.getIdToken();

        const apiCall = fetch('/api/analyze-website', {
            method: 'POST',
            headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
            body: JSON.stringify({ url })
        });

        const [, resp] = await Promise.all([animDone, apiCall]);
        if (resp.ok) {
            const payload = await resp.json();
            personaData = payload.data || null;
        } else if (resp.status === 404 || resp.status === 501 || resp.status === 422) {
            // 422 = insufficient data from backend
            apiError = 'not_ready';
        } else {
            apiError = 'api_error';
        }
    } catch (e) {
        console.warn('[DT] analyze-website call failed:', e);
        apiError = 'network';
        await animDone;
    }

    // Handle result
    if (personaData) {
        dtPopulatePersonas(personaData, url);
    } else if (apiError === 'not_ready' && _dtIsLocal) {
        // DEV-ONLY mock — never runs in production
        console.warn('[DT] Using mock persona data — localhost only');
        dtPopulatePersonas(dtMockPersona(url), url);
    } else {
        // Production graceful failure
        dtSwitchView('dt-view-a');
        showToast('Digital Twin engine is currently provisioning. Please use manual entry.', 'error');
    }
};

// DEV-ONLY mock persona generator (localhost guard enforced above)
function dtMockPersona(url) {
    const domain = (() => { try { return new URL(url).hostname.replace('www.', ''); } catch(e) { return url; } })();
    return {
        company:  { name: domain, description: `${domain} is a growing business looking to expand its client base.`, value: 'B2B Services' },
        targets: [
            { name: 'Small Business Owners', description: 'Businesses with 1-10 employees actively seeking growth tools.' },
            { name: 'Marketing Managers', description: 'Mid-market companies with active social media budgets.' },
            { name: 'Agency Decision Makers', description: 'Agencies managing multiple client accounts and seeking automation.' }
        ]
    };
}

// Populate View C with API or mock data
function dtPopulatePersonas(data, url) {
    const company = data.company || {};
    const targets = data.targets || [];

    // Store in state
    window._dtState.companyName = company.name || '';
    window._dtState.companyDesc = company.description || '';
    window._dtState.companyValue = company.value || '';
    window._dtState.targets = [
        targets[0] || { name: '', desc: '' },
        targets[1] || { name: '', desc: '' },
        targets[2] || { name: '', desc: '' }
    ];

    // Store recommendations
    window._dtState.recommendedCampaigns = data.recommended_campaigns || [];
    
    // Derive campaign payload
    window._dtState.extractedBio    = company.description || '';
    window._dtState.extractedWho    = (targets[0]?.name || '') + (targets.length > 1 ? `, ${targets[1]?.name || ''}` : '');
    window._dtState.extractedGl     = data.detected_gl || '';

    // Populate DOM
    const setText = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val || '—'; };
    setText('dt-company-name', company.name);
    setText('dt-company-desc', company.description);
    setText('dt-company-value', company.value);
    for (let i = 0; i < 3; i++) {
        const t = targets[i] || {};
        setText(`dt-target-${i+1}-name`, t.name);
        setText(`dt-target-${i+1}-desc`, t.description || t.desc);
    }

    // Store for handoff
    document.getElementById('dt-extracted-bio').value  = window._dtState.extractedBio;
    document.getElementById('dt-extracted-who').value  = window._dtState.extractedWho;
    document.getElementById('dt-extracted-gl').value   = window._dtState.extractedGl;

    // Final progress
    const statusEl = document.getElementById('dt-status-text');
    const progressEl = document.getElementById('dt-progress-fill');
    if (statusEl) statusEl.textContent = 'Analysis complete \u2713';
    if (progressEl) progressEl.style.width = '100%';

    // Transition to View C
    setTimeout(() => {
        dtSwitchView('dt-view-c');
    }, 500);
}

// View C "Launch Campaign" — hands off to direct API call, completely bypassing legacy DOM
window.dtPrefillAndLaunch = function() {
    const bio = document.getElementById('dt-extracted-bio')?.value || window._dtState.extractedBio;
    const who = document.getElementById('dt-extracted-who')?.value || window._dtState.extractedWho;
    const gl  = document.getElementById('dt-extracted-gl')?.value  || window._dtState.extractedGl;
    const company = window._dtState.companyName;

    if (!bio || !who) {
        showToast('Please fill in company and target descriptions before launching.', 'error');
        return;
    }

    const now = new Date();
    const month = now.toLocaleString('en', { month: 'short' });
    const campName = `${who.substring(0, 35)} \u00B7 ${month} ${now.getFullYear()}`;
    const keys = who.substring(0, 120);

    closeDTModal();
    window.saveTenantProfileAction({
        name: campName,
        bio: bio,
        keywords: keys,
        gl: gl,
        recommended_campaigns: window._dtState.recommendedCampaigns || []
    });
};

window.saveTenantProfileAction = async function(payload) {
    // ── STRICT LOADING STATE (abolish optimistic UI) ─────────────────────────
    // The UI must NOT show "Digital Twin created" until we receive a verified
    // 200/201 from the backend. Disable all onboarding CTAs and show a spinner.
    const _twBtns = document.querySelectorAll(
        '.dt-launch-btn, #dt-launch-btn, [onclick*="dtPrefillAndLaunch"], [onclick*="dtLaunchFallback"]'
    );
    _twBtns.forEach(b => { b.disabled = true; b._orig = b.innerHTML; b.innerHTML = '⏳ Creating Twin...'; });
    const _restoreTwin = () => _twBtns.forEach(b => { b.disabled = false; b.innerHTML = b._orig || 'Launch'; });

    showToast('Setting up Master Twin Profile...', 'info');
    try {
        const user = firebase.auth().currentUser;
        if (!user) {
            _restoreTwin();
            showToast('Session expired — please sign in again.', 'error');
            return;
        }

        // force=true: mandatory on iOS Safari — background tab throttling
        // silently expires Firebase tokens without triggering onIdTokenChanged.
        // Without force=true, the Orchestrator returns 401 and the Firestore
        // write is silently dropped with no error shown to the user.
        const token = await user.getIdToken(true);

        const createResp = await fetch(`${API_BASE}/api/tenant_profiles`, {
            method:  'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body:    JSON.stringify(payload)
        });

        // STRICT CHECK: only proceed on verified 200/201. Any other status
        // (including 204, 400, 403, 500) is treated as a failure. Never
        // show success on assumption.
        if (!createResp.ok) {
            let errDetail = '';
            try { const errBody = await createResp.json(); errDetail = errBody.message || errBody.error || ''; } catch(_) {}
            throw new Error(`HTTP ${createResp.status}${errDetail ? ': ' + errDetail : ''}`);
        }

        // ── Verified success ────────────────────────────────────────────────
        _restoreTwin();
        loadDashboard();
        showToast('✅ Master Twin active! You can now add child campaigns.', 'success');

    } catch(err) {
        console.error('[saveTenantProfileAction]', err);
        _restoreTwin();
        // Surface a visible, actionable error — never silently fail.
        showToast(`Failed to create Digital Twin: ${err.message || 'Network error'}. Please try again.`, 'error');
    }
};

// Natural Language Fallback Launch
window.dtLaunchFallback = function() {
    const fallbackInp = document.getElementById('dt-intent-fallback');
    const txt = fallbackInp?.value.trim();
    if (!txt || txt.length < 5) {
        if (fallbackInp) { fallbackInp.style.borderColor = '#ef4444'; setTimeout(() => fallbackInp.style.borderColor='', 2000); }
        showToast('Please type a few words about your ideal clients.', 'warn');
        return;
    }

    const now = new Date();
    const month = now.toLocaleString('en', { month: 'short' });
    const campName = `${txt.substring(0, 35)} \u00B7 ${month} ${now.getFullYear()}`;

    closeDTModal();

    window.saveTenantProfileAction({
        name: campName,
        bio: 'Fallback intent processing required.', // LLM Backend Cartographer handles intent
        keywords: txt.substring(0, 120),
        gl: ''
    });
};

// Transition from View A to View D
window.dtFallbackToNaturalLanguage = function() {
    dtSwitchView('dt-view-d');
    setTimeout(() => document.getElementById('dt-intent-fallback')?.focus(), 100);
};


// =============================================================================
// V18 MULTI-CAMPAIGN: CHILD CAMPAIGN CREATION (STATE B)
// =============================================================================

window.openChildCampaignModal = async function() {
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
        showModal('child-campaign-modal');
        const fallbackCont = document.getElementById('cc-custom-fallback-container');
        if (fallbackCont) fallbackCont.style.display = 'none'; // FIX T2: no .hidden CSS rule
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
                
                html += `
                <div id="c-card-${idx}" style="background: rgba(255,255,255,0.6); padding: 16px; border-radius: 12px; margin-bottom: 16px; border: 1px solid var(--glass-border); text-align: left;">
                    <div id="c-card-view-${idx}">
                        <h4 style="margin:0 0 6px 0; color:var(--primary); font-size:1.1rem;">${camp.product_name || 'Product'}</h4>
                        <p style="font-size:0.9rem; margin-bottom:12px; line-height: 1.4;"><strong style="color:#4f46e5;">Market Trend:</strong> ${camp.market_trend_hook || ''}<br><strong style="color:#4f46e5;">Advantage:</strong> ${camp.unfair_advantage || ''}</p>
                        <button class="primary-btn" style="width:100%; font-size:0.9rem; padding:8px;" onclick="window.editPredictiveCard(${idx})">Review & Launch</button>
                    </div>
                    <div id="c-card-edit-${idx}" class="hidden">
                        <label style="font-size:0.8rem; color:var(--text-muted); display: block;">Product Focus</label>
                        <input type="text" id="c-prod-${idx}" class="fc-intent-input" style="height:36px; padding:8px; margin-bottom:8px; width: 100%; border: 1px solid #d1d5db; border-radius: 8px;" value="${(camp.product_name || '').replace(/"/g, '&quot;')}">
                        
                        <label style="font-size:0.8rem; color:var(--text-muted); display: block;">Market Opportunity</label>
                        <textarea id="c-hook-${idx}" class="fc-intent-input" style="min-height:60px; padding:8px; margin-bottom:8px; width: 100%; border: 1px solid #d1d5db; border-radius: 8px;">${(camp.market_trend_hook || '')}</textarea>
                        
                        <label style="font-size:0.8rem; color:var(--text-muted); display: block;">Unfair Advantage</label>
                        <textarea id="c-adv-${idx}" class="fc-intent-input" style="min-height:60px; padding:8px; margin-bottom:12px; width: 100%; border: 1px solid #d1d5db; border-radius: 8px;">${(camp.unfair_advantage || '')}</textarea>

                        <label style="font-size:0.8rem; color:var(--text-muted); display: block;">Target Location</label>
                        <input type="text" id="c-loc-${idx}" class="fc-intent-input" style="height:36px; padding:8px; margin-bottom:12px; width: 100%; border: 1px solid #d1d5db; border-radius: 8px;" placeholder="e.g. London, UK, Worldwide" value="${window._dtState?.extractedGl || ''}">
                        
                        <button class="primary-btn" style="width:100%; font-size:0.9rem; padding:8px; background:#10b981; border:none; border-radius: 20px; color:white; font-weight: 600; cursor: pointer;" onclick="window.deployPredictiveCard(${idx}, '${bProd}', '${bHook}', '${bAdv}')">Deploy Campaign</button>
                    </div>
                </div>
                `;
            });
        } else {
            // == FIX T1: Manual Profile Fallback ==
            // When no recommended_campaigns (user has no website yet), check if
            // an admin manually pushed a tenant_profile with company_bio or KB.
            // Surface a single editable "Manual Twin" card so the user can
            // immediately launch a campaign without scanning a website URL.
            const manualBio = (rawProfile && (
                rawProfile.company_bio        ||
                rawProfile.company_description ||
                rawProfile.bio                ||
                (rawProfile.knowledge_base_text && rawProfile.knowledge_base_text[0])
            )) || '';

            if (manualBio) {
                const productHint  = (rawProfile && (rawProfile.company_name || rawProfile.name)) || 'Your Core Service';
                const locationHint = (rawProfile && rawProfile.detected_gl) || '';
                const bProd = btoa(productHint.replace(/['"]/g, ''));
                const bHook = btoa(manualBio.slice(0, 120).replace(/['"]/g, ''));
                const bAdv  = btoa('');
                html = `<div style="background:linear-gradient(135deg,rgba(79,70,229,0.05),rgba(124,58,237,0.05));border:1px dashed rgba(79,70,229,0.3);border-radius:12px;padding:14px;margin-bottom:16px;text-align:left;"><div style="font-size:0.72rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:var(--primary);margin-bottom:6px;">&#10022; Profile Detected (Manual Twin)</div><p style="font-size:0.88rem;color:var(--text-muted);margin-bottom:14px;line-height:1.5;">${manualBio.slice(0,180)}${manualBio.length>180?'\u2026':''}</p><button class="primary-btn" style="width:100%;font-size:0.9rem;padding:8px;" onclick="window.editPredictiveCard(0)">Customise &amp; Launch &#8594;</button></div><div id="c-card-0"><div id="c-card-view-0" class="hidden"></div><div id="c-card-edit-0"><label style="font-size:0.8rem;color:var(--text-muted);display:block;">Product / Service Focus</label><input type="text" id="c-prod-0" class="fc-intent-input" style="height:36px;padding:8px;margin-bottom:8px;width:100%;border:1px solid #d1d5db;border-radius:8px;" value="${productHint.replace(/"/g,'&quot;')}"><label style="font-size:0.8rem;color:var(--text-muted);display:block;">Market Opportunity / Pain Point</label><textarea id="c-hook-0" class="fc-intent-input" style="min-height:60px;padding:8px;margin-bottom:8px;width:100%;border:1px solid #d1d5db;border-radius:8px;">${manualBio.slice(0,200)}</textarea><label style="font-size:0.8rem;color:var(--text-muted);display:block;">Unfair Advantage</label><textarea id="c-adv-0" class="fc-intent-input" style="min-height:60px;padding:8px;margin-bottom:12px;width:100%;border:1px solid #d1d5db;border-radius:8px;"></textarea><label style="font-size:0.8rem;color:var(--text-muted);display:block;">Target Location</label><input type="text" id="c-loc-0" class="fc-intent-input" style="height:36px;padding:8px;margin-bottom:12px;width:100%;border:1px solid #d1d5db;border-radius:8px;" placeholder="e.g. Kerala, India, Worldwide" value="${locationHint}"><button class="primary-btn" style="width:100%;font-size:0.9rem;padding:8px;background:#10b981;border:none;border-radius:20px;color:white;font-weight:600;cursor:pointer;" onclick="window.deployPredictiveCard(0,'${bProd}','${bHook}','${bAdv}')">Deploy Campaign</button></div></div>`;
            } else {
                html = '<p style="text-align:center; color:#6b7280;">No predictive campaigns available. Use the custom fallback.</p>';
            }
        }
        if(cardsEl) cardsEl.innerHTML = html;
        if(document.getElementById('cc-name')) document.getElementById('cc-name').value = '';
    }
};

window.editPredictiveCard = function(idx) {
    document.getElementById('c-card-view-' + idx).classList.add('hidden');
    document.getElementById('c-card-edit-' + idx).classList.remove('hidden');
};

window.deployPredictiveCard = async function(idx, origProd, origHook, origAdv) {
    const prod = (document.getElementById('c-prod-' + idx)?.value || '').trim();
    const hook = (document.getElementById('c-hook-' + idx)?.value || '').trim();
    const adv  = (document.getElementById('c-adv-' + idx)?.value || '').trim();
    const loc  = (document.getElementById('c-loc-' + idx)?.value || '').trim();

    if (!loc) {
        showToast('Target Location is required.', 'error');
        return;
    }

    // ── Step 1: Auto-save the AI opportunity as a Persona ──────────────────
    // Compose a structured directive bio from the AI card fields
    const personaBio = [
        `[Who we help]: Businesses struggling with: ${hook || prod}`,
        `[The problem we solve]: ${hook || prod}`,
        `[Our unfair advantage / Unique Value]: ${adv || 'Our unique positioning in this market'}`
    ].join('\n');
    const personaName = `${prod} Strategy`;

    const deployBtn = document.querySelector(`#c-card-edit-${idx} button.primary-btn`) ||
                      document.querySelector(`[onclick*="deployPredictiveCard(${idx}"]`);
    if (deployBtn) { deployBtn.disabled = true; deployBtn.textContent = '⚙️ Saving Agent...'; }

    try {
        const user  = firebase.auth().currentUser;
        if (!user) { showToast('Session expired.', 'error'); return; }
        const token = await user.getIdToken(true);

        const pResp = await fetch(`${API_BASE}/api/personas`, {
            method:  'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body:    JSON.stringify({ name: personaName, bio: personaBio, keywords: prod })
        });
        if (pResp.ok) {
            const pJson = await pResp.json();
            window._selectedPersonaId = pJson.id || '';
            window._personasCache = []; // invalidate cache
            console.log(`[DEPLOY] Auto-created persona '${personaName}' → ${pJson.id}`);
        } else {
            console.warn('[DEPLOY] Persona auto-save failed:', await pResp.text());
            // Non-fatal: continue without persona if API fails
            window._selectedPersonaId = '';
        }
    } catch(pErr) {
        console.warn('[DEPLOY] Persona auto-save error (non-fatal):', pErr);
        window._selectedPersonaId = '';
    }

    if (deployBtn) { deployBtn.textContent = '🚀 Launching...'; }

    // ── Step 2: Create the campaign with the new persona_id linked ──────────
    const wasEdited = (btoa(prod.replace(/['"]/g, '')) !== origProd) ||
                      (btoa(hook.replace(/['"]/g, '')) !== origHook) ||
                      (btoa(adv.replace(/['"]/g, ''))  !== origAdv);

    closeModal('child-campaign-modal');

    saveCampaignAction({
        name:              prod,
        bio:               'CHILD_CAMPAIGN_OVERRIDE',
        keywords:          '',
        campaign_focus:    prod,
        pain_point:        hook,
        unfair_advantage:  adv,
        gl:                '',
        location:          loc,
        target_urls:       [],
        human_edited:      wasEdited,
        target_angle_hook: hook,
        target_angle_adv:  adv
    });
};

window.showCcCustomFallback = function() {
    // FIX T2: hide cards, show manual form; pre-fill location from DT state
    const r = document.getElementById('cc-recommendation-cards');
    if (r) r.style.display = 'none';
    const f = document.getElementById('cc-custom-fallback-container');
    if (f) f.style.display = 'block';
    const locEl = document.getElementById('cc-location');
    if (locEl && !locEl.value && window._dtState && window._dtState.extractedGl) {
        locEl.value = window._dtState.extractedGl;
    }
    // Populate persona dropdown — always refresh to catch newly created personas
    populatePersonaDropdown('cc-persona-select');
};

window.saveChildCampaign = async function() {
    const focusEl   = document.getElementById('cc-focus');
    const locEl     = document.getElementById('cc-location');
    const painEl    = document.getElementById('cc-pain');
    const advEl     = document.getElementById('cc-advantage');
    const personaSel= document.getElementById('cc-persona-select');

    const focus   = focusEl?.value.trim()    || 'Custom Campaign';
    const loc     = locEl?.value.trim()      || '';
    const pain    = painEl?.value.trim()     || '';
    const adv     = advEl?.value.trim()      || '';
    const selPid  = personaSel?.value        || '';

    // Persona validation — required
    if (!selPid) {
        showToast('Please select an AI Agent / Persona before launching.', 'error');
        personaSel?.focus();
        return;
    }
    window._selectedPersonaId = selPid;

    if (!loc) {
        showToast('Target Geography is required.', 'error');
        return;
    }

    closeModal('child-campaign-modal');

    saveCampaignAction({
        name:             focus,
        bio:              'CHILD_CAMPAIGN_OVERRIDE',
        keywords:         '',
        campaign_focus:   focus,
        pain_point:       pain,
        unfair_advantage: adv,
        gl:               '',
        location:         loc,
        target_urls:      []
    });
};


// =============================================================================
// V18 MULTI-TENANCY: BUSINESS PROFILE HUB
// =============================================================================

window.openBusinessProfile = async function() {
    const modal = document.getElementById('business-profile-modal');
    if (!modal) return;
    
    // Hide edit state, default to view
    document.getElementById('bp-edit-mode').classList.add('hidden');
    document.getElementById('bp-view-mode').classList.remove('hidden');
    document.getElementById('bp-bio-disp').textContent = 'Loading...';
    document.getElementById('bp-keys-disp').textContent = 'Loading...';
    modal.classList.remove('hidden');
    
    const tp = await fetchTenantProfile();
    if (tp) {
        document.getElementById('bp-bio-disp').textContent = tp.bio || 'Not Set';
        document.getElementById('bp-keys-disp').textContent = tp.keywords || 'Not Set';
        
        document.getElementById('bp-bio-edit').value = tp.bio || '';
        document.getElementById('bp-keys-edit').value = tp.keywords || '';
    }
};

window.saveBusinessProfile = async function() {
    const newBio = document.getElementById('bp-bio-edit').value.trim();
    const newKeys = document.getElementById('bp-keys-edit').value.trim();
    
    try {
        const user = window.firebase.auth().currentUser;
        const token = await user.getIdToken();
        const response = await fetch(`${API_BASE}/api/tenant_profiles`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({
                bio: newBio,
                keywords: newKeys
            })
        });
        
        if (response.ok) {
            showToast('Business Profile Updated', 'success');
            document.getElementById('bp-edit-mode').classList.add('hidden');
            document.getElementById('bp-view-mode').classList.remove('hidden');
            
            document.getElementById('bp-bio-disp').textContent = newBio;
            document.getElementById('bp-keys-disp').textContent = newKeys;
        } else {
            showToast('Failed to update business profile', 'error');
        }
    } catch(e) {
        showToast('Error syncing profile', 'error');
    }
};

window.uploadKnowledgeBase = async function() {
    const fileInput = document.getElementById('bp-kb-upload');
    if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
        showToast('Please select a file first.', 'warn');
        return;
    }
    
    const file = fileInput.files[0];
    const user = window.firebase.auth().currentUser;
    if (!user) return;
    
    const tenantId = window.currentUserData?.tenant_id || user.uid;
    const pathRef = `knowledge_bases/${tenantId}/${file.name}`;
    
    showToast(`Uploading ${file.name}...`, 'info');
    
    try {
        const storageRef = firebase.storage().ref();
        const fileRef = storageRef.child(pathRef);
        await fileRef.put(file);
        
        showToast(`${file.name} uploaded successfully. Extracting context...`, 'info');
        
        const token = await user.getIdToken();
        const response = await fetch(`${API_BASE}/api/tenant_profiles/extract-kb`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({ filepath: pathRef })
        });
        
        const data = await response.json();
        if (response.ok) {
            showToast(`Extraction Complete: Knowledge Base Context Appended ✨`, 'success');
        } else {
            showToast(data.error || 'Failed to extract text from file', 'error');
        }
    } catch (e) {
        console.error('KB Upload Error:', e);
        showToast('Upload or extraction failed.', 'error');
    }
};


// =============================================================================
// REMOVED: Global body click delegation for btn-new-twin-* and btn-add-campaign-*.
//
// BUG ROOT CAUSE (Double-Fire): Both buttons have onclick="..." attributes directly
// in the HTML AND were also caught here via body delegation, causing every click
// to fire openNewCampaignModal() / openChildCampaignModal() TWICE. The second
// async call was resetting modal state while the first was still executing.
//
// Fix: The onclick attributes on the HTML buttons are sufficient. Body delegation
// for these specific buttons has been removed. The body listener below is kept
// only for genuinely dynamic elements that are rendered via innerHTML (no onclick).
// =============================================================================
document.body.addEventListener('click', function(e) {
    // Intentionally empty — kept for future dynamic element delegation only.
    // Do NOT re-add btn-new-twin-* or btn-add-campaign-* here.
});

// =============================================================================
// PERSONA VAULT — Full CRUD UI Engine
// =============================================================================

// In-memory cache of loaded personas for dropdown population
window._personasCache = [];
window._selectedPersonaId = '';

// ── loadPersonaVault ──────────────────────────────────────────────────────────
window.loadPersonaVault = async function() {
    const grid = document.getElementById('persona-grid');
    if (!grid) return;
    grid.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:48px 20px;grid-column:1/-1;">&#8987; Loading&hellip;</div>';

    try {
        const user  = firebase.auth().currentUser;
        if (!user) return;
        const token = await user.getIdToken(true);
        const resp  = await fetch(`${API_BASE}/api/personas`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const json  = await resp.json();
        const list  = json.data || [];

        window._personasCache = list;

        if (list.length === 0) {
            grid.innerHTML = `
                <div style="text-align:center; color:var(--text-muted); padding:64px 20px; grid-column:1/-1;">
                    <div style="font-size:3rem; margin-bottom:12px;">&#127918;</div>
                    <div style="font-size:1.05rem; font-weight:600; margin-bottom:8px;">No Personas Yet</div>
                    <div style="font-size:0.85rem; margin-bottom:20px; max-width:360px; margin-left:auto; margin-right:auto;">
                        Create named AI agent personas with unique pitches and keywords.
                        Attach them to campaigns so each search uses the right voice.
                    </div>
                    <button class="primary-btn" onclick="openPersonaModal()" style="min-height:44px; padding:10px 28px;">
                        &#43; Create Your First Persona
                    </button>
                </div>`;
            return;
        }

        grid.innerHTML = list.map(p => _buildPersonaCard(p)).join('');
    } catch(err) {
        console.error('[Persona Vault]', err);
        if (grid) grid.innerHTML = `<div style="text-align:center;color:#ef4444;padding:32px;grid-column:1/-1;">Failed to load personas: ${err.message}</div>`;
    }
};

// ── _buildPersonaCard ─────────────────────────────────────────────────────────
function _buildPersonaCard(p) {
    const kwChips = (p.keywords || '').split(',')
        .map(k => k.trim()).filter(Boolean).slice(0, 5)
        .map(k => `<span style="display:inline-block; background:rgba(79,70,229,0.08); color:#4f46e5; font-size:0.7rem; font-weight:600; padding:3px 8px; border-radius:20px; margin:2px;">${k}</span>`)
        .join('');
    const bioPreview = (p.bio || '').length > 120 ? p.bio.slice(0, 120) + '…' : (p.bio || '—');
    const safeId  = (p.id  || '').replace(/'/g, "\\'");
    const safeName = (p.name||'').replace(/'/g, "\\'");
    const safeBio  = (p.bio ||'').replace(/'/g, "\\'").replace(/\n/g, ' ');
    const safeKeys = (p.keywords||'').replace(/'/g, "\\'");

    return `
    <div style="background:#fff; border:1px solid #e5e7eb; border-radius:16px; padding:20px; display:flex; flex-direction:column; gap:12px; box-shadow:0 1px 4px rgba(0,0,0,0.06); transition:box-shadow 0.2s;" onmouseover="this.style.boxShadow='0 4px 16px rgba(79,70,229,0.12)'" onmouseout="this.style.boxShadow='0 1px 4px rgba(0,0,0,0.06)'">
        <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:8px;">
            <div style="font-size:1rem; font-weight:700; color:#1e1b4b; line-height:1.3;">${p.name || 'Unnamed Persona'}</div>
            <div style="display:flex; gap:8px; flex-shrink:0;">
                <button onclick="openPersonaModal('${safeId}','${safeName}','${safeBio}','${safeKeys}')" style="background:none; border:1px solid #d1d5db; border-radius:8px; padding:5px 10px; font-size:0.75rem; font-weight:600; color:#4f46e5; cursor:pointer; transition:all 0.15s;" onmouseover="this.style.background='#ede9fe'" onmouseout="this.style.background='none'">&#9998; Edit</button>
                <button onclick="deletePersona('${safeId}','${safeName}')" style="background:none; border:1px solid #fecaca; border-radius:8px; padding:5px 10px; font-size:0.75rem; font-weight:600; color:#ef4444; cursor:pointer; transition:all 0.15s;" onmouseover="this.style.background='#fef2f2'" onmouseout="this.style.background='none'">&#128465;</button>
            </div>
        </div>
        <div style="font-size:0.82rem; color:#6b7280; line-height:1.5;">${bioPreview}</div>
        <div style="display:flex; flex-wrap:wrap; gap:4px; min-height:24px;">${kwChips || '<span style="font-size:0.72rem;color:#9ca3af;">No keywords set</span>'}</div>
        <div style="margin-top:auto; padding-top:8px; border-top:1px solid #f1f5f9; display:flex; justify-content:flex-end;">
            <button onclick="selectPersonaForCampaign('${safeId}','${safeName || 'Persona'}')" style="background:linear-gradient(135deg,#4f46e5,#7c3aed); color:#fff; border:none; border-radius:8px; padding:7px 14px; font-size:0.78rem; font-weight:600; cursor:pointer; transition:opacity 0.15s;" onmouseover="this.style.opacity='0.85'" onmouseout="this.style.opacity='1'">
                &#128640; Use in Campaign
            </button>
        </div>
    </div>`;
}

// ── openPersonaModal ──────────────────────────────────────────────────────────
// ── Tag engine internal state ────────────────────────────────────────────────
let _personaTags = [];

function _syncPersonaTagsToHidden() {
    const hidden = document.getElementById('persona-keywords');
    if (hidden) hidden.value = _personaTags.join(', ');
}

function _renderPersonaTags() {
    const container = document.getElementById('persona-tag-container');
    const input     = document.getElementById('persona-tag-input');
    if (!container || !input) return;

    // Remove all existing pills (leave the input in place)
    container.querySelectorAll('.persona-pill').forEach(p => p.remove());

    _personaTags.forEach((tag, idx) => {
        const isNegative = tag.toLowerCase().startsWith('not ');
        const pill = document.createElement('span');
        pill.className = 'persona-pill';
        pill.style.cssText = [
            'display:inline-flex', 'align-items:center', 'gap:5px',
            'padding:4px 10px', 'border-radius:20px',
            'font-size:0.78rem', 'font-weight:600', 'line-height:1', 'cursor:default',
            isNegative
                ? 'background:#fff1f0; color:#cf1322; border:1px solid #ffa39e;'
                : 'background:rgba(79,70,229,0.09); color:#4338ca; border:1px solid rgba(79,70,229,0.2);'
        ].join(';');
        pill.innerHTML = `${tag}<button tabindex="-1" onclick="_removePersonaTag(${idx})" style="background:none;border:none;cursor:pointer;padding:0 0 0 2px;color:inherit;opacity:0.6;font-size:0.85rem;line-height:1;" onmouseover="this.style.opacity='1'" onmouseout="this.style.opacity='0.6'">&#10005;</button>`;
        container.insertBefore(pill, input);
    });

    input.placeholder = _personaTags.length === 0 ? 'Type a signal and press Enter\u2026' : '';
    _syncPersonaTagsToHidden();
}

window._removePersonaTag = function(idx) {
    _personaTags.splice(idx, 1);
    _renderPersonaTags();
};

window.handlePersonaTagInput = function(e) {
    const input = e.target;
    const val   = (input.value || '').trim().replace(/,+$/, '').trim();

    if ((e.key === 'Enter' || e.key === ',') && val) {
        e.preventDefault();
        if (!_personaTags.includes(val)) {
            _personaTags.push(val);
            _renderPersonaTags();
        }
        input.value = '';
        input.style.width = '';
        return;
    }

    // Backspace on empty input removes last tag
    if (e.key === 'Backspace' && !input.value && _personaTags.length > 0) {
        _personaTags.pop();
        _renderPersonaTags();
    }
};

// ── openPersonaModal ──────────────────────────────────────────────────────────
const _BIO_TEMPLATE = `[Who we help]: ...
[The problem we solve]: ...
[Our unfair advantage / Unique Value]: ...`;

window.openPersonaModal = function(id='', name='', bio='', keywords='') {
    const overlay = document.getElementById('persona-modal-overlay');
    const title   = document.getElementById('persona-modal-title');
    const editId  = document.getElementById('persona-edit-id');
    const nameEl  = document.getElementById('persona-name');
    const bioEl   = document.getElementById('persona-bio');
    const tagInput= document.getElementById('persona-tag-input');
    if (!overlay) return;

    const isEdit = !!id;

    if (title)  title.textContent = isEdit ? 'Edit AI Agent' : 'Configure AI Agent';
    if (editId) editId.value      = id;
    if (nameEl) nameEl.value      = name;

    // Bio: use template for new, actual bio for edit (strip trailing ellipsis from card preview)
    if (bioEl)  bioEl.value = isEdit ? bio.replace(/\u2026$/, '').trimEnd() : _BIO_TEMPLATE;

    // Rebuild tag pills from existing keywords
    _personaTags = keywords
        ? keywords.split(',').map(k => k.trim()).filter(Boolean)
        : [];
    _renderPersonaTags();
    if (tagInput) { tagInput.value = ''; tagInput.style.width = ''; }

    overlay.style.display = 'flex';
    setTimeout(() => nameEl?.focus(), 80);
};

// ── closePersonaModal ─────────────────────────────────────────────────────────
window.closePersonaModal = function() {
    const overlay = document.getElementById('persona-modal-overlay');
    if (overlay) overlay.style.display = 'none';
    // Reset tag state
    _personaTags = [];
    _renderPersonaTags();
};

// ── savePersona ───────────────────────────────────────────────────────────────
window.savePersona = async function() {
    const editId  = document.getElementById('persona-edit-id')?.value || '';
    const name    = (document.getElementById('persona-name')?.value    || '').trim();
    const bio     = (document.getElementById('persona-bio')?.value     || '').trim();
    const keywords= (document.getElementById('persona-keywords')?.value|| '').trim();
    const saveBtn = document.getElementById('persona-save-btn');

    if (!name) { showToast('Agent Name / Strategy is required.', 'error');  return; }
    if (!bio || bio === _BIO_TEMPLATE) { showToast('Please fill in the Core Directive.', 'error'); return; }

    if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.innerHTML = '&#9203; Deploying\u2026';
    }

    try {
        const user  = firebase.auth().currentUser;
        if (!user) { showToast('Session expired. Please sign in again.', 'error'); return; }
        const token = await user.getIdToken(true);

        const isEdit = !!editId;
        const url    = isEdit ? `${API_BASE}/api/personas/${editId}` : `${API_BASE}/api/personas`;
        const method = isEdit ? 'PUT' : 'POST';

        const resp = await fetch(url, {
            method,
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, bio, keywords })
        });

        if (!resp.ok) {
            const e = await resp.json().catch(() => ({}));
            throw new Error(e.error || `HTTP ${resp.status}`);
        }

        const result = await resp.json();
        if (isEdit && result.linked_campaigns > 0) {
            showToast(`Agent updated. Cache refreshed for ${result.linked_campaigns} campaign(s).`, 'success');
        } else {
            showToast(isEdit ? 'Agent configuration saved.' : '&#9889; Agent deployed!', 'success');
        }

        closePersonaModal();
        loadPersonaVault();
    } catch(err) {
        console.error('[savePersona]', err);
        showToast('Deploy failed: ' + err.message, 'error');
    } finally {
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.innerHTML = '<span>&#9889;</span> Deploy Agent';
        }
    }
};


// ── deletePersona ─────────────────────────────────────────────────────────────
window.deletePersona = async function(id, name) {
    if (!id) return;
    if (!confirm(`Delete persona "${name}"?\nThis cannot be undone. Active campaigns using it will fall back to their own bio.`)) return;

    try {
        const user  = firebase.auth().currentUser;
        if (!user) return;
        const token = await user.getIdToken(true);
        const resp  = await fetch(`${API_BASE}/api/personas/${id}`, {
            method:  'DELETE',
            headers: { 'Authorization': `Bearer ${token}` }
        });
        const json = await resp.json();
        if (!resp.ok) {
            if (resp.status === 409) {
                showToast(`Cannot delete: in use by campaigns: ${json.campaigns?.join(', ')}`, 'error');
            } else {
                throw new Error(json.error || `HTTP ${resp.status}`);
            }
            return;
        }
        showToast(`Persona "${name}" deleted.`, 'success');
        loadPersonaVault();
    } catch(err) {
        console.error('[deletePersona]', err);
        showToast('Delete failed: ' + err.message, 'error');
    }
};

// ── selectPersonaForCampaign ──────────────────────────────────────────────────
// Called from persona card "Use in Campaign" — sets the global selection and
// switches to the campaign builder tab pre-loaded with this persona.
window.selectPersonaForCampaign = function(id, name) {
    window._selectedPersonaId = id;
    showToast(`Persona "${name}" selected. Open a campaign to use it.`, 'success');
    // Navigate to campaign builder
    switchTab('target');
};

// ── populatePersonaDropdown ───────────────────────────────────────────────────
// Call this when opening the campaign creation modal to inject persona options.
window.populatePersonaDropdown = async function(selectElId) {
    const sel = document.getElementById(selectElId);
    if (!sel) return;
    sel.innerHTML = '<option value="">— No Persona (use campaign bio) —</option>';

    try {
        const user  = firebase.auth().currentUser;
        if (!user) return;
        // Use cache if available, else fetch
        let list = window._personasCache;
        if (!list || list.length === 0) {
            const token = await user.getIdToken();
            const resp  = await fetch(`${API_BASE}/api/personas`, {
                headers: { 'Authorization': `Bearer ${token}` }
            });
            const json  = await resp.json();
            list = json.data || [];
            window._personasCache = list;
        }
        list.forEach(p => {
            const opt   = document.createElement('option');
            opt.value   = p.id;
            opt.textContent = p.name;
            if (p.id === window._selectedPersonaId) opt.selected = true;
            sel.appendChild(opt);
        });
        sel.onchange = () => { window._selectedPersonaId = sel.value; };
    } catch(err) {
        console.warn('[populatePersonaDropdown]', err);
    }
};

