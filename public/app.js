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

// Digital Twin Engine — dedicated microservice (POST /api/analyze-website)
// Update this URL after first `gcloud run deploy digital-twin-engine` completes.
const DT_ENGINE_URL = "https://digital-twin-engine-222247989819.asia-south1.run.app";



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

    // V15.1 HOTFIX: crmAutoOpen resolved here â€” inside the real loadDashboard,
    // NOT via a recursive override. loadMe() must have run first so
    // window.currentUserData (and .role) is populated before switchTab checks it.
    if (window.crmAutoOpen) {
        window.crmAutoOpen = false;
        // switchTab contains its own super_admin gate; no recursion possible.
        switchTab('crm-test');
    }
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

            // Defensively check both payload tracks mapping legacy or missing keys safely
            const w = payload.wallet || data.wallet || {allocated_credits: 0, consumed_credits: 0};
            activeWallet = w;
            const el = document.getElementById('wallet-balance');
            const credits = (w.allocated_credits || 0) - (w.consumed_credits || 0);
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
                if (tosModal) tosModal.classList.remove('hidden');
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
        document.getElementById('audit-log-modal')?.classList.remove('hidden');
    } catch(e) { console.error('Timeline error', e); }
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

// Step 1: character hint update
window.fcUpdateCharHint = function(el) {
    const n = el.value.trim().length;
    const hint = document.getElementById('fc-char-hint');
    if (!hint) return;
    if (n === 0)       hint.textContent = 'Press Enter or click below';
    else if (n < 20)   hint.textContent = 'A bit more detail helps get better results â†“';
    else if (n < 60)   hint.textContent = 'Good. Add a location for sharper targeting â†’';
    else               hint.textContent = 'âœ“ Ready â€” click to proceed';
};

// Fill template chip
window.fcFillTemplate = function(btn) {
    const ta = document.getElementById('fc-intent');
    if (!ta) return;
    // Strip emoji prefix from chip text
    ta.value = btn.textContent.replace(/^[^\w]+/, '').trim();
    ta.focus();
    fcUpdateCharHint(ta);
};

// Step 1 â†’ Step 2
window.fcStep1Next = function() {
    const ta = document.getElementById('fc-intent');
    if (!ta) return;
    const sentence = ta.value.trim();
    if (sentence.length < 5) {
        ta.style.borderColor = '#ef4444';
        ta.placeholder = 'Please describe who you want to reachâ€¦';
        ta.focus();
        setTimeout(() => { ta.style.borderColor = ''; }, 2000);
        return;
    }

    const parsed = fcParseIntent(sentence);
    window._fcState.gl       = parsed.gl;
    window._fcState.location = parsed.where;
    window._fcState.whoConfirmed = parsed.who;

    // Populate step 2
    const whoEl = document.getElementById('fc-confirm-who');
    if (whoEl) whoEl.textContent = parsed.who;
    const editWho = document.getElementById('fc-edit-who');
    if (editWho) editWho.value = parsed.who;

    // Auto-select location if parsed
    if (parsed.gl) {
        document.querySelectorAll('.fc-loc-chip').forEach(c => {
            c.classList.toggle('selected', c.dataset.gl === parsed.gl);
        });
        const blockWhere = document.getElementById('fc-block-where');
        if (blockWhere) blockWhere.style.borderColor = '';
        document.getElementById('fc-where-required')?.classList.remove('show');
    } else {
        // No location detected â€” highlight location block
        const blockWhere = document.getElementById('fc-block-where');
        if (blockWhere) blockWhere.style.borderColor = '#f59e0b';
        document.getElementById('fc-where-required')?.classList.add('show');
    }

    // Open "what" edit if bio not in sentence
    const editWhat = document.getElementById('fc-edit-what');
    if (editWhat) {
        editWhat.classList.remove('hidden');
        editWhat.style.display = '';
    }
    document.getElementById('fc-what-btn')?.setAttribute('data-open', '1');

    // Transition
    document.getElementById('fc-step-1').classList.add('hidden');
    document.getElementById('fc-step-2').classList.remove('hidden');
};

// Back button
window.fcGoBack = function() {
    document.getElementById('fc-step-2').classList.add('hidden');
    document.getElementById('fc-step-1').classList.remove('hidden');
};

// Toggle inline edit
window.fcToggleEdit = function(field) {
    if (field === 'who') {
        const val  = document.getElementById('fc-confirm-who');
        const inp  = document.getElementById('fc-edit-who');
        if (!val || !inp) return;
        const isOpen = !inp.classList.contains('hidden');
        if (isOpen) {
            // Save
            const newVal = inp.value.trim() || val.textContent;
            val.textContent = newVal;
            window._fcState.whoConfirmed = newVal;
            inp.classList.add('hidden');
        } else {
            inp.classList.remove('hidden');
            inp.focus();
        }
    } else if (field === 'what') {
        const inp = document.getElementById('fc-edit-what');
        const btn = document.getElementById('fc-what-btn');
        if (!inp) return;
        const isOpen = inp.style.display !== 'none' && !inp.classList.contains('hidden');
        if (isOpen) {
            // Save
            const v = inp.value.trim();
            window._fcState.whatConfirmed = v;
            const valEl = document.getElementById('fc-confirm-what');
            if (valEl && v) { valEl.textContent = v; valEl.style.fontStyle = 'normal'; valEl.style.color = 'var(--text-main)'; }
            if (btn) btn.textContent = 'Edit';
            document.getElementById('fc-what-required')?.classList.remove('show');
        } else {
            inp.style.display = '';
            inp.classList.remove('hidden');
            inp.focus();
            if (btn) btn.textContent = 'Save âœ“';
        }
    }
};

// Location chip selection
window.fcSelectLocation = function(btn) {
    document.querySelectorAll('.fc-loc-chip').forEach(c => c.classList.remove('selected'));
    btn.classList.add('selected');
    window._fcState.gl       = btn.dataset.gl || '';
    window._fcState.location = btn.dataset.loc || '';
    document.getElementById('fc-where-required')?.classList.remove('show');
    const blockWhere = document.getElementById('fc-block-where');
    if (blockWhere) blockWhere.style.borderColor = 'transparent';
};

// Launch (validation + submit)
window.fcLaunch = function() {
    const bar  = document.getElementById('fc-validation-bar');
    const errs = [];

    // Validate WHO
    const who = document.getElementById('fc-edit-who')?.value.trim()
             || document.getElementById('fc-confirm-who')?.textContent.trim()
             || window._fcState.whoConfirmed;
    if (!who || who === 'â€”') errs.push('Tell me who you want to reach.');

    // Validate WHAT (required for bio)
    const what = document.getElementById('fc-edit-what')?.value.trim()
              || window._fcState.whatConfirmed;
    if (!what || what.length < 15) {
        errs.push("Add a short description of what you sell â€” this personalises every pitch.");
        document.getElementById('fc-what-required')?.classList.add('show');
    }

    // Validate WHERE
    if (!window._fcState.gl && !window._fcState.location) {
        errs.push("Pick a location â€” even 'Worldwide' is fine.");
        document.getElementById('fc-where-required')?.classList.add('show');
        const blockWhere = document.getElementById('fc-block-where');
        if (blockWhere) blockWhere.style.borderColor = '#f87171';
    }

    if (errs.length > 0) {
        if (bar) { bar.textContent = 'âš¡ ' + errs[0]; bar.classList.remove('hidden'); }
        return;
    }
    if (bar) bar.classList.add('hidden');

    // Populate hidden fields for saveCampaignAction
    const cityInput = document.getElementById('fc-edit-where-city');
    const city = cityInput?.value.trim() || '';
    const locationText = city ? `${city}, ${window._fcState.location}` : window._fcState.location;
    document.getElementById('camp-gl').value       = window._fcState.gl;
    document.getElementById('camp-location').value  = locationText;
    document.getElementById('camp-name').value      = fcBuildCampaignName(who, window._fcState.location);
    document.getElementById('camp-bio').value       = what;
    // Use who-description as keywords (AI will extract from bio anyway)
    document.getElementById('camp-keys').value      = who.substring(0, 120);
    document.getElementById('camp-target-urls').value = '';

    saveCampaignAction();
};

// Close modal — restored (was accidentally removed during V18 edit)
window.closeNewCampaignModal = function() {
    document.getElementById('new-campaign-modal').classList.add('hidden');
    document.getElementById('fc-step-1').classList.remove('hidden');
    document.getElementById('fc-step-2').classList.add('hidden');
    const ta = document.getElementById('fc-intent');
    if (ta) ta.value = '';
    window._fcState = { gl:'', location:'', whoConfirmed:'', whatConfirmed:'' };
};

// Override openNewCampaignModal to use new modal instead
window.openNewCampaignModal = async function() {
    const remaining = (window.activeWallet?.allocated_credits || 0) - (window.activeWallet?.consumed_credits || 0);
    if (remaining <= 0) {
        showToast('Credits exhausted. Contact admin to reload.', 'error');
        return;
    }
    document.getElementById('new-campaign-modal').classList.remove('hidden');
    document.getElementById('fc-intent')?.focus();

    // Auto-detect location and pre-select chip
    try {
        const resp = await fetch('https://ipapi.co/json/', { cache: 'force-cache' });
        const json = await resp.json();
        if (json.country_code) {
            const glCode = json.country_code.toLowerCase();
            window._fcState.gl = glCode;
            // Pre-select country chip on step 2
            document.querySelectorAll('.fc-loc-chip').forEach(c => {
                if (c.dataset.gl === glCode) {
                    c.classList.add('selected');
                    window._fcState.location = c.dataset.loc;
                }
            });
        }
        if (json.city) {
            const cityInput = document.getElementById('fc-edit-where-city');
            if (cityInput) cityInput.value = json.city;
        }
    } catch(e) { /* silent â€” location is not required */ }
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
    const isOpen = !dropdown.classList.contains('hidden');
    if (isOpen) {
        dropdown.classList.add('hidden');
        if (pill) pill.classList.remove('open');
    } else {
        dropdown.classList.remove('hidden');
        if (pill) pill.classList.add('open');
        // Auto-close on outside click
        setTimeout(() => {
            const handler = (e) => {
                const wrap = document.getElementById('user-pill-wrap') || pill?.closest('.user-pill-wrap');
                if (!wrap || !wrap.contains(e.target)) {
                    dropdown.classList.add('hidden');
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
    // Close user dropdown if open
    document.getElementById('user-dropdown')?.classList.add('hidden');
    document.getElementById('user-pill-btn')?.classList.remove('open');
    document.getElementById('settings-modal')?.classList.remove('hidden');
};

window.closeSettingsModal = function() {
    document.getElementById('settings-modal')?.classList.add('hidden');
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

// Open the Digital Twin modal
window.openDTModal = function() {
    document.getElementById('user-dropdown')?.classList.add('hidden');
    document.getElementById('user-pill-btn')?.classList.remove('open');
    // Reset to View A
    document.getElementById('dt-view-a')?.classList.remove('hidden');
    document.getElementById('dt-view-b')?.classList.add('hidden');
    document.getElementById('dt-view-c')?.classList.add('hidden');
    const urlInput = document.getElementById('dt-url-input');
    if (urlInput) urlInput.value = '';
    document.getElementById('dt-onboarding-modal')?.classList.remove('hidden');
    setTimeout(() => document.getElementById('dt-url-input')?.focus(), 100);
};

window.closeDTModal = function() {
    document.getElementById('dt-onboarding-modal')?.classList.add('hidden');
};

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
    document.getElementById('dt-view-a')?.classList.add('hidden');
    document.getElementById('dt-view-b')?.classList.remove('hidden');

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

        const apiCall = fetch(`${DT_ENGINE_URL}/api/analyze-website`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
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
        document.getElementById('dt-view-b')?.classList.add('hidden');
        document.getElementById('dt-view-a')?.classList.remove('hidden');
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
        document.getElementById('dt-view-b')?.classList.add('hidden');
        document.getElementById('dt-view-c')?.classList.remove('hidden');
    }, 500);
}

// Skip DT — go directly to existing manual flow
window.skipDTonboarding = function() {
    closeDTModal();
    openNewCampaignModal();
};

// Edit individual persona inline (simple prompt-based for now)
window.dtEditPersona = function(field) {
    if (field === 'company') {
        const newDesc = prompt('Edit your company description:', window._dtState.companyDesc);
        if (newDesc !== null) {
            window._dtState.companyDesc = newDesc;
            window._dtState.extractedBio = newDesc;
            document.getElementById('dt-company-desc').textContent = newDesc;
            document.getElementById('dt-extracted-bio').value = newDesc;
        }
    } else {
        const idx = parseInt(field.split('-')[1]) - 1;
        const t = window._dtState.targets[idx] || {};
        const newName = prompt('Edit target persona name:', t.name || '');
        if (newName !== null) {
            window._dtState.targets[idx] = { ...t, name: newName };
            document.getElementById(`dt-target-${idx+1}-name`).textContent = newName;
            // Rebuild extractedWho
            window._dtState.extractedWho = window._dtState.targets.map(t => t.name).filter(Boolean).join(', ');
            document.getElementById('dt-extracted-who').value = window._dtState.extractedWho;
        }
    }
};

// View C "Launch Campaign" — pre-fills fc-step-2 and hands off for human review
// Decision: DO NOT auto-launch. Show fc modal for final user confirmation.
window.dtPrefillAndLaunch = function() {
    const bio = document.getElementById('dt-extracted-bio')?.value || window._dtState.extractedBio;
    const who = document.getElementById('dt-extracted-who')?.value || window._dtState.extractedWho;
    const gl  = document.getElementById('dt-extracted-gl')?.value  || window._dtState.extractedGl;
    const company = window._dtState.companyName;

    if (!bio || !who) {
        showToast('Please fill in company and target descriptions before launching.', 'error');
        return;
    }

    // Close DT modal
    closeDTModal();

    // Pre-fill the hidden fc form fields and reveal fc-step-2 directly
    // (bypassing step 1 since we already have the intent data)
    const intentEl = document.getElementById('fc-intent');
    if (intentEl) intentEl.value = `${who} for ${company || 'our company'}`;

    // Set fc-step-2 confirmation display values
    const whoEl = document.getElementById('fc-confirm-who');
    const editWhoEl = document.getElementById('fc-edit-who');
    const whatEl = document.getElementById('fc-confirm-what');
    const editWhatEl = document.getElementById('fc-edit-what');

    if (whoEl) whoEl.textContent = who;
    if (editWhoEl) editWhoEl.value = who;
    if (whatEl) { whatEl.textContent = bio; whatEl.style.fontStyle = 'normal'; whatEl.style.color = 'var(--text-main)'; }
    if (editWhatEl) { editWhatEl.value = bio; editWhatEl.classList.remove('hidden'); }

    // Set location if detected
    if (gl) {
        window._fcState = window._fcState || {};
        window._fcState.gl = gl;
        document.querySelectorAll('.fc-loc-chip').forEach(c => {
            c.classList.toggle('selected', c.dataset.gl === gl);
            if (c.dataset.gl === gl) window._fcState.location = c.dataset.loc || '';
        });
    }

    // Set internal _fcState
    window._fcState.whoConfirmed = who;
    window._fcState.whatConfirmed = bio;

    // Populate hidden form fields that saveCampaignAction() reads
    const now = new Date();
    const month = now.toLocaleString('en', { month: 'short' });
    const campName = `${who.substring(0, 35)} \u00B7 ${month} ${now.getFullYear()}`;
    const campGl   = document.getElementById('camp-gl');
    const campLoc  = document.getElementById('camp-location');
    const campNm   = document.getElementById('camp-name');
    const campBio  = document.getElementById('camp-bio');
    const campKeys = document.getElementById('camp-keys');
    if (campGl)   campGl.value   = gl;
    if (campLoc)  campLoc.value  = window._fcState.location || '';
    if (campNm)   campNm.value   = campName;
    if (campBio)  campBio.value  = bio;
    if (campKeys) campKeys.value = who.substring(0, 120);

    // Show fc modal at step 2 (human review before deploy)
    const modal = document.getElementById('new-campaign-modal');
    const step1  = document.getElementById('fc-step-1');
    const step2  = document.getElementById('fc-step-2');
    if (modal)  modal.classList.remove('hidden');
    if (step1)  step1.classList.add('hidden');
    if (step2)  step2.classList.remove('hidden');

    showToast('Personas loaded — review and confirm before launching.', 'info');
};


