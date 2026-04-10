import os

with open('public/index.html', 'r', encoding='utf-8') as f:
    text = f.read()


# 1. Add Firebase Storage
if 'firebase-storage.js' not in text:
    target = '<script src="https://www.gstatic.com/firebasejs/8.10.1/firebase-firestore.js"></script>'
    new_scripts = target + '\n    <script src="https://www.gstatic.com/firebasejs/8.10.1/firebase-storage.js"></script>'
    text = text.replace(target, new_scripts)

# 2. Add Business Profile Button to Navbar
if 'Business Profile' not in text:
    target = '<div class="fc-wallet-display" id="global-wallet-btn" onclick="javascript:document.getElementById(\'wallet-modal\').classList.remove(\'hidden\')">'
    new_btn = '''<button class="secondary-btn" style="margin-right: 16px; padding: 6px 12px; font-size: 0.9rem;" onclick="window.openBusinessProfile()">⚙️ Business Profile</button>
            ''' + target
    text = text.replace(target, new_btn)

# 3. Add Business Profile Modal
if 'business-profile-modal' not in text:
    target_modal = '<!-- Modals -->'
    modal_html = '''<!-- Modals -->
    
    <!-- Business Profile Modal -->
    <div class="fc-modal-overlay hidden" id="business-profile-modal">
        <div class="fc-modal-content" style="max-width: 600px; text-align: left;">
            <button class="fc-close-btn" onclick="document.getElementById('business-profile-modal').classList.add('hidden')">&#10005;</button>
            <h2 class="fc-headline" style="font-size: 1.8rem; margin-bottom: 16px;">⚙️ Business Profile (Master Twin)</h2>
            <div id="bp-view-mode">
                <p><strong>Company Bio:</strong> <span id="bp-bio-disp"></span></p>
                <p><strong>Target Keywords:</strong> <span id="bp-keys-disp"></span></p>
                <div style="margin-top:20px; display:flex; gap:12px;">
                    <button class="primary-btn" onclick="document.getElementById('bp-view-mode').classList.add('hidden'); document.getElementById('bp-edit-mode').classList.remove('hidden');">Edit Profile</button>
                </div>
            </div>
            
            <div id="bp-edit-mode" class="hidden">
                <label class="fc-label">Company Bio</label>
                <textarea id="bp-bio-edit" class="fc-intent-input" style="min-height:80px;"></textarea>
                
                <label class="fc-label">Target Keywords</label>
                <textarea id="bp-keys-edit" class="fc-intent-input" style="min-height:80px;"></textarea>
                
                <div style="margin-top:20px; display:flex; gap:12px;">
                    <button class="primary-btn" onclick="window.saveBusinessProfile()">Save Changes</button>
                    <button class="secondary-btn" onclick="document.getElementById('bp-edit-mode').classList.add('hidden'); document.getElementById('bp-view-mode').classList.remove('hidden');">Cancel</button>
                </div>
            </div>
            
            <div style="margin-top: 32px; padding-top: 24px; border-top: 1px solid var(--glass-border);">
                <h3 style="font-size: 1.2rem; margin-bottom: 12px;">Phase 1 Augmentation: Knowledge Base</h3>
                <p style="font-size:0.9rem; color:var(--text-muted); margin-bottom:12px;">Upload PDFs or Text files (e.g., Sales scripts, Product Catalogs) to enhance Vertex AI's context.</p>
                <input type="file" id="bp-kb-upload" accept=".pdf,.txt" style="margin-bottom:12px;" />
                <button class="primary-btn" style="background:var(--accent); width:100%;" onclick="window.uploadKnowledgeBase()">Upload & Extract</button>
            </div>
        </div>
    </div>
'''
    text = text.replace(target_modal, modal_html)

# 4. Modify Child Campaign Modal (Copilot Mind-Map fallback)
target_fallback = '''                <div id="cc-custom-fallback-container" class="hidden">
                    <div class="fc-input-wrap" style="text-align: left; margin-bottom: 24px;">
                        <label class="fc-label">Custom Campaign Name</label>
                        <input type="text" id="cc-name" class="fc-intent-input" placeholder="e.g. SEO Audit Push" />
                    </div>
                    <button class="primary-btn full-width" onclick="window.saveChildCampaign()">Launch Campaign</button>
                </div>'''

new_fallback = '''                <div id="cc-custom-fallback-container" class="hidden">
                    <div class="fc-input-wrap" style="text-align: left; margin-bottom: 16px;">
                        <label class="fc-label">1. Product Focus</label>
                        <input type="text" id="cc-focus" class="fc-intent-input" placeholder="e.g. Enterprise SEO Audit" />
                    </div>
                    <div class="fc-input-wrap" style="text-align: left; margin-bottom: 16px;">
                        <label class="fc-label">2. Core Pain Point</label>
                        <textarea id="cc-pain" class="fc-intent-input" placeholder="What keeps them up at night?"></textarea>
                    </div>
                    <div class="fc-input-wrap" style="text-align: left; margin-bottom: 24px;">
                        <label class="fc-label">3. Unfair Advantage</label>
                        <textarea id="cc-advantage" class="fc-intent-input" placeholder="Why should they choose you?"></textarea>
                    </div>
                    <button class="primary-btn full-width" onclick="window.saveChildCampaign()">Launch Campaign</button>
                </div>'''

if 'cc-focus' not in text:
    text = text.replace(target_fallback, new_fallback)

with open('public/index.html', 'w', encoding='utf-8') as f:
    f.write(text)
print("index.html patched")
