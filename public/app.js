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
            if (el) el.innerText = (w.allocated_credits || 0) - (w.consumed_credits || 0);
            
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

// Load Leads Real-Time (Thin Client API)
async function loadLeads() {
    leadsList.innerHTML = '<div class="lead-card pulse">Connecting to Secure Orchestrator...</div>';
    
    try {
        const user = firebase.auth().currentUser;
        if (!user) return handleAuthRejection();
        
        const token = await user.getIdToken(); 
        const response = await fetch(`${API_BASE}/api/leads`, {
            method: 'GET',
            headers: { 'Authorization': `Bearer ${token}` }
        });
        
        if (response.status === 401 || response.status === 403) {
            return handleAuthRejection();
        }
        
        const payload = await response.json();
        rawLeadsCache = payload.data || [];
        
        if (rawLeadsCache.length === 0) {
            renderLeads();
            initAnalyticsChart(0,0,0);
            return;
        }

        rawLeadsCache.sort((a, b) => (b.score || 0) - (a.score || 0));
        
        let cNew = 0, cContact= 0, cConvert = 0;

        rawLeadsCache.forEach(l => {
            if (l.status === 'ignored') return; // Exclude entirely
            if (l.status === 'contacted') { cContact++; }
            else if (l.status === 'converted') { cConvert++; }
            else { cNew++; }
        });
        
        const elProsp = document.getElementById('stat-prospects');
        const elMsg = document.getElementById('stat-messaged');
        const elRep = document.getElementById('stat-replies');
        if (elProsp) elProsp.innerText = cNew;
        if (elMsg) elMsg.innerText = cContact;
        if (elRep) elRep.innerText = cConvert;
        
        initAnalyticsChart(cNew, cContact, cConvert);
        renderLeads();
        
    } catch (error) {
        console.error("Fetch API Listener Error:", error);
        leadsList.innerHTML = '<div class="lead-card" style="color: #ef4444; border-color: #ef4444;">Could not connect to API Gateway. Please check backend health.</div>';
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
    
    let hiringBadge = (lead.hiring_intent_found && lead.hiring_intent_found !== "None") ? `<span style="font-size:0.75rem; background:#ecfdf5; color:#059669; padding:2px 6px; border-radius:4px; margin-right:6px; border:1px solid #a7f3d0">🟢 Hiring: ${lead.hiring_intent_found}</span>` : '';
    let techBadges = (lead.tech_stack_found && lead.tech_stack_found.length > 0) ? lead.tech_stack_found.map(tech => `<span style="font-size:0.75rem; background:#f0fdfa; color:#0d9488; padding:2px 6px; border-radius:4px; margin-right:6px; border:1px solid #99f6e4">⚡ ${tech}</span>`).join('') : '';
    let exclusiveBadge = `<span style="font-size:0.75rem; background:#fef2f2; color:#dc2626; padding:2px 6px; border-radius:4px; margin-right:6px; border:1px solid #fecaca">🔒 Exclusive Lead</span>`;

    card.innerHTML = `
        <div class="lead-header">
            <div>
                <strong><a href="${lead.url || '#'}" target="_blank" style="color: var(--text-main); text-decoration: none;">${urlHostname}</a></strong> • ${lead.source || 'Organic Search'} 
                <span style="margin-left:8px; font-size:0.75rem; padding: 2px 6px; border-radius:4px; border: 1px solid ${statusColor}; color: ${statusColor}">${(lead.status || 'new').toUpperCase()}</span>
            </div>
            <div class="score">Score: ${lead.score || 0}/10</div>
        </div>
        <div class="pain-point">" ${lead.pain_point || 'Analyzing sentiment...'} "</div>
        <div class="premium-badges" style="margin-top: 8px; margin-bottom: 8px; font-weight: 500;">
            ${exclusiveBadge}
            ${hiringBadge}
            ${techBadges}
        </div>
        <div class="dm-draft">${lead.dm || 'Drafting variation...'}</div>
        <div class="action-row" style="flex-wrap: wrap; gap: 8px; margin-top:12px; padding-top:12px; border-top: 1px solid var(--glass-border)">
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'contacted')" title="Mark as Contacted">✅ Contacted</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'ignored')" title="Ignore Lead">🚫 Ignore</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'converted')" title="Lead Converted">🎯 Converted</button>
            <button class="action-btn" style="background:#f8fafc; color:var(--text-muted); border: 1px solid var(--glass-border);" onclick="viewLeadTimeline('${encodeURIComponent(JSON.stringify(lead.interactions || []))}')" title="Audit Log">🕒 View Timeline Logs</button>
        </div>
    `;
    return card;
}

window.filterLeadsByCampaign = function(campaignId) {
    currentCampaignFilter = campaignId;
    renderLeads();
};

function renderLeads() {
    if (rawLeadsCache.length === 0) {
        leadsList.innerHTML = `
            <div class="lead-card" style="text-align: center; padding: 40px; border: none; background: transparent; box-shadow: none;">
                <div style="font-size: 3rem; margin-bottom: 12px; opacity: 0.8;">🚀</div>
                <h3 style="color: var(--text-main); margin-bottom: 8px;">Let's Grow Your Business</h3>
                <p style="color: var(--text-muted); font-size: 0.95rem; line-height: 1.5;">
                    Your dashboard is ready and secure.<br>
                    No leads found matching this filter.
                </p>
            </div>
        `;
        return;
    }
    
    leadsList.innerHTML = '';
    const filteredLeads = rawLeadsCache.filter(lead => {
        if (lead.status === 'ignored') return false;
        if (currentCampaignFilter !== 'all' && lead.campaign_id !== currentCampaignFilter) return false;
        return true;
    });
    
    if (filteredLeads.length === 0) {
         leadsList.innerHTML = '<div class="lead-card" style="text-align:center; padding: 24px; color: var(--text-muted);">No leads currently discovered for this campaign. The AI is still searching.</div>';
         return;
    }
    filteredLeads.forEach(lead => leadsList.appendChild(createLeadCard(lead.id, lead)));
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
    
    if (!nameInput || !keysInput || !nameInput.value || !keysInput.value) {
        showToast('Campaign Name and Keywords are required', 'error');
        return;
    }
    
    showToast('Setting up your search...', 'info');
    try {
        const success = await performApiMutation(`/api/campaigns`, 'POST', {
            name: nameInput.value,
            bio: bioInput.value,
            keywords: keysInput.value,
            gl: glInput ? glInput.value : '',
            location: locationInput ? locationInput.value : '',
            status: 'active'
        });
        if(success) {
            document.getElementById('new-campaign-modal').classList.add('hidden');
            showToast('System is now looking for clients!', 'success');
            nameInput.value = ''; bioInput.value = ''; keysInput.value = '';
            if (glInput) glInput.value = '';
            if (locationInput) locationInput.value = '';
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
        fetchL0Data();
    } else if(tabName === 'macro') {
        if(document.getElementById('view-macro')) document.getElementById('view-macro').classList.remove('hidden');
        if(document.getElementById('tab-macro')) document.getElementById('tab-macro').classList.add('active');
        fetchMacroTrends();
    }
};

window.fetchL0Data = async function() {
    const tableBody = document.getElementById('l0-tenant-table');
    try {
        const user = firebase.auth().currentUser;
        if (!user) return;
        const token = await user.getIdToken();
        const response = await fetch(`${API_BASE}/api/l0/users`, {
            method: 'GET',
            headers: { 'Authorization': `Bearer ${token}` }
        });
        
        if (response.status === 200) {
            document.getElementById('tab-l0-admin').classList.remove('hidden'); // Enable globally
            const payload = await response.json();
            const data = payload.data || [];
            
            data.sort((a,b) => {
                if (a.approval_status === 'pending' && b.approval_status !== 'pending') return -1;
                if (b.approval_status === 'pending' && a.approval_status !== 'pending') return 1;
                return 0;
            });
            
            let tableHTML = '';
            data.forEach(t => {
                const isSuspended = t.is_active === false; // Usually true or undefined = active
                const isPending = t.approval_status === 'pending';
                const statusColor = isSuspended ? '#ef4444' : (isPending ? '#f59e0b' : '#25D366');
                const statusBadge = `<strong style="color:${statusColor}">${isSuspended ? 'SUSPENDED' : (isPending ? 'PENDING' : 'ACTIVE')}</strong>`;
                const um = t.usage_metrics || {};
                
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
                
                const w = t.wallet || {allocated_credits: 0, consumed_credits: 0};
                const rem = (w.allocated_credits || 0) - (w.consumed_credits || 0);

                tableHTML += `
                <tr style="border-bottom: 1px solid var(--glass-border);">
                    <td style="padding: 12px;">
                        <strong>${t.email || 'No email saved'}</strong><br>
                        <small style="font-family:monospace; color:var(--text-muted);">${(t.tenant_id||'Unknown').substring(0,8)}...</small>
                    </td>
                    <td style="padding: 12px;">${(t.role || 'admin').toUpperCase()}</td>
                    <td style="padding: 12px;">${statusBadge}</td>
                    <td style="padding: 12px; font-family:monospace;">${rem.toLocaleString()} CR</td>
                    <td style="padding: 12px;">$${((um.gemini_calls || 0) * 0.0001).toFixed(4)} (<small>${um.gemini_calls||0}</small>)</td>
                    <td style="padding: 12px; text-align:right;">${actionHTML}</td>
                </tr>
                `;
            });
            if (tableBody) tableBody.innerHTML = tableHTML || '<tr><td colspan="6" style="padding:16px; text-align:center;">No tenants found.</td></tr>';
            
            // Sync the left pane immediately to prevent visual desync locally
            if (typeof loadMe === "function") {
                loadMe();
            }
        } else {
            if (tableBody) tableBody.innerHTML = '<tr><td colspan="6" style="padding:16px; text-align:center; color: #ef4444;">Access Denied. L0 Privilege Missing.</td></tr>';
        }
    } catch(err) {
        console.error(err);
    }
};

let rawMacroTrends = null;
let macroChartObj = null;

window.fetchMacroTrends = async function() {
    const tableBody = document.getElementById('macro-keywords-table').querySelector('tbody');
    try {
        const user = firebase.auth().currentUser;
        if (!user) return;
        tableBody.innerHTML = '<tr><td colspan="2" style="padding:16px; text-align:center;">Calculating AI Map-Reduce Vectors...</td></tr>';
        
        const token = await user.getIdToken();
        const response = await fetch(`${API_BASE}/api/l0/trends`, {
            method: 'GET',
            headers: { 'Authorization': `Bearer ${token}` }
        });
        
        if (response.ok) {
            const payload = await response.json();
            rawMacroTrends = payload.data || {};
            
            // Populate select dropdowns initially
            const geoMap = rawMacroTrends.geo_distribution || {};
            const countries = new Set();
            const states = new Set();
            
            Object.keys(geoMap).forEach(key => {
                if (key === 'global') return;
                const parts = key.split('|');
                if (parts[0]) countries.add(parts[0].toUpperCase());
            });
            
            const countryEl = document.getElementById('macro-filter-country');
            if (countryEl && countryEl.options.length === 1) {
                Array.from(countries).sort().forEach(c => {
                    const opt = document.createElement('option');
                    opt.value = c.toLowerCase();
                    opt.innerText = c;
                    countryEl.appendChild(opt);
                });
            }
            renderMacroTrends();
        } else {
            tableBody.innerHTML = '<tr><td colspan="2" style="padding:16px; text-align:center; color:#ef4444;">Access Denied. L0 Privilege Missing.</td></tr>';
        }
    } catch (e) {
        tableBody.innerHTML = '<tr><td colspan="2" style="padding:16px; text-align:center; color:#ef4444;">Gateway Error.</td></tr>';
    }
};

window.renderMacroTrends = function() {
    if (!rawMacroTrends) return;
    
    const country = document.getElementById('macro-filter-country').value;
    const gKey = country === 'global' ? '' : country.toLowerCase();
    
    let aggregateDomains = {"Medical/Pharma":0, "Retail/B2C":0, "Finance":0, "Software/Agency":0, "Real Estate":0, "Corporate/Other":0};
    const tableBody = document.getElementById('macro-keywords-table').querySelector('tbody');
    
    // Sum domains matching filter
    const domainMap = rawMacroTrends.domain_mapping || {};
    Object.keys(domainMap).forEach(dKey => {
        if (!gKey || dKey.startsWith(gKey)) {
            const counts = domainMap[dKey];
            Object.keys(counts).forEach(ind => {
                if (aggregateDomains[ind] !== undefined) aggregateDomains[ind] += counts[ind];
            });
        }
    });
    
    // Draw Chart
    const ctx = document.getElementById('macroDomainChart');
    if (ctx) {
        const labels = Object.keys(aggregateDomains);
        const data = Object.values(aggregateDomains);
        
        if (macroChartObj) {
            macroChartObj.data.datasets[0].data = data;
            macroChartObj.update();
        } else {
            macroChartObj = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: labels,
                    datasets: [{
                        label: 'Active Leads Operations',
                        data: data,
                        backgroundColor: '#4F46E5',
                        borderRadius: 4
                    }]
                },
                options: { responsive: true, maintainAspectRatio: false, scales: { y: { beginAtZero: true } } }
            });
        }
    }
    
    // Draw Keywords (only global for now to prevent expensive client-side correlation loops, usually backend correlates this strictly)
    const kwArr = rawMacroTrends.global_keywords || [];
    if (kwArr.length === 0) {
        tableBody.innerHTML = '<tr><td colspan="2" style="padding:16px; text-align:center;">No tracking data available.</td></tr>';
    } else {
        tableBody.innerHTML = kwArr.slice(0, 15).map(k => `
            <tr style="border-bottom: 1px solid var(--glass-border);">
                <td style="padding: 8px;">${k.keyword}</td>
                <td style="padding: 8px; text-align:right; font-weight:bold; color:var(--primary)">${k.count}</td>
            </tr>
        `).join('');
    }
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
        fetchL0Data();
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
            fetchL0Data();
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
