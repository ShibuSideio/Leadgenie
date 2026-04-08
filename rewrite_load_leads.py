import re

with open('public/app.js', 'r', encoding='utf-8') as f:
    text = f.read()

old_func = """// Load Leads Real-Time (Thin Client API)
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
        
    } catch (error) {
        console.error("Fetch API Listener Error:", error);
        leadsList.innerHTML = '<div class="lead-card" style="color: #ef4444; border-color: #ef4444;">Could not connect to API Gateway. Please check backend health.</div>';
        showToast('Connection Refused', 'error');
    }
}"""

new_func = """// Load Leads Real-Time (Firestore Subscription)
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
}"""

if old_func in text:
    text = text.replace(old_func, new_func)
    print("Function replaced.")
else:
    print("Function pattern not found.")

with open('public/app.js', 'w', encoding='utf-8') as f:
    f.write(text)
