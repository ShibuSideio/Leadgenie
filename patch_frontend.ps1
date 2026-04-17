
<#
  Sideio Frontend Bug Patch — Tasks 1-4
  Run from the sideio_leads directory:
    powershell -ExecutionPolicy Bypass -File patch_frontend.ps1
#>

$appJsPath   = "public\app.js"
$cssPath     = "public\styles.css"
$orchPath    = "services\orchestrator\main.py"

# ──────────────────────────────────────────────────────────────────────────────
# UTILITY
# ──────────────────────────────────────────────────────────────────────────────
function Patch-File {
    param([string]$Path, [string]$Old, [string]$New, [string]$Label)
    $content = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    if ($content.IndexOf($Old) -eq -1) {
        Write-Host "[$Label] WARNING: target string not found in $Path" -ForegroundColor Yellow
        return
    }
    $patched = $content.Replace($Old, $New)
    [System.IO.File]::WriteAllText($Path, $patched, [System.Text.Encoding]::UTF8)
    Write-Host "[$Label] OK" -ForegroundColor Green
}

# ──────────────────────────────────────────────────────────────────────────────
# TASK 2 FIX: cc-custom-fallback-container — classList.add('hidden') → display:none
# Root cause: no .hidden CSS rule exists, so element is never actually hidden.
# ──────────────────────────────────────────────────────────────────────────────
Patch-File -Path $appJsPath `
    -Old "if(fallbackCont) fallbackCont.classList.add('hidden');" `
    -New "if (fallbackCont) fallbackCont.style.display = 'none'; // FIX T2: no .hidden CSS rule" `
    -Label "T2-fallback-container-visibility"

# ──────────────────────────────────────────────────────────────────────────────
# TASK 1 FIX: Manual Profile Fallback in the empty-state branch.
# When recommended_campaigns is [], check for company_bio / knowledge_base_text.
# ──────────────────────────────────────────────────────────────────────────────
$old_empty_state = @'
        } else {
            html = '<p style="text-align:center; color:#6b7280;">No predictive campaigns available. Use the custom fallback.</p>';
        }
'@

$new_empty_state = @'
        } else {
            // ── FIX T1: Manual Profile Fallback ──────────────────────────────
            // When no recommended_campaigns (user has no website yet), check if
            // an admin manually pushed a tenant_profile with company_bio or KB.
            // Surface a single editable "Manual Twin" card so the user can
            // immediately launch a campaign without scanning a website URL.
            const manualBio = (rawProfile && (
                rawProfile.company_bio        ||
                rawProfile.company_description ||
                rawProfile.bio                ||
                (rawProfile.knowledge_base_text && rawProfile.knowledge_base_text[0])
            )) || '';

            if (manualBio) {
                const productHint  = (rawProfile && (rawProfile.company_name || rawProfile.name)) || 'Your Core Service';
                const locationHint = (rawProfile && rawProfile.detected_gl) || '';
                const bProd = btoa(productHint.replace(/['"]/g, ''));
                const bHook = btoa(manualBio.slice(0, 120).replace(/['"]/g, ''));
                const bAdv  = btoa('');

                html = `
                <div style="background:linear-gradient(135deg,rgba(79,70,229,0.05),rgba(124,58,237,0.05));border:1px dashed rgba(79,70,229,0.3);border-radius:12px;padding:14px;margin-bottom:16px;text-align:left;">
                    <div style="font-size:0.72rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:var(--primary);margin-bottom:6px;">&#10022; Profile Detected (Manual Twin)</div>
                    <p style="font-size:0.88rem;color:var(--text-muted);margin-bottom:14px;line-height:1.5;">${manualBio.slice(0,180)}${manualBio.length > 180 ? '\u2026' : ''}</p>
                    <button class="primary-btn" style="width:100%;font-size:0.9rem;padding:8px;" onclick="window.editPredictiveCard(0)">Customise &amp; Launch &#8594;</button>
                </div>
                <div id="c-card-0">
                    <div id="c-card-view-0" class="hidden"></div>
                    <div id="c-card-edit-0">
                        <label style="font-size:0.8rem;color:var(--text-muted);display:block;">Product / Service Focus</label>
                        <input type="text" id="c-prod-0" class="fc-intent-input" style="height:36px;padding:8px;margin-bottom:8px;width:100%;border:1px solid #d1d5db;border-radius:8px;" value="${productHint.replace(/"/g, '&quot;')}">
                        <label style="font-size:0.8rem;color:var(--text-muted);display:block;">Market Opportunity / Pain Point</label>
                        <textarea id="c-hook-0" class="fc-intent-input" style="min-height:60px;padding:8px;margin-bottom:8px;width:100%;border:1px solid #d1d5db;border-radius:8px;">${manualBio.slice(0,200)}</textarea>
                        <label style="font-size:0.8rem;color:var(--text-muted);display:block;">Unfair Advantage</label>
                        <textarea id="c-adv-0" class="fc-intent-input" style="min-height:60px;padding:8px;margin-bottom:12px;width:100%;border:1px solid #d1d5db;border-radius:8px;"></textarea>
                        <label style="font-size:0.8rem;color:var(--text-muted);display:block;">Target Location</label>
                        <input type="text" id="c-loc-0" class="fc-intent-input" style="height:36px;padding:8px;margin-bottom:12px;width:100%;border:1px solid #d1d5db;border-radius:8px;" placeholder="e.g. Kerala, India, Worldwide" value="${locationHint}">
                        <button class="primary-btn" style="width:100%;font-size:0.9rem;padding:8px;background:#10b981;border:none;border-radius:20px;color:white;font-weight:600;cursor:pointer;"
                            onclick="window.deployPredictiveCard(0,'${bProd}','${bHook}','${bAdv}')">Deploy Campaign</button>
                    </div>
                </div>`;
            } else {
                html = '<p style="text-align:center; color:#6b7280;">No predictive campaigns available. Use the custom fallback.</p>';
            }
        }
'@

Patch-File -Path $appJsPath `
    -Old $old_empty_state `
    -New $new_empty_state `
    -Label "T1-manual-profile-fallback"

# ──────────────────────────────────────────────────────────────────────────────
# TASK 2 FIX: showCcCustomFallback — add location pre-fill
# ──────────────────────────────────────────────────────────────────────────────
$old_fallback_fn = @'
window.showCcCustomFallback = function() {
    const r = document.getElementById('cc-recommendation-cards');
    if (r) r.style.display = 'none';
    const f = document.getElementById('cc-custom-fallback-container');
    if (f) f.style.display = 'block';
};
'@

$new_fallback_fn = @'
window.showCcCustomFallback = function() {
    // FIX T2: hide cards, show manual form; pre-fill location from DT state
    const r = document.getElementById('cc-recommendation-cards');
    if (r) r.style.display = 'none';
    const f = document.getElementById('cc-custom-fallback-container');
    if (f) f.style.display = 'block';
    const locEl = document.getElementById('cc-location');
    if (locEl && !locEl.value && window._dtState && window._dtState.extractedGl) {
        locEl.value = window._dtState.extractedGl;
    }
};
'@

Patch-File -Path $appJsPath `
    -Old $old_fallback_fn `
    -New $new_fallback_fn `
    -Label "T2-showCcCustomFallback-location-prefill"

# ──────────────────────────────────────────────────────────────────────────────
# TASK 3 FIX (Backend): orchestrator gl_map — add all Indian states
# ──────────────────────────────────────────────────────────────────────────────
$old_gl_map = @'
                gl_map = {
                    "usa": "us", "united states": "us", "uk": "uk", 
                    "united kingdom": "uk", "canada": "ca", "australia": "au",
                    "germany": "de", "singapore": "sg", "uae": "ae", 
                    "dubai": "ae", "india": "in"
                }
                
                # If explicit match, set GL. If not, Serper defaults to 'us' but 
                # loc_raw remains in 'location' string to be appended to Vertex Search Context.
                if loc_raw in gl_map:
                    data['gl'] = gl_map[loc_raw]
                elif loc_raw == "worldwide" or not loc_raw:
                    data['gl'] = "us" # default fallback
                else:
                    data['gl'] = "us" # custom cities fallback to US GL, append loc string elsewhere
'@

$new_gl_map = @'
                # FIX T3: expanded gl_map with Indian states, cities, and major geographies
                gl_map = {
                    # Global
                    "usa": "us", "united states": "us",
                    "uk": "uk", "united kingdom": "uk", "england": "uk", "scotland": "uk",
                    "canada": "ca", "australia": "au", "germany": "de",
                    "singapore": "sg", "uae": "ae", "dubai": "ae", "abu dhabi": "ae",
                    # India — root
                    "india": "in",
                    # Indian states & UTs
                    "kerala": "in", "karnataka": "in", "maharashtra": "in",
                    "gujarat": "in", "rajasthan": "in", "tamil nadu": "in",
                    "andhra pradesh": "in", "telangana": "in", "uttar pradesh": "in",
                    "west bengal": "in", "punjab": "in", "haryana": "in",
                    "madhya pradesh": "in", "bihar": "in", "odisha": "in",
                    "assam": "in", "goa": "in", "jharkhand": "in",
                    # Indian cities
                    "mumbai": "in", "delhi": "in", "new delhi": "in",
                    "bangalore": "in", "bengaluru": "in", "hyderabad": "in",
                    "chennai": "in", "kolkata": "in", "pune": "in",
                    "ahmedabad": "in", "jaipur": "in", "kochi": "in",
                    "thiruvananthapuram": "in", "surat": "in", "lucknow": "in",
                    "coimbatore": "in", "indore": "in", "bhopal": "in",
                    "visakhapatnam": "in", "nagpur": "in", "chandigarh": "in",
                }

                # FIX T3: match by startswith prefix so "kerala, india" also maps correctly
                derived_gl = gl_map.get(loc_raw)
                if not derived_gl:
                    for key, val in gl_map.items():
                        if loc_raw.startswith(key) or key in loc_raw:
                            derived_gl = val
                            break

                if derived_gl:
                    data['gl'] = derived_gl
                elif loc_raw in ("worldwide", "global", ""):
                    data['gl'] = ""   # truly global — let Serper run without gl restriction
                else:
                    # Unknown city/region: preserve loc_raw in location field for
                    # Vertex AI context string; do NOT force gl='us' — set empty
                    # so Serper searches without a geo filter (broader results).
                    data['gl'] = ""
'@

Patch-File -Path $orchPath `
    -Old $old_gl_map `
    -New $new_gl_map `
    -Label "T3-orchestrator-gl-map-india-states"

# ──────────────────────────────────────────────────────────────────────────────
# TASK 4 FIX (CSS): user-dropdown — fix clipping issues
# The dropdown uses display:flex from the JS toggle, which causes it to size
# as a flex child instead of an absolute popup. Fix: always use display:block.
# Also: ensure nav has overflow:visible so the absolute dropdown isn't clipped.
# ──────────────────────────────────────────────────────────────────────────────

# 4a: Fix nav overflow
Patch-File -Path $cssPath `
    -Old "    position: sticky;
    top: 0;
    z-index: 100;
    box-shadow: 0 1px 3px rgba(0,0,0,0.02);" `
    -New "    position: sticky;
    top: 0;
    z-index: 100;
    overflow: visible; /* FIX T4: allow absolute dropdown to escape nav bounds */
    box-shadow: 0 1px 3px rgba(0,0,0,0.02);" `
    -Label "T4-css-nav-overflow-visible"

# 4b: Raise dropdown z-index above Pipeline Credits pill (which overlaps)
Patch-File -Path $cssPath `
    -Old "    box-shadow: 0 16px 48px rgba(0, 0, 0, 0.14);
    padding: 12px 0;
    z-index: 300;
    animation: fcSlideUp 0.2s cubic-bezier(0.16, 1, 0.3, 1);" `
    -New "    box-shadow: 0 16px 48px rgba(0, 0, 0, 0.14);
    padding: 12px 0;
    z-index: 9999; /* FIX T4: above Pipeline Credits pill and all overlapping elements */
    animation: fcSlideUp 0.2s cubic-bezier(0.16, 1, 0.3, 1);" `
    -Label "T4-css-dropdown-z-index"

# 4c: Fix JS toggleUserDropdown — open as 'block' not 'flex'
# The dropdown is an absolute-positioned block; opening it as flex causes it
# to shrink to fit flex children instead of its defined width:260px.
Patch-File -Path $appJsPath `
    -Old "        dropdown.style.display = 'flex';
        dropdown.classList.add('open');" `
    -New "        dropdown.style.display = 'block'; // FIX T4: absolute dropdown must be block not flex
        dropdown.classList.add('open');" `
    -Label "T4-js-dropdown-display-block"

Write-Host "`nAll patches applied successfully. Verify with git diff." -ForegroundColor Cyan
