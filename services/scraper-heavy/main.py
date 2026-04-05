import os
import asyncio
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright
from google.cloud import tasks_v2
import json

app = Flask(__name__)

async def fetch_page_content(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--single-process", "--no-sandbox", "--no-zygote"])
        # ignore_https_errors bypasses weak SME SSL cert domains; bypass_csp prevents local scripts locking up headless eval
        context = await browser.new_context(ignore_https_errors=True, bypass_csp=True)
        page = await context.new_page()
        try:
            # Block heavy non-text resources to prevent OOM kills
            await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font", "stylesheet"] else route.continue_())
            
            # Enforce strict 15s page load timeout
            await page.goto(url, timeout=15000, wait_until="domcontentloaded")
            
            # Remove scripts, styles for clean text extraction
            await page.evaluate("""() => {
                document.querySelectorAll('script, style, noscript, nav, footer, iframe').forEach(el => el.remove());
            }""")

            # Contact Harvesting
            contacts = await page.evaluate("""() => {
                let emails = new Set();
                let phones = new Set();
                document.querySelectorAll('a[href^="mailto:"]').forEach(a => {
                    let email = a.href.replace('mailto:', '').split('?')[0].trim();
                    if (email) emails.add(email);
                });
                document.querySelectorAll('a[href^="tel:"]').forEach(a => {
                    let phone = a.href.replace('tel:', '').replace(/[^\\d+]/g, '').trim();
                    if (phone) phones.add(phone);
                });
                return { emails: Array.from(emails), phones: Array.from(phones) };
            }""")

            text = await page.evaluate("() => document.body.innerText")
            return text, contacts
        except Exception as e:
            print(f"Error scraping {url}: {str(e)}")
            return "", {"emails": [], "phones": []}
        finally:
            await browser.close()

@app.route("/scrape", methods=["POST"])
def scrape():
    data = request.json
    url = data.get("url")
    if not url:
        return jsonify({"error": "No URL provided"}), 400
        
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # Wrap the entire Playwright lifecycle in an inescapable kill switch (20s)
        text, contacts = loop.run_until_complete(asyncio.wait_for(fetch_page_content(url), timeout=20.0))
    except Exception as e:
         print(f"Playwright hard kill-switch activated on {url} due to total process freeze: {e}")
         text, contacts = "", {}
    finally:
         loop.close()
    
    # Decoupled Webhook Callback via Cloud Tasks
    payload = {
        "text": text[:100000] if text else "",
        "emails": contacts.get("emails", []) if contacts else [],
        "phones": contacts.get("phones", []) if contacts else [],
        "lead_id": data.get("lead_id"),
        "tenant_id": data.get("tenant_id"),
        "campaign_id": data.get("campaign_id"),
        "bio": data.get("bio"),
        "url": url,
        "target_domain": data.get("target_domain"),
        "preferences_weights": data.get("preferences_weights", {})
    }
    
    try:
        tasks_client = tasks_v2.CloudTasksClient()
        # Fallback to defaults since env vars might be loose in scraper-heavy initially
        project = os.environ.get("PROJECT_ID", "sideio-leads-v16")
        location = os.environ.get("LOCATION", "asia-south1")
        queue = os.environ.get("QUEUE", "lead-pipeline-queue")
        parent = tasks_client.queue_path(project, location, queue)
        
        base_url = os.environ.get("PIPELINE_BASE_URL", "https://lead-pipeline-main-abc.a.run.app")
        
        task_def = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{base_url}/finalize",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload).encode()
            }
        }
        tasks_client.create_task(parent=parent, task=task_def)
    except Exception as hook_e:
        print(f"Failed to queue finalize webhook: {hook_e}")

    return jsonify({"status": "queued_to_finalize"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
