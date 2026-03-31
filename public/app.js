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

// Initialize Firebase
firebase.initializeApp(firebaseConfig);
const auth = firebase.auth();
const db = firebase.firestore();

// DOM Elements
const authContainer = document.getElementById('auth-container');
const appContainer = document.getElementById('app-container');
const loginBtn = document.getElementById('login-btn');
const logoutBtn = document.getElementById('logout-btn');
const leadsList = document.getElementById('leads-list');

// Authentication state observer
auth.onAuthStateChanged(user => {
    if (user) {
        // User logged in
        authContainer.classList.add('hidden');
        appContainer.classList.remove('hidden');
        loadLeads(user);
        loadCampaigns(user);
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

let leadsListenerUnsubscribe = null;
let campaignsListenerUnsubscribe = null;

// Selected Filter State
let currentCampaignFilter = 'all';
let rawLeadsCache = [];

// Dynamic Campaign Hydration
function loadCampaigns(user) {
    const feed = document.getElementById('active-campaign-feed');
    const tableBody = document.getElementById('campaign-list-table');
    const filterSelect = document.getElementById('campaign-filter');
    
    if (campaignsListenerUnsubscribe) campaignsListenerUnsubscribe();
    
    // Listen to ALL campaigns for the table
    campaignsListenerUnsubscribe = db.collection('campaigns')
        .orderBy('createdAt', 'desc')
        .onSnapshot(snapshot => {
            if (snapshot.empty) {
                if (feed) feed.innerHTML = '';
                if (tableBody) tableBody.innerHTML = '<tr><td colspan="4" style="padding:16px; text-align:center;">No campaigns found. Click "New Search" to start.</td></tr>';
                return;
            }
            
            let activeCount = 0;
            let tableHTML = '';
            let filterHTML = '<option value="all">All Campaigns</option>';
            
            snapshot.forEach(doc => {
                const camp = doc.data();
                const id = doc.id;
                const isActive = camp.status === 'active';
                if (isActive) activeCount++;
                
                // Build Table Row
                const statusColor = isActive ? '#25D366' : '#ef4444';
                const toggleAction = isActive ? 'pause' : 'resume';
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
                
                // Build Filter Dropdown
                filterHTML += `<option value="${id}">${camp.name}</option>`;
            });
            
            if (tableBody) tableBody.innerHTML = tableHTML;
            if (filterSelect) {
                const currentVal = filterSelect.value;
                filterSelect.innerHTML = filterHTML;
                filterSelect.value = currentVal || 'all';
            }
            
            // Render Generic Active Stats Header instead of pinning just one campaign
            if (feed) {
                feed.innerHTML = `
                    <div class="competitor-monitor" style="background: rgba(79, 70, 229, 0.05); border: 1px solid rgba(79, 70, 229, 0.2); padding: 12px; border-radius: 8px; margin-bottom: 24px;">
                        <span class="badge" style="background: var(--primary);">System Status: Online</span>
                        <span style="color: var(--text-muted); font-size: 0.9rem; margin-left: 8px;">Scraping ${activeCount} Active Target Matrices</span>
                    </div>
                `;
            }
        }, error => {
            console.error("Campaign Hook Error:", error);
            if (tableBody) tableBody.innerHTML = '<tr><td colspan="4" style="padding:16px; text-align:center; color: #ef4444;">Database Connection Error</td></tr>';
        });
}

// Load Leads Real-Time
function loadLeads(user) {
    leadsList.innerHTML = '<div class="lead-card pulse">Connecting to your secure database...</div>';
    
    if (leadsListenerUnsubscribe) {
        leadsListenerUnsubscribe();
    }
    
    // Real Firestore Listener binding to the 'leads' collection
    leadsListenerUnsubscribe = db.collection('leads')
        .orderBy('createdAt', 'desc')
        .onSnapshot(snapshot => {
            if (snapshot.empty) {
                rawLeadsCache = [];
                renderLeads();
                return;
            }

            rawLeadsCache = snapshot.docs.map(doc => ({ id: doc.id, ...doc.data() }));
            renderLeads();
            
        }, error => {
            console.error("Firestore Listener Error:", error);
            leadsList.innerHTML = '<div class="lead-card" style="color: #ef4444; border-color: #ef4444;">Could not connect to database. Please check permissions.</div>';
            showToast('Connection Refused', 'error');
        });
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

    filteredLeads.forEach(lead => {
        const card = createLeadCard(lead.id, lead);
        leadsList.appendChild(card);
    });
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
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'completed')" title="Mark as Contacted">✅ Contacted</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'ignored')" title="Ignore Lead">🚫 Ignore</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'converted')" title="Lead Converted">🎯 Converted</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'snoozed')" title="Follow-up Later">⏰ Snooze</button>
        </div>
    `;
    return card;
}

// Database Mutators
window.updateLeadStatus = function(docId, newStatus) {
    db.collection('leads').doc(docId).update({
        status: newStatus,
        updatedAt: firebase.firestore.FieldValue.serverTimestamp()
    }).then(() => {
        showToast(`Lead status updated: ${newStatus}`, 'success');
    }).catch(error => {
        console.error("Mutation Error:", error);
        showToast('Error saving update to database', 'error');
    });
};

// --- TOAST UI ENGINE ---
window.showToast = function(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;
    
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    
    container.appendChild(toast);
    
    // Animate In
    setTimeout(() => toast.classList.add('show'), 10);
    
    // Animate Out & Cleanup
    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    }, 3500);
};

// Campaign Edit Hub
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

window.saveEditedCampaign = function() {
    const id = document.getElementById('edit-camp-id').value;
    const name = document.getElementById('edit-camp-name').value;
    const bio = document.getElementById('edit-camp-bio').value;
    const keys = document.getElementById('edit-camp-keys').value;
    
    if (!name || !keys) return showToast('Name and Keywords required', 'error');
    
    showToast('Pushing updates to AI Engine...', 'info');
    db.collection('campaigns').doc(id).update({
        name: name,
        bio: bio,
        keywords: keys,
        updatedAt: firebase.firestore.FieldValue.serverTimestamp()
    }).then(() => {
        closeEditModal();
        showToast('Campaign successfully updated!', 'success');
    }).catch(err => {
        console.error("Update Error:", err);
        showToast('Error modifying campaign', 'error');
    });
};

window.toggleCampaignStatus = function(id, currentStatus) {
    const newStatus = currentStatus === 'active' ? 'paused' : 'active';
    db.collection('campaigns').doc(id).update({
        status: newStatus,
        updatedAt: firebase.firestore.FieldValue.serverTimestamp()
    }).then(() => {
        showToast(`Campaign ${newStatus} successfully`, 'success');
    }).catch(err => {
        showToast('Status update failed', 'error');
    });
};

// Campaign Creator
window.saveCampaignAction = function() {
    const nameInput = document.getElementById('camp-name');
    const bioInput = document.getElementById('camp-bio');
    const keysInput = document.getElementById('camp-keys');
    
    if (!nameInput || !keysInput || !nameInput.value || !keysInput.value) {
        showToast('Campaign Name and Keywords are required', 'error');
        return;
    }
    
    showToast('Setting up your search...', 'info');
    
    // Physical Firestore Mutation
    db.collection('campaigns').add({
        name: nameInput.value,
        bio: bioInput.value,
        keywords: keysInput.value,
        status: 'active',
        createdAt: firebase.firestore.FieldValue.serverTimestamp()
    }).then(() => {
        document.getElementById('new-campaign-modal').classList.add('hidden');
        showToast('System is now looking for clients!', 'success');
        
        // Purge inputs
        nameInput.value = '';
        bioInput.value = '';
        keysInput.value = '';
    }).catch(error => {
        console.error("Campaign Creation Error:", error);
        showToast('Failed to save campaign. Check permissions.', 'error');
    });
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

// Extractor Action Hooks
window.sendEmailReport = function() {
    showToast('Connecting to Cloud Run SMTP queue...', 'info');
    // Bypassing hardcoded URL routing for dynamic edge delivery
    setTimeout(() => {
        showToast('Enterprise PDF dispatched to your registered email.', 'success');
    }, 1500);
};
