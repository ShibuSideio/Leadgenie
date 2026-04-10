import os

with open('public/app.js', 'r', encoding='utf-8') as f:
    text = f.read()

target_save_child_camp = '''window.saveChildCampaign = function() {
    const nameInput = document.getElementById('cc-name');
    const name = nameInput?.value.trim() || 'Untitled Campaign';

    document.getElementById('child-campaign-modal')?.classList.add('hidden');

    saveCampaignAction({
        name: name,
        bio: 'CHILD_CAMPAIGN_OVERRIDE', // Backend relies on Master Twin bio, this signals it is a child
        keywords: name.substring(0, 150),
        gl: '',
        location: '',
        target_urls: []
    });
};'''

new_save_child_camp = '''window.saveChildCampaign = function() {
    const focusEl = document.getElementById('cc-focus');
    const painEl = document.getElementById('cc-pain');
    const advEl = document.getElementById('cc-advantage');
    
    const focus = focusEl?.value.trim() || 'Custom Campaign';
    const pain = painEl?.value.trim() || '';
    const adv = advEl?.value.trim() || '';

    document.getElementById('child-campaign-modal')?.classList.add('hidden');

    // Guardrail 2: Route distinctly, DO NOT concat into keywords
    saveCampaignAction({
        name: focus,
        bio: 'CHILD_CAMPAIGN_OVERRIDE',
        keywords: '', // Clear legacy keywords reliance
        campaign_focus: focus,
        pain_point: pain,
        unfair_advantage: adv,
        gl: '',
        location: '',
        target_urls: []
    });
};'''

if 'const nameInput = document.getElementById(\'cc-name\');' in text:
    text = text.replace(target_save_child_camp, new_save_child_camp)


business_profile_code = '''

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
'''

if 'window.openBusinessProfile' not in text:
    text += business_profile_code

with open('public/app.js', 'w', encoding='utf-8') as f:
    f.write(text)
print("app.js patched")
