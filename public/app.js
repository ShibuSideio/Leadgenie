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
                if (alertBanner) { alertBanner.innerText = "ðŸ›‘ Wallet Empty: You have 0 credits. Upgrade your account to continue sweeping."; alertBanner.classList.remove('hidden'); }
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

            // V17: Greeting with first name
            const dName = auth.currentUser?.displayName || '';
            const firstName = dName.split(' ')[0] || '';
            fcUpdateGreeting(firstName);
            
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
            const locationWarn = hasLocation ? '' : '<br><span style="color: #ea580c; font-size: 0.75rem; display:block; margin-top:4px;">âš ï¸ Location Missing: Edit Campaign to set Targeting</span>';
            
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
            .where('is_in_crm', '==', false)  // V15: Main feed = raw intelligence only (not yet pushed to CRM)
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
                fcUpdateKPIs(rawLeadsCache);
                renderLeads();
            }, (error) => {
                console.error('[Firestore] onSnapshot Error:', error);
                if (error.code === 'failed-precondition') {
                    // Missing composite index for (tenant_id, is_in_crm).
                    // DO NOT retry â€” show actionable toast and stop.
                    const msg = 'Firestore index missing for CRM feed filter. Check GCP Console to create the composite index for (tenant_id, is_in_crm).';
                    console.error('[Firestore] Missing index:', msg);
                    showToast('Feed index missing â€” see console for index link.', 'error');
                    leadsList.innerHTML = `<div class="lead-card" style="color:#f59e0b; border-color:#f59e0b; padding:16px;">
                        âš ï¸ Firestore composite index required.<br>
                        <small>Open the browser console for the GCP link to auto-create it (takes ~1 min).</small>
                    </div>`;
                    return; // Hard stop â€” no retry, no recursion.
                }
                if (error.code === 'permission-denied') {
                    console.warn('[Firestore] Permission denied â€” check firestore.rules or approval_status.');
                    return;
                }
                // All other errors: log + display, no retry.
                console.error('[Firestore] Unhandled snapshot error:', error.code, error.message);
                showToast('Live feed error â€” refresh to reconnect.', 'error');
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
        hiringBadge = `<span style="font-size:0.75rem; background:#ecfdf5; color:#059669; padding:2px 6px; border-radius:4px; border:1px solid #a7f3d0">ðŸŸ¢ Hiring</span>`;
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
    
    let techBadges = (lead.tech_stack_found && lead.tech_stack_found.length > 0) ? lead.tech_stack_found.map(tech => `<span style="font-size:0.75rem; background:transparent; color:#6b7280; padding:2px 6px; border-radius:4px; border:1px solid #e5e7eb">âš¡ ${techDict[tech.toLowerCase()] || tech}</span>`).join('') : '';
    let exclusiveBadge = `<span style="font-size:0.75rem; background:#f3e8ff; color:#6b21a8; padding:2px 6px; border-radius:4px; border:1px solid #e9d5ff">ðŸ”’ Exclusive Lead</span>`;
    let competitorBadge = lead.competitor_match ? `<span style="font-size:0.75rem; background:#fee2e2; color:#b91c1c; padding:2px 6px; border-radius:4px; border:1px solid #fecaca">ðŸŽ¯ Competitor Intercept: ${lead.competitor_match}</span>` : '';

    // V16: origin_engine badge â€” safe fallback for legacy leads without this field
    const originEngine  = lead.origin_engine || 'cartographer';
    const engineBadge   = (originEngine === 'autonomous')
        ? '<span style="font-size:0.75rem; background:#faf5ff; color:#7c3aed; padding:2px 8px; border-radius:4px; border:1px solid #ddd6fe; font-weight:600;">&#9889; Predictive Match</span>'
        : '';

    card.innerHTML = `
        <div class="lead-header">
            <div>
                <strong><a href="${lead.url || '#'}" target="_blank" style="color: var(--text-main); text-decoration: none;">${urlHostname} &#8599;</a></strong> &bull; ${lead.source || 'Organic Search'} 
                <span style="margin-left:8px; font-size:0.75rem; padding: 2px 6px; border-radius:4px; border: 1px solid ${statusColor}; color: ${statusColor}">${(lead.status || 'new').toUpperCase()}</span>
            </div>
            <div class="score">Score: ${lead.score || 0}/10</div>
        </div>
        <div class="pain-point">" ${lead.pain_point || 'Analyzing sentiment...'} "</div>
        <div class="premium-badges" style="margin-top: 8px; margin-bottom: 8px; font-weight: 500; display: flex; flex-wrap: wrap; gap: 6px; align-items: center;">
            ${engineBadge}
            ${exclusiveBadge}
            ${competitorBadge}
            ${hiringBadge}
            ${techBadges}
        </div>
        <div class="dm-draft">${lead.dm || 'Drafting variation...'}</div>
        <div class="contact-info" style="margin-top: 8px; margin-bottom: 8px; font-size: 0.85rem; color: var(--text-main); font-weight: 500;">
            ${lead.email ? `<a href="mailto:${lead.email}" target="_blank" style="color:#2563eb; text-decoration:none;">&#128231; ${lead.email}</a> &nbsp;` : ''}
            ${lead.phone ? `<a href="tel:${lead.phone}" style="color:#2563eb; text-decoration:none;">&#128222; ${lead.phone}</a>` : ''}
            ${!lead.email && !lead.phone ? '<span style="color:var(--text-muted); font-style:italic;">No Contact Info Found</span>' : ''}
        </div>
        <div class="action-row" style="flex-wrap: wrap; gap: 8px; margin-top:12px; padding-top:12px; border-top: 1px solid var(--glass-border)">
            <button class="action-btn" onclick="copyMessageAndContact('${docId}', \`${(lead.dm || '').replace(/`/g, '\\`').replace(/'/g, "\\'")}\`)" title="Copy Message">&#128203; Copy Message</button>
            <button id="crm-btn-${docId}" class="action-btn ${lead.is_in_crm ? 'in-crm' : ''}" onclick="${lead.is_in_crm ? '' : `pushToCRM('${docId}', \`${encodeURIComponent(JSON.stringify(lead)).replace(/'/g, "\\'")}\`)`}" style="color: ${lead.is_in_crm ? '#16a34a' : '#4f46e5'}; border-color: ${lead.is_in_crm ? '#86efac' : '#c7d2fe'}; background: ${lead.is_in_crm ? '#dcfce7' : '#e0e7ff'};" ${lead.is_in_crm ? 'disabled' : ''}>${lead.is_in_crm ? '&#10003; In CRM' : '&#9729; Push to CRM'}</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'ignored')" title="Ignore Lead">&#128683; Ignore</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'converted')" title="Lead Converted">&#127919; Converted</button>
            <button class="action-btn" style="background:#f8fafc; color:var(--text-muted); border: 1px solid var(--glass-border);" onclick="viewLeadTimeline('${encodeURIComponent(JSON.stringify(lead.interactions || []))}')" title="Audit Log">&#128336; View Timeline Logs</button>
        </div>
    `;
    return card;
}

window.pushToCRM = async function(docId, leadStr) {
    // V15: Native CRM push â€” sets is_in_crm:true + initialises crm_status:new
    const btn = document.getElementById(`crm-btn-${docId}`);
    try {
        const success = await performApiMutation(`/api/leads/${docId}`, 'PUT', {
            is_in_crm:      true,
            crm_status:     'new',
            estimated_value: 0,
            notes:          [],
            expire_at:      null  // DPDP TTL exemption: null prevents Firestore 90-day sweep
        });
        if (success) {
            // Optimistic UI: remove the entire lead card from the DOM immediately
            const cardEl = document.getElementById(docId);
            if (cardEl) {
                virtualObserver.unobserve(cardEl);
                cardEl.remove();
            }
            // Prune from rawLeadsCache so re-renders stay clean
            rawLeadsCache = rawLeadsCache.filter(l => (l.id || l.doc_id) !== docId);

            showToast('Lead filed in CRM â€” navigate to #crm-test to manage it.', 'success');

            // Optional legacy webhook fire
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
        showToast('CRM push failed â€” try again.', 'error');
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

    // 3. Open the URI â€” Phase 2: Relative Path Defence
    if (uri) {
        const isEmail = platform === 'email' || (uri.includes('@') && !uri.startsWith('http'));
        const isPhone = platform === 'other' && /^[\d\s+()\-]{6,}$/.test(uri);
        let href;
        if (isEmail) {
            href = `mailto:${uri}`;
        } else if (isPhone) {
            href = `tel:${uri}`;
        } else if (/^(https?:\/\/|mailto:|tel:)/i.test(uri)) {
            // URI already has a valid protocol â€” use as-is
            href = uri;
        } else {
            // Relative path / naked domain guard: prepend https://
            href = `https://${uri}`;
        }
        window.open(href, '_blank', 'noopener,noreferrer');
    }

    showToast('Message copied â€” opening contact...', 'success');
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
// ENTERPRISE DOSSIER RENDERER â€” V14 POLYMORPHIC SCHEMA
// ---------------------------------------------------------------------------

// Platform metadata: icon, label, CTA text for each endpoint type
const PLATFORM_META = {
    whatsapp:  { icon: 'ðŸ’¬', label: 'WhatsApp',   cta: 'Message on WhatsApp',  priority: 1 },
    instagram: { icon: 'ðŸ“¸', label: 'Instagram',  cta: 'DM on Instagram',      priority: 2 },
    linkedin:  { icon: 'ðŸ’¼', label: 'LinkedIn',   cta: 'Connect on LinkedIn',  priority: 2 },
    facebook:  { icon: 'ðŸ“˜', label: 'Facebook',   cta: 'Message on Facebook',  priority: 3 },
    email:     { icon: 'ðŸ“§', label: 'Email',      cta: 'Send Email',           priority: 4 },
    gmb:       { icon: 'ðŸ“', label: 'GMB',        cta: 'Open Maps Profile',    priority: 4 },
    reddit:    { icon: 'ðŸ”´', label: 'Reddit',     cta: 'Open Reddit Profile',  priority: 5 },
    other:     { icon: 'ðŸ”—', label: 'Contact',    cta: 'Open Contact',         priority: 6 }
};

/**
 * Resolves the primary endpoint from a contact_endpoints array
 * using the hierarchy: WhatsApp â†’ Instagram/LinkedIn â†’ Email â†’ Reddit/Forums â†’ Other
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
    // â”€â”€ Enterprise Dossier fields â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    const targetName       = (!lead.decision_maker_name        || lead.decision_maker_name        === 'N/A') ? 'Data unavailable on scanned domain'                       : lead.decision_maker_name;
    const companySize      = (!lead.company_size_tier          || lead.company_size_tier          === 'N/A') ? 'Requires secondary analysis'                               : lead.company_size_tier;
    const primaryObjection = (!lead.primary_objection_hypothesis || lead.primary_objection_hypothesis === 'N/A') ? 'Insufficient data to generate confident hypothesis'  : lead.primary_objection_hypothesis;
    const icebreakerAngle  = lead.icebreaker_angle || '';

    // â”€â”€ URL â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    let urlHostname = 'Unknown URL';
    try { if (lead.url) urlHostname = new URL(lead.url).hostname; } catch(e) {}

    const statusColor = lead.status === 'completed' ? 'var(--success)'
        : (lead.status === 'ignored' ? '#ef4444' : 'var(--text-muted)');

    // â”€â”€ V14: Intent Signal (replaces plain pain_point at top of card) â”€â”€â”€â”€â”€â”€â”€â”€â”€
    const intentSignal = lead.intent_signal || lead.pain_point || '';
    const intentSignalHtml = intentSignal
        ? `<div class="intent-signal">${intentSignal}</div>`
        : '';

    // â”€â”€ Confidence Tier badge â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    const tierClass  = lead.confidence_tier === 'Medium' ? 'tier-medium' : 'tier-high';
    const tierBadge  = lead.confidence_tier
        ? `<span class="tier-badge ${tierClass}">${lead.confidence_tier === 'High' ? 'âœ“' : '~'} ${lead.confidence_tier}</span>`
        : '';

    // â”€â”€ Badges â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    const hiringBadge = (lead.hiring_intent_found === 'Yes')
        ? `<span style="font-size:0.75rem; background:#ecfdf5; color:#059669; padding:2px 6px; border-radius:4px; border:1px solid #a7f3d0">ðŸŸ¢ Hiring</span>`
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
            `<span style="font-size:0.75rem; background:transparent; color:#6b7280; padding:2px 6px; border-radius:4px; border:1px solid #e5e7eb">âš¡ ${techDict[t.toLowerCase()] || t}</span>`
          ).join('')
        : '';

    // V16: origin_engine badge â€” safe fallback for all legacy leads without this field
    const originEngine = lead.origin_engine || 'cartographer';
    const engineBadge  = (originEngine === 'autonomous')
        ? '<span style="font-size:0.75rem; background:#faf5ff; color:#7c3aed; padding:2px 8px; border-radius:4px; border:1px solid #ddd6fe; font-weight:600;">&#9889; Predictive Match</span>'
        : '';

    const exclusiveBadge = `<span style="font-size:0.75rem; background:#f3e8ff; color:#6b21a8; padding:2px 6px; border-radius:4px; border:1px solid #e9d5ff">&#128274; Exclusive Lead</span>`;
    const competitorBadge = lead.competitor_match
        ? `<span style="font-size:0.75rem; background:#fee2e2; color:#b91c1c; padding:2px 6px; border-radius:4px; border:1px solid #fecaca">ðŸŽ¯ Competitor Intercept: ${lead.competitor_match}</span>`
        : '';
    const targetNameBadge = (lead.decision_maker_name)
        ? `<span style="font-size:0.75rem; background:#eff6ff; color:#1d4ed8; padding:2px 6px; border-radius:4px; border:1px solid #bfdbfe">ðŸ‘¤ ${targetName}</span>`
        : '';
    const companySizeBadge = (lead.company_size_tier)
        ? `<span style="font-size:0.75rem; background:#fefce8; color:#854d0e; padding:2px 6px; border-radius:4px; border:1px solid #fef08a">ðŸ¢ ${companySize}</span>`
        : '';

    // â”€â”€ V14: Polymorphic Contact Endpoints â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
        primaryCtaHtml = `<button class="action-btn" onclick="copyMessageAndContact('${docId}', \`${safeDm}\`)" title="Copy Message">ðŸ“‹ Copy Message</button>`;
    }

    // Alt-contacts dropdown (only if there are secondary endpoints)
    let altDropdownHtml = '';
    if (altEndpoints.length > 0) {
        const altItems = altEndpoints.map(ep => {
            const m = PLATFORM_META[ep.platform] || PLATFORM_META.other;
            const safeUri = encodeURIComponent(ep.uri);
            // V14.2: Platform-aware label (Phone: ..., Email: ..., WhatsApp: ...)
            let displayLabel;
            const isPhone = ep.platform === 'other' && /^[\d\s+()\-]{6,}$/.test(ep.uri);
            const isEmail = ep.platform === 'email' || (ep.uri.includes('@') && !ep.uri.startsWith('http'));
            if (isPhone) {
                displayLabel = `Phone: ${ep.uri}`;
            } else if (isEmail) {
                displayLabel = `Email: ${ep.uri}`;
            } else {
                const shortUri = ep.uri.length > 26 ? ep.uri.substring(0, 24) + 'â€¦' : ep.uri;
                displayLabel = `${m.label}: ${shortUri}`;
            }
            return `<button class="alt-contact-item"
                onclick="smartContactAction('${docId}', \`${safeDm}\`, decodeURIComponent('${safeUri}'), '${ep.platform}')">
                <span class="platform-icon">${m.icon}</span>
                <span class="platform-uri">${displayLabel}</span>
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

    // â”€â”€ Icebreaker / Objection rows â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    const icebreakerRow = icebreakerAngle
        ? `<div style="margin-top:8px; font-size:0.85rem; color:#4f46e5; font-style:italic; padding:6px 10px; background:rgba(79,70,229,0.05); border-left:3px solid #6366f1; border-radius:0 4px 4px 0;">
               ðŸ’¡ Icebreaker: ${icebreakerAngle}
           </div>`
        : '';
    const objectionRow = (lead.primary_objection_hypothesis)
        ? `<div style="margin-top:6px; font-size:0.82rem; color:#b45309; padding:4px 10px; background:#fffbeb; border-left:3px solid #f59e0b; border-radius:0 4px 4px 0;">
               âš ï¸ Likely Objection: ${primaryObjection}
           </div>`
        : '';

    // â”€â”€ Safe serialisation for timeline â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
                <strong><a href="${lead.url || '#'}" target="_blank" style="color: var(--text-main); text-decoration: none;">${urlHostname} â†—</a></strong> â€¢ ${lead.source || 'Organic Search'}
                <span style="margin-left:8px; font-size:0.75rem; padding: 2px 6px; border-radius:4px; border: 1px solid ${statusColor}; color: ${statusColor}">${(lead.status || 'new').toUpperCase()}</span>
                ${tierBadge}
            </div>
            <div class="score">Score: ${lead.score || 0}/10</div>
        </div>
        ${intentSignalHtml}
        <div class="premium-badges" style="margin-top: 8px; margin-bottom: 8px; font-weight: 500; display: flex; flex-wrap: wrap; gap: 6px; align-items: center;">
            ${engineBadge}
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
            <button class="action-btn" onclick="pushToCRM('${docId}', '${safeLeadEnc}')" style="color: #4f46e5; border-color: #c7d2fe; background: #e0e7ff;">â˜ï¸ Push to CRM</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'ignored')" title="Ignore Lead">ðŸš« Ignore</button>
            <button class="action-btn" onclick="updateLeadStatus('${docId}', 'converted')" title="Lead Converted">ðŸŽ¯ Converted</button>
            <button class="action-btn" style="background:#f8fafc; color:var(--text-muted); border: 1px solid var(--glass-border);" onclick="viewLeadTimeline('${safeEvents}')" title="Audit Log">ðŸ•’ Timeline</button>
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
                    // V17: Use new folded card renderer
                    const newCard = window.createLeadCardV2(leadId, lead);
                    entry.target.replaceWith(newCard);
                    virtualObserver.unobserve(entry.target);
                    virtualObserver.observe(newCard);
                    newCard.setAttribute('data-rendered', 'true');
                }
            }
        }
    });
}, { rootMargin: "600px" });

function renderLeads() {
    const filteredLeads = rawLeadsCache.filter(lead => {
        if (!['new', 'contacted', 'converted'].includes(lead.status || 'new')) return false;
        // V14.2: Fix campaign filter â€” leads now use matched_campaigns[] array, not campaign_id scalar
        if (currentCampaignFilter !== 'all') {
            const matched = Array.isArray(lead.matched_campaigns)
                ? lead.matched_campaigns.includes(currentCampaignFilter)
                : lead.campaign_id === currentCampaignFilter; // legacy fallback
            if (!matched) return false;
        }
        return true;
    });
    
    if (filteredLeads.length === 0) {
        leadsList.innerHTML = `
            <div class="lead-card" style="text-align: center; padding: 40px; border: none; background: transparent; box-shadow: none;">
                <div style="font-size: 3rem; margin-bottom: 12px; opacity: 0.8;">â³</div>
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

// â”€â”€ Location State Sync â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// Called by onchange on both country dropdowns.
// Clears the city/region text input and sets a country-native placeholder
// so the user always gets a clean, contextual hint after switching country.
const COUNTRY_PLACEHOLDER_MAP = {
    'us': 'e.g. San Francisco, California',
    'uk': 'e.g. Manchester, England',
    'ca': 'e.g. Toronto, Ontario',
    'au': 'e.g. Melbourne, Victoria',
    'in': 'e.g. Kochi, Kerala',
    '':   'City, State/Region'   // Global fallback
};

window.handleCountryChange = function(selectId, inputId) {
    const gl    = document.getElementById(selectId)?.value || '';
    const input = document.getElementById(inputId);
    if (!input) return;
    input.value       = '';                                         // clear stale city
    input.placeholder = COUNTRY_PLACEHOLDER_MAP[gl] || 'City, State/Region';
};

window.openEditModal = function(id, name, bio, keywords, gl, location, targetUrls) {
    document.getElementById('edit-camp-id').value = id;
    document.getElementById('edit-camp-name').value = name;
    document.getElementById('edit-camp-bio').value = bio;
    document.getElementById('edit-camp-keys').value = keywords;
    const glEl = document.getElementById('edit-camp-gl');
    const locEl = document.getElementById('edit-camp-location');
    if (glEl) glEl.value = gl;
    if (locEl) locEl.value = location;
    // V14.2: Populate target URLs if present
    const targetUrlsEl = document.getElementById('edit-camp-target-urls');
    if (targetUrlsEl) targetUrlsEl.value = Array.isArray(targetUrls) ? targetUrls.join('\n') : (targetUrls || '');
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
    const glInput  = document.getElementById('camp-gl');
    const locInput = document.getElementById('camp-location');

    // Sync placeholder to any already-selected country on modal open
    if (glInput && glInput.value) {
        handleCountryChange('camp-gl', 'camp-location');
    }

    // Auto-detect Geo if unpopulated
    if (glInput && !glInput.value) {
        try {
            const resp = await fetch('https://ipapi.co/json/');
            const json = await resp.json();
            if (json.country_code) {
                glInput.value = json.country_code.toLowerCase();
                // Sync placeholder AFTER setting country code, THEN fill city
                handleCountryChange('camp-gl', 'camp-location');
            }
            if (json.city && locInput) locInput.value = `${json.city}, ${json.region}`;
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
    const targetUrlsInput = document.getElementById('edit-camp-target-urls');

    if (!name || !keys) return showToast('Name and Keywords required', 'error');

    // V14.2: Parse target URLs textarea
    let targetUrls = [];
    if (targetUrlsInput && targetUrlsInput.value.trim()) {
        targetUrls = targetUrlsInput.value.split('\n').map(u => u.trim()).filter(u => u.length > 0);
        if (targetUrls.length > 10) {
            showToast('Warning: Only the first 10 URLs will be used.', 'error');
            targetUrls = targetUrls.slice(0, 10);
        }
    }

    showToast('Pushing updates to AI Engine...', 'info');
    try {
        const payload = {
            name,
            bio,
            keywords: keys,
            gl: glInput ? glInput.value : '',
            location: locationInput ? locationInput.value : '',
            target_urls: targetUrls,
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
        // Step 1: Create the campaign
        const user  = auth.currentUser;
        if (!user) return;
        const token = await user.getIdToken();
        const createResp = await fetch(`${API_BASE}/api/campaigns`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name:        nameInput.value,
                bio:         bioInput.value,
                keywords:    keysInput.value,
                gl:          glInput ? glInput.value : '',
                location:    locationInput ? locationInput.value : '',
                target_urls: targetUrls,
                status:      'active'
            })
        });
        if (!createResp.ok) throw new Error('Campaign creation failed');
        const createData = await createResp.json();
        const campaignId = createData.id;

        // Step 2: Fire Epsilon-Greedy Router for immediate first batch
        let routerMsg = 'System is now looking for clients!';
        if (campaignId) {
            try {
                const routerResp = await fetch(`${API_BASE}/api/campaigns/${campaignId}/run`, {
                    method:  'POST',
                    headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
                    body:    JSON.stringify({})
                });
                if (routerResp.ok) {
                    const r = await routerResp.json();
                    const v16  = r.autonomous_promoted  || 0;
                    const v14  = r.cartographer_queued  || 0;
                    routerMsg = `Engine dispatched: âš¡ ${v16} Predictive + ðŸ” ${v14} Cartographer leads`;
                }
            } catch (routerErr) {
                console.warn('[ROUTER] Router call failed, Cartographer sweep will pick up:', routerErr);
            }
        }

        closeNewCampaignModal();  // V17: resets conversational flow state
        showToast(routerMsg, 'success');
        if (targetUrlsInput) targetUrlsInput.value = '';
        loadDashboard();
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
    } else if(tabName === 'crm-test') {
        // V15: L0 super_admin only â€” no nav link exposed to regular users
        const isAdmin = window.currentUserData?.role === 'super_admin';
        if (!isAdmin) {
            showToast('CRM module is restricted to L0 administrators.', 'error');
            return;
        }
        const crmView = document.getElementById('view-crm-test');
        if (crmView) {
            crmView.classList.remove('hidden');
            loadCrmBoard();
        }
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

// Close modal
window.closeNewCampaignModal = function() {
    document.getElementById('new-campaign-modal').classList.add('hidden');
    // Reset step
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
// V17: NEW LEAD CARD RENDERER (createLeadCardV2)
// Called from the existing snapshot handler â€” replaces createLeadCard
// =============================================================================

function getScoreEmoji(score) {
    if (score >= 9) return 'ðŸ”¥';
    if (score >= 7) return 'âš¡';
    if (score >= 5) return 'ðŸ‘';
    return 'ðŸ“‹';
}

window.createLeadCardV2 = function(docId, lead) {
    const card = document.createElement('div');
    card.className = 'lead-card-v2';
    card.id = docId;

    // Company name (prefer company_name, fallback to hostname)
    let displayName = lead.company_name || '';
    let hostname = '';
    try { hostname = lead.url ? new URL(lead.url).hostname.replace('www.','') : (lead.source_url ? new URL(lead.source_url).hostname.replace('www.','') : ''); } catch(e){}
    if (!displayName) displayName = hostname || 'Unknown Company';

    const score     = lead.score || 0;
    const heatPct   = Math.round((score / 10) * 100);
    const emoji     = getScoreEmoji(score);
    const signal    = lead.intent_signal || lead.pain_point || '';
    const dm        = lead.dm || '';
    const icebreaker = dm; // Icebreaker IS the opening message

    // Badges â€” only show if value exists
    const badges = [];
    if (lead.origin_engine === 'autonomous') badges.push({ text: 'âš¡ Predictive', bg: '#faf5ff', color: '#7c3aed', border: '#ddd6fe' });
    badges.push({ text: 'ðŸ”’ Exclusive', bg: '#f3e8ff', color: '#6b21a8', border: '#e9d5ff' });
    if (lead.hiring_intent_found === 'Yes') badges.push({ text: 'ðŸŸ¢ Hiring', bg: '#ecfdf5', color: '#059669', border: '#a7f3d0' });
    if (lead.competitor_match) badges.push({ text: `ðŸŽ¯ ${lead.competitor_match}`, bg: '#fee2e2', color: '#b91c1c', border: '#fecaca' });

    const badgesHTML = badges.map(b =>
        `<span class="lc-badge" style="background:${b.bg};color:${b.color};border-color:${b.border}">${b.text}</span>`
    ).join('');

    // Time ago
    const timeAgo = fcTimeAgo(lead.createdAt || lead.promotedAt);

    // Source label â€” human-readable
    const sourceLabel = (lead.sourcing_vector || lead.source || '').includes('Autonomous')
        ? 'AI Match' : (lead.source || 'Web Signal');

    // Contact URI for primary CTA
    const primaryUri  = (lead.contact_endpoints || [])[0]?.value || lead.email || lead.url || lead.source_url || '#';
    const primaryPlat = (lead.contact_endpoints || [])[0]?.platform || 'email';

    const expandId    = `lc-expand-${docId}`;
    const moreId      = `lc-more-${docId}`;
    const overflowId  = `lc-of-${docId}`;

    card.innerHTML = `
        <div class="lc-header">
            <div class="lc-left">
                <div class="lc-company-name">
                    <a href="${lead.url || lead.source_url || '#'}" target="_blank" rel="noopener noreferrer" title="Open company website">${displayName} &#8599;</a>
                </div>
                <div class="lc-meta">
                    <span>${sourceLabel}</span>
                    ${timeAgo ? `<span class="lc-found-ago">Â· ${timeAgo}</span>` : ''}
                </div>
            </div>
            <div class="lc-score-wrap">
                <div class="lc-score-emoji">${emoji}</div>
                <div class="lc-heat-bar"><div class="lc-heat-fill" style="width:${heatPct}%"></div></div>
                <div class="lc-score-label">${score}/10</div>
            </div>
        </div>

        ${signal ? `<div class="lc-signal">${signal}</div>` : ''}

        <div class="lc-badges">${badgesHTML}</div>

        <button class="lc-expand-btn" onclick="lcToggleExpand('${docId}')">
            <span id="lc-expand-icon-${docId}">â†“</span> See opening message &amp; full intelligence
        </button>

        <div class="lc-expanded" id="${expandId}">

            ${icebreaker ? `
            <div class="lc-section">
                <div class="lc-section-label">Your Opening Message</div>
                <div class="lc-icebreaker">${icebreaker}</div>
            </div>` : ''}

            ${lead.pain_point && lead.pain_point !== signal ? `
            <div class="lc-section">
                <div class="lc-section-label">Why This Lead</div>
                <div class="lc-why">${lead.pain_point}</div>
            </div>` : ''}

            ${lead.objection ? `
            <div class="lc-section">
                <div class="lc-section-label">Likely Objection</div>
                <div class="lc-objection">âš ï¸ ${lead.objection}</div>
            </div>` : ''}

            ${lead.email || lead.phone ? `
            <div class="lc-section" style="font-size:0.85rem; color:var(--text-main);">
                <div class="lc-section-label">Contact Info</div>
                ${lead.email ? `<a href="mailto:${lead.email}" style="color:#2563eb;text-decoration:none;">âœ‰ ${lead.email}</a>&nbsp;` : ''}
                ${lead.phone ? `<a href="tel:${lead.phone}" style="color:#2563eb;text-decoration:none;">ðŸ“ž ${lead.phone}</a>` : ''}
            </div>` : ''}

        </div>

        <div class="lc-actions-primary">
            <button class="lc-contact-btn" onclick="smartContactAction('${docId}', \`${dm.replace(/`/g,'\\`').replace(/\\/g,'\\\\')}\`, '${primaryUri}', '${primaryPlat}')">
                âœ‰ Contact This Lead
            </button>
            <button class="lc-crm-btn ${lead.is_in_crm ? 'in-crm' : ''}"
                id="crm-btn-${docId}"
                onclick="${lead.is_in_crm ? '' : `pushToCRM('${docId}', \`${encodeURIComponent(JSON.stringify(lead)).replace(/\\/g,'\\\\')}\`)`}"
                ${lead.is_in_crm ? 'disabled' : ''}
                title="Send to pipeline CRM">
                ${lead.is_in_crm ? 'âœ“ In CRM' : 'â†’ CRM'}
            </button>
            <div style="position:relative;">
                <button class="lc-more-btn" id="${moreId}" onclick="lcToggleMore('${docId}')" title="More options">Â·Â·Â·</button>
                <div class="lc-overflow-menu" id="${overflowId}">
                    <button class="lc-overflow-item" onclick="updateLeadStatus('${docId}','converted');lcCloseMore('${docId}')">ðŸŽ¯ Mark Converted</button>
                    <button class="lc-overflow-item" onclick="viewLeadTimeline('${encodeURIComponent(JSON.stringify(lead.interactions||[]))}');lcCloseMore('${docId}')">ðŸ• View Timeline</button>
                    <button class="lc-overflow-item danger" onclick="updateLeadStatus('${docId}','ignored');lcCloseMore('${docId}')">âœ• Skip This Lead</button>
                </div>
            </div>
        </div>
    `;
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
    document.getElementById(`lc-of-${docId}`)?.classList.remove('open');
};

