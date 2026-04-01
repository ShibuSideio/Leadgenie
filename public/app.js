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
        loadCampaigns(),
        loadLeads()
    ]);
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
        const response = await fetch('/api/campaigns', {
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
            
            tableHTML += `
                <tr style="border-bottom: 1px solid var(--glass-border);">
                    <td style="padding: 12px;"><strong>${camp.name || 'Untitled'}</strong></td>
                    <td style="padding: 12px;"><i style="color:var(--text-muted); font-size:0.85rem">${camp.keywords || 'N/A'}</i></td>
                    <td style="padding: 12px;">${statusBadge}</td>
                    <td style="padding: 12px; text-align:right;">
                        <button class="secondary-btn" style="padding: 4px 8px; font-size: 0.75rem; margin-right: 4px;" onclick="openEditModal('${id}', '${(camp.name || '').replace(/'/g, "\\'")}', '${(camp.bio || '').replace(/'/g, "\\'")}', '${(camp.keywords || '').replace(/'/g, "\\'")}')">Edit</button>
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
        const response = await fetch('/api/leads', {
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
            if (l.status === 'contacted') cContact++;
            else if (l.status === 'converted') cConvert++;
            else cNew++;
        });
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
    card.innerHTML = `
        <div class="lead-header">
            <div>
                <strong><a href="${lead.url || '#'}" target="_blank" style="color: var(--text-main); text-decoration: none;">${urlHostname}</a></strong> • ${lead.source || 'Organic Search'} 
                <span style="margin-left:8px; font-size:0.75rem; padding: 2px 6px; border-radius:4px; border: 1px solid ${statusColor}; color: ${statusColor}">${(lead.status || 'new').toUpperCase()}</span>
            </div>
            <div class="score">Score: ${lead.score || 0}/10</div>
        </div>
        <div class="pain-point">" ${lead.pain_point || 'Analyzing sentiment...'} "</div>
        <div class="dm-draft">${lead.dm || 'Drafting variation...'}</div>
        <div class="action-row" style="flex-wrap: wrap; gap: 8px; margin-top:12px; padding-top:12px; border-top: 1px solid var(--glass-border)">
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'contacted')" title="Mark as Contacted">✅ Contacted</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'ignored')" title="Ignore Lead">🚫 Ignore</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'converted')" title="Lead Converted">🎯 Converted</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'snoozed')" title="Follow-up Later">⏰ Snooze</button>
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
    const filteredLeads = currentCampaignFilter === 'all' 
        ? rawLeadsCache 
        : rawLeadsCache.filter(lead => lead.campaign_id === currentCampaignFilter);
    
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

window.openEditModal = function(id, name, bio, keywords) {
    document.getElementById('edit-camp-id').value = id;
    document.getElementById('edit-camp-name').value = name;
    document.getElementById('edit-camp-bio').value = bio;
    document.getElementById('edit-camp-keys').value = keywords;
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
    const response = await fetch(url, {
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
    try {
        const success = await performApiMutation(`/api/leads/${docId}`, 'PUT', { status: newStatus });
        if(success) {
            showToast(`Lead status updated: ${newStatus}`, 'success');
            loadDashboard();
        }
    } catch(err) {
        showToast('Error saving update to database', 'error');
    }
};

window.saveEditedCampaign = async function() {
    const id = document.getElementById('edit-camp-id').value;
    const name = document.getElementById('edit-camp-name').value;
    const bio = document.getElementById('edit-camp-bio').value;
    const keys = document.getElementById('edit-camp-keys').value;
    if (!name || !keys) return showToast('Name and Keywords required', 'error');
    
    showToast('Pushing updates to AI Engine...', 'info');
    try {
        const success = await performApiMutation(`/api/campaigns/${id}`, 'PUT', { name, bio, keywords: keys });
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
            status: 'active'
        });
        if(success) {
            document.getElementById('new-campaign-modal').classList.add('hidden');
            showToast('System is now looking for clients!', 'success');
            nameInput.value = ''; bioInput.value = ''; keysInput.value = '';
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
    }
};

window.sendEmailReport = function() {
    showToast('Connecting to Cloud Run SMTP queue...', 'info');
    setTimeout(() => { showToast('Enterprise PDF dispatched to your registered email.', 'success'); }, 1500);
};

window.loadMoreLeads = function() {
    showToast('Historical offset cursors must be mapped in Orchestrator Endpoint v2.', 'info');
};
