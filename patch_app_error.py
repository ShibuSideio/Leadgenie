import os

with open('public/app.js', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
in_load_me = False
in_load_campaigns = False
in_fetch_tenant_profile = False

for line in lines:
    if 'async function loadMe()' in line:
        in_load_me = True
    elif 'async function loadCampaigns()' in line:
        in_load_campaigns = True
    elif 'async function fetchTenantProfile()' in line:
        in_fetch_tenant_profile = True
        
    if 'await response.json()' in line or 'await res.json()' in line:
        if in_load_me:
            new_lines.extend([
                "        if (!response.ok) {\n",
                "            console.error('Backend Error (loadMe):', await response.text());\n",
                "            return;\n",
                "        }\n"
            ])
            in_load_me = False
        elif in_load_campaigns:
            new_lines.extend([
                "        if (!response.ok) {\n",
                "            console.error('Backend Error (loadCampaigns):', await response.text());\n",
                "            if (tableBody) tableBody.innerHTML = '<tr><td colspan=\"4\" class=\"empty-state\">Failed to load campaigns.</td></tr>';\n",
                "            return;\n",
                "        }\n"
            ])
            in_load_campaigns = False
        elif in_fetch_tenant_profile:
            new_lines.extend([
                "        if (!response.ok) {\n",
                "            console.error('Backend Error (fetchTenantProfile):', await response.text());\n",
                "            return null;\n",
                "        }\n"
            ])
            in_fetch_tenant_profile = False

    new_lines.append(line)
    
with open('public/app.js', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
print('Frontend error handling patched')
