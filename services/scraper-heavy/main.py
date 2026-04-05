import os
import asyncio
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright

app = Flask(__name__)

async def fetch_page_content(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(url, timeout=30000, wait_until="networkidle")
            
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
    text, contacts = loop.run_until_complete(fetch_page_content(url))
    
    # Return up to 100k chars to ensure we don't blow up memory/firebase
    return jsonify({"text": text[:100000], "emails": contacts.get("emails", []), "phones": contacts.get("phones", [])}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
