// Firebase configuration
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

// Load Leads
function loadLeads(user) {
    // In a real scenario, you'd fetch the user's specific tenant ID. 
    // Here we query "leads" group collection or specific path
    // For V16 architecture, users read from tenants/{tenantId}/leads
    
    // Simulating loading state
    leadsList.innerHTML = '<div class="lead-card pulse">Loading intelligence...</div>';
    
    // Mock Data rendering for Demo 
    setTimeout(() => {
        renderMockLeads();
    }, 1500);
}

function renderMockLeads() {
    const mockLeads = [
        {
            url: "https://linkedin.com/in/demouser1",
            score: 9,
            pain_point: "Struggling to manage disparate B2B sales pipelines.",
            dm: "Hi Demo, saw you're navigating complex sales pipelines right now. I built a tool that simplifies this natively on GCP—would love to share how it solves those disjointed workflows without any sales pressure.",
            source: "LinkedIn",
            status: "new"
        },
        {
            url: "https://twitter.com/targetSME",
            score: 8,
            pain_point: "High ad spend on Meta with low conversion rates.",
            dm: "Hey! Noticed you mentioned Meta ad costs spiking. Have you considered leveraging organic search signals instead? Built a tool for MSMEs doing exactly that, let me know if you want a look.",
            source: "Twitter",
            status: "contacted"
        }
    ];

    leadsList.innerHTML = '';
    mockLeads.forEach(lead => {
        const card = document.createElement('div');
        card.className = 'lead-card';
        card.innerHTML = `
            <div class="lead-header">
                <div>
                    <strong><a href="${lead.url}" target="_blank" style="color: white; text-decoration: none;">${new URL(lead.url).hostname}</a></strong> • ${lead.source}
                </div>
                <div class="score">Score: ${lead.score}/10</div>
            </div>
            <div class="pain-point">" ${lead.pain_point} "</div>
            <div class="dm-draft">${lead.dm}</div>
            <div class="action-row" style="flex-wrap: wrap; gap: 8px; margin-top:12px; padding-top:12px; border-top: 1px solid rgba(255,255,255,0.1)">
                <button class="action-btn" title="Contact Complete">✅ Complete</button>
                <button class="action-btn" title="Ignore Lead">🚫 Ignore</button>
                <button class="action-btn" title="Lead Converted">🎯 Converted</button>
                <button class="action-btn" title="No Response">👻 No Response</button>
                <button class="action-btn" title="Follow-up Later">⏰ Follow-up</button>
            </div>
        `;
        leadsList.appendChild(card);
    });
}
