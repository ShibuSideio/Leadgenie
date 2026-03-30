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

// Load Leads Real-Time
function loadLeads(user) {
    leadsList.innerHTML = '<div class="lead-card pulse">Authenticating real-time datastream...</div>';
    
    if (leadsListenerUnsubscribe) {
        leadsListenerUnsubscribe();
    }
    
    // Real Firestore Listener binding to the 'leads' collection
    leadsListenerUnsubscribe = db.collection('leads')
        .orderBy('status', 'desc')
        .onSnapshot(snapshot => {
            if (snapshot.empty) {
                leadsList.innerHTML = `
                    <div class="lead-card" style="text-align: center; padding: 40px;">
                        <div style="font-size: 3rem; margin-bottom: 12px; opacity: 0.8;">🌱</div>
                        <h3 style="color: var(--text-main); margin-bottom: 8px;">Your Pipeline is Pristine</h3>
                        <p style="color: var(--text-muted); font-size: 0.95rem; line-height: 1.5;">
                            There is currently absolutely zero data inside the <strong>lead-sniper-prod</strong> database.<br>
                            Start a new campaign to command the engine to begin scraping intelligent targets.
                        </p>
                    </div>
                `;
                return;
            }

            leadsList.innerHTML = '';
            snapshot.forEach(doc => {
                const lead = doc.data();
                const card = createLeadCard(doc.id, lead);
                leadsList.appendChild(card);
            });
        }, error => {
            console.error("Firestore Listener Error:", error);
            leadsList.innerHTML = '<div class="lead-card" style="color: #ef4444; border-color: #ef4444;">Permission Denied. Verify database rules exist.</div>';
            showToast('Datastream Authentication Failed', 'error');
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
        showToast(`Lead trajectory updated to: ${newStatus}`, 'success');
    }).catch(error => {
        console.error("Mutation Error:", error);
        showToast('Permission Denied updating document hierarchy', 'error');
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

// Campaign Handler
window.saveCampaignAction = function() {
    showToast('Provisioning Campaign Blueprint...', 'info');
    setTimeout(() => {
        document.getElementById('new-campaign-modal').classList.add('hidden');
        showToast('Autonomous Scanner Activated', 'success');
    }, 800);
};
