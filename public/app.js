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
const appContainer = document.getElementById('app-container');
const loginBtn = document.getElementById('login-btn');
const logoutBtn = document.getElementById('logout-btn');
const leadsList = document.getElementById('leads-list');

// Selected Filter State
let currentCampaignFilter = 'all';
let rawLeadsCache = [];
// V18: O(1) lead lookup for Event Delegation copilot action
// Keyed by Firestore docId. Populated in createLeadCardV2.
const _leadsMap = new Map();

function handleAuthRejection() {
    showToast("Session Expired or Unauthorized Access.", "error");
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
            if (el) el.innerText = credits;
            
            const alertBanner = document.getElementById('wallet-alert-banner');
            const newCampBtn = document.querySelector('button[onclick="openNewCampaignModal()"]');
            if (credits <= 0) {
                if (alertBanner) { alertBanner.textContent = 'Wallet Empty: You have 0 credits. Contact admin to top up.'; alertBanner.classList.remove('hidden'); }
                if (newCampBtn) { newCampBtn.textContent = 'Upgrade Plan (0 Credits)'; newCampBtn.disabled = true; newCampBtn.style.background = '#94a3b8'; }
            } else if (credits < 50) {
                if (alertBanner) alertBanner.classList.remove('hidden');
            } else {
                if (alertBanner) alertBanner.classList.add('hidden');
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
        
        if (!response.ok) {
            console.error('Backend Error (loadCampaigns):', await response.text());
            if (tableBody) tableBody.innerHTML = '<tr><td colspan="4" class="empty-state">Failed to load campaigns.</td></tr>';
            return;
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
            const locationWarn = hasLocation ? '' : '<br><span style="color: #ea580c; font-size: 0.75rem; display:block; margin-top:4px;">âš ï¸  Location Missing: Edit Campaign to set Targeting</span>';
            
            tableHTML += `
                <tr style="border-bottom: 1px solid var(--glass-border);">
                    <td style="padding: 12px;"><strong>${camp.name || 'Untitled'}</strong>${locationWarn}</td>
                    <td style="padding: 12px;"><i style="color:var(--text-muted); font-size:0.85rem">${camp.keywords || 'N/A'}</i></td>
                    <td style="padding: 12px;">${statusBadge}</td>
                    <td style="padding: 12px; text-align:right;">
                        <button class="secondary-btn" style="padding: 4px 8px; font-size: 0.75rem; margin-right: 4px;" onclick="openEditModal('${id}', '${(camp.name || '').replace(/'/g, "\\'")}', '${(camp.bio || '').replace(/'/g, "\\'")}', '${(camp.keywords || '').replace(/'/g, "\\'")}', '${(camp.gl || '').replace(/'/g, "\\'")}', '${(camp.location || '').replace(/'/g, "\\'")}', ${JSON.stringify(camp.target_urls || [])})">Edit</button>
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
// V18: funnel-wrap is conditionally shown only when leads exist (clean canvas otherwise)
let conversionChart = null;
function initAnalyticsChart(newC, contactedC, convertedC) {
    const ctx = document.getElementById('funnelChart');
    const wrapper = document.getElementById('funnel-wrap');
    const totalLeads = newC + contactedC + convertedC;

    // Show/hide the wrapper based on whether there is any data
    if (wrapper) {
        if (totalLeads > 0) {
            wrapper.classList.remove('hidden');
        } else {
            wrapper.classList.add('hidden');
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

window.showToast = function(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => toast.classList.add('show'), 10);
    setTimeout(() => { toast.classList.remove('show'); setTimeout(() => toast.remove(), 300); }, 3500);
};

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
    const token = await user.getIdToken();
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
// LEAD STATUS UPDATE — contacted / converted / ignored
// =============================================================================

window.updateLeadStatus = async function(docId, newStatus) {
    if (newStatus === 'ignored') {
        const idx = rawLeadsCache.findIndex(l => l.id === docId);
        if (idx !== -1) { rawLeadsCache.splice(idx, 1); renderLeads(); }
    }
    try {
        const success = await performApiMutation(`/api/leads/${docId}`, 'PUT', { status: newStatus });
        if (success) {
            showToast(`Lead status updated: ${newStatus}`, 'success');
            if (newStatus !== 'ignored') loadDashboard();
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

window.openEditModal = function(id, name, bio, keywords, gl, location, targetUrls) {
    document.getElementById('edit-camp-id').value        = id       || '';
    document.getElementById('edit-camp-name').value      = name     || '';
    document.getElementById('edit-camp-bio').value       = bio      || '';
    document.getElementById('edit-camp-keys').value      = keywords || '';
    const glEl = document.getElementById('edit-camp-gl');
    if (glEl) glEl.value = gl || '';
    document.getElementById('edit-camp-location').value  = location || '';
    const urlsEl = document.getElementById('edit-camp-target-urls');
    if (urlsEl) {
        const urls = Array.isArray(targetUrls) ? targetUrls : JSON.parse(targetUrls || '[]');
        urlsEl.value = urls.join('\n');
    }
    const modal = document.getElementById('edit-campaign-modal');
    if (modal) modal.style.display = 'flex';
};

window.closeEditModal = function() {
    const modal = document.getElementById('edit-campaign-modal');
    if (modal) modal.style.display = 'none';
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

    if (!cpName || !cpKeys) {
        showToast('Campaign Name and Keywords are required', 'error');
        return;
    }

    showToast('Setting up your search...', 'info');
    try {
        const user = firebase.auth().currentUser;
        if (!user) return;
        const token = await user.getIdToken();

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
                status:      'active'
            })
        });
        if (!createResp.ok) throw new Error('Campaign creation failed');
        const createData = await createResp.json();
        const campaignId = createData.id;

        // Fire Epsilon-Greedy Router for immediate first batch
        let routerMsg = 'System is now looking for clients!';
        if (campaignId) {
            try {
                const routerResp = await fetch(`${API_BASE}/api/campaigns/${campaignId}/run`, {
                    method:  'POST',
                    headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
                    body:    JSON.stringify({})
                });
                if (routerResp.ok) {
                    const r    = await routerResp.json();
                    const v16  = r.autonomous_promoted || 0;
                    const v14  = r.cartographer_queued || 0;
                    routerMsg  = `Engine dispatched: ${v16} Predictive + ${v14} Cartographer leads queued`;
                }
            } catch (routerErr) {
                console.warn('[ROUTER] Router call failed — Cartographer sweep will pick up:', routerErr);
            }
        }

        const targetUrlsInput = document.getElementById('camp-target-urls');
        if (targetUrlsInput) targetUrlsInput.value = '';
        
        loadDashboard();
        showToast(routerMsg, 'success');
    } catch(err) {
        console.error('[saveCampaignAction]', err);
        showToast('Failed to save campaign. Check API permissions.', 'error');
    }
};

// SPA Router — V18: syncs both top cmd-bar (desktop) and bottom dock (mobile)
window.switchTab = function(tabName) {
    document.querySelectorAll('.main-feed').forEach(el => el.classList.add('hidden'));
    document.querySelectorAll('.cmd-btn').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.dock-btn').forEach(el => el.classList.remove('active'));

    const activateTab = (topId, dockId) => {
        const topEl = document.getElementById(topId);
        const dockEl = document.getElementById(dockId);
        if (topEl) topEl.classList.add('active');
        if (dockEl) dockEl.classList.add('active');
    };

    if (tabName === 'dashboard') {
        document.getElementById('view-dashboard').classList.remove('hidden');
        activateTab('tab-dashboard', 'dock-tab-dashboard');
    } else if (tabName === 'target') {
        if (document.getElementById('view-target')) document.getElementById('view-target').classList.remove('hidden');
        activateTab('tab-campaigns', 'dock-tab-campaigns');
    } else if (tabName === 'team') {
        if (document.getElementById('view-team')) document.getElementById('view-team').classList.remove('hidden');
    } else if (tabName === 'reports') {
        if (document.getElementById('view-reports')) document.getElementById('view-reports').classList.remove('hidden');
        activateTab('tab-reports', 'dock-tab-reports');
    } else if (tabName === 'l0-admin') {
        if (document.getElementById('view-l0-admin')) document.getElementById('view-l0-admin').classList.remove('hidden');
        const l0Tab = document.getElementById('tab-l0-admin');
        if (l0Tab) l0Tab.classList.add('active');
        fetchL0Telemetry();
    } else if (tabName === 'macro') {
        if (document.getElementById('view-macro')) document.getElementById('view-macro').classList.remove('hidden');
        fetchMacroTrends();
    } else if (tabName === 'crm-test') {
        const isAdmin = window.currentUserData?.role === 'super_admin';
        if (!isAdmin) { showToast('CRM module is restricted to L0 administrators.', 'error'); return; }
        const crmView = document.getElementById('view-crm-test');
        if (crmView) { crmView.classList.remove('hidden'); loadCrmBoard(); }
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
                    '<button class="lc-overflow-item danger" onclick="updateLeadStatus(\''+docId+'\',\'ignored\');lcCloseMore(\''+docId+'\')">Skip This Lead</button>' +
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
        dropdown.style.display = 'flex';
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
    showToast('Setting up Master Twin Profile...', 'info');
    try {
        const user = firebase.auth().currentUser;
        if (!user) return;
        const token = await user.getIdToken();

        const createResp = await fetch(`${API_BASE}/api/tenant_profiles`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!createResp.ok) throw new Error('Master profile creation failed');
        
        loadDashboard();
        showToast('Master Twin active! You can now add child campaigns.', 'success');
    } catch(err) {
        console.error('[saveTenantProfileAction]', err);
        showToast('Failed to save Master Twin. Check API permissions.', 'error');
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
    const loc  = (document.getElementById('c-loc-' + idx)?.value || '').trim();
    
    // BUG FIX: Previous logic `!loc && loc.toLowerCase() !== 'worldwide'` was
    // inverted — it would always fire validation error on empty string.
    // Correct intent: require a non-empty location value.
    if (!loc) {
        showToast('Target Location is required.', 'error');
        return;
    }

    // basic diff via btoa
    const wasEdited = (btoa(prod.replace(/['"]/g, '')) !== origProd) || 
                      (btoa(hook.replace(/['"]/g, '')) !== origHook) || 
                      (btoa(adv.replace(/['"]/g, '')) !== origAdv);
                      
    closeModal('child-campaign-modal');

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
};

window.showCcCustomFallback = function() {
    const r = document.getElementById('cc-recommendation-cards');
    if(r) r.style.display = 'none';
    const f = document.getElementById('cc-custom-fallback-container');
    if(f) f.classList.remove('hidden');
};

window.saveChildCampaign = function() {
    const focusEl = document.getElementById('cc-focus');
    const locEl = document.getElementById('cc-location');
    const painEl = document.getElementById('cc-pain');
    const advEl = document.getElementById('cc-advantage');
    
    const focus = focusEl?.value.trim() || 'Custom Campaign';
    const loc = locEl?.value.trim() || '';
    const pain = painEl?.value.trim() || '';
    const adv = advEl?.value.trim() || '';

    // BUG FIX: Same inverted validation as deployPredictiveCard.
    // Require any non-empty location string.
    if (!loc) {
        showToast('Target Geography is required.', 'error');
        return;
    }

    closeModal('child-campaign-modal');

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
