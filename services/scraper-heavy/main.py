import os
import asyncio
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright
from google.cloud import tasks_v2
from google.cloud import secretmanager
import json

app = Flask(__name__)

# =============================================================================
# FIX 04B/C: MODULE-LEVEL SINGLETON CLIENTS
# Previously these were instantiated on every request (inside get_secret() and
# inside scrape()). Each instantiation creates a new gRPC channel + TLS
# handshake, accumulating connection objects across warm invocations and
# adding latency directly into Playwright's 20-second kill-switch budget.
#
# Now: one client per container lifecycle (cold start). Warm invocations reuse
# the existing gRPC channels with no TLS overhead.
# =============================================================================
_SM_CLIENT    = secretmanager.SecretManagerServiceClient()
_TASKS_CLIENT = tasks_v2.CloudTasksClient()

# FIX 04B: Secret cache — proxy credentials are immutable for the container
# lifetime. Caching avoids 2× Secret Manager RPC calls per scrape invocation
# and stays within Secret Manager's API rate limits under burst traffic.
_SECRET_CACHE: dict = {}


def get_secret(secret_id: str) -> str:
    """
    Returns the secret value from cache (warm hit) or Secret Manager (cold hit).
    Re-raises on Secret Manager failure — caller must handle gracefully.
    """
    if secret_id in _SECRET_CACHE:
        return _SECRET_CACHE[secret_id]
    project_id = os.environ.get("PROJECT_ID", "sideio-leads-v16")
    try:
        name     = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        response = _SM_CLIENT.access_secret_version(request={"name": name})
        value    = response.payload.data.decode("UTF-8").strip()
        _SECRET_CACHE[secret_id] = value   # cache for container lifetime
        return value
    except Exception as e:
        print(f"[SECRET] Fetch failed for {secret_id}: {e}. Falling back to env.")
        return os.environ.get(secret_id, "")


async def fetch_page_content(url: str):
    """
    FIX 04A: Explicit Playwright lifecycle management.
    Uses pw.start() / pw.stop() instead of `async with async_playwright()` so
    that the finally block is guaranteed to execute even when the coroutine is
    cancelled by asyncio.wait_for() (TimeoutError → CancelledError).

    Under the old `async with` pattern, a TimeoutError cancellation could leave
    the context manager's __aexit__ un-awaited, producing orphaned Chromium OS
    processes (~100MB each). Across 5-10 warm invocations under WAF conditions
    these accumulate and OOM-kill the container — the root cause of the zombie
    death loop.

    Now: browser and pw_instance are tracked explicitly. The finally block
    always closes the browser and stops Playwright, even on cancellation.
    """
    DECODO_STANDARD_PROXY = get_secret("DECODO_STANDARD_PROXY")
    DECODO_PREMIUM_PROXY  = get_secret("DECODO_PREMIUM_PROXY")

    args = ["--disable-dev-shm-usage", "--single-process", "--no-sandbox", "--no-zygote"]

    # Explicit start — allows deterministic stop() in the finally block
    pw_instance = await async_playwright().start()
    browser     = None

    try:
        kw = {}
        if DECODO_STANDARD_PROXY:
            kw["proxy"] = {"server": DECODO_STANDARD_PROXY}

        browser = await pw_instance.chromium.launch(headless=True, args=args, **kw)
        context = await browser.new_context(ignore_https_errors=True, bypass_csp=True)
        page    = await context.new_page()

        # Block heavy non-text resources to prevent OOM kills
        await page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ["image", "media", "font", "stylesheet"]
            else route.continue_()
        )

        response    = await page.goto(url, timeout=15000, wait_until="domcontentloaded")
        waf_blocked = bool(response and response.status in [403, 429, 503])

        text, title = "", ""
        try:
            text  = await page.evaluate("() => document.body.innerText")
            title = await page.title()
        except Exception:
            pass

        if not waf_blocked:
            dom_str      = (text + " " + title).lower()
            waf_keywords = ["just a moment...", "attention required",
                            "cf-browser-verification", "ray id"]
            if any(k in dom_str for k in waf_keywords):
                waf_blocked = True

        # Premium Fallback (High-Cost) — reuses pw_instance, spawns new browser
        if waf_blocked and DECODO_PREMIUM_PROXY:
            print(f"[SCRAPER] WAF detected for {url} — switching to DECODO_PREMIUM_PROXY.")
            await browser.close()
            browser = await pw_instance.chromium.launch(
                headless=True, args=args,
                proxy={"server": DECODO_PREMIUM_PROXY}
            )
            context = await browser.new_context(ignore_https_errors=True, bypass_csp=True)
            page    = await context.new_page()
            await page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ["image", "media", "font", "stylesheet"]
                else route.continue_()
            )
            await page.goto(url, timeout=15000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

        # Strip heavy DOM nodes before text extraction
        try:
            await page.evaluate(
                """() => {
                    document.querySelectorAll('script, style, noscript, nav, footer, iframe')
                        .forEach(el => el.remove());
                }"""
            )
        except Exception:
            pass

        # Contact Harvesting
        contacts = await page.evaluate(
            """() => {
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
            }"""
        )

        final_text = await page.evaluate("() => document.body.innerText")
        return final_text, contacts

    except asyncio.CancelledError:
        # Re-raise so asyncio.wait_for() can propagate the TimeoutError correctly.
        # The finally block below still executes before the raise unwinds.
        print(f"[SCRAPER] Coroutine cancelled for {url} — cleaning up Chromium.")
        raise

    except Exception as e:
        print(f"[SCRAPER] Error scraping {url}: {e}")
        return "", {"emails": [], "phones": []}

    finally:
        # ── FIX 04A: Guaranteed teardown regardless of how we exit ──────────
        # This finally block runs on: normal return, exception, AND CancelledError.
        # Closing browser + stopping Playwright kills all child Chromium PIDs
        # before the coroutine unwinds, preventing orphaned OS processes.
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        try:
            await pw_instance.stop()
        except Exception:
            pass


@app.route("/scrape", methods=["POST"])
def scrape():
    data = request.json
    url  = data.get("url")
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    # FIX 04A: Fresh event loop per request — required because Flask is sync
    # and each request is handled sequentially in the same thread.
    # The loop is closed in the finally block; cleanup callbacks (browser.close,
    # pw.stop) are drained via run_until_complete(sleep(0)) before close() so
    # asyncio does not emit "Future destroyed but not awaited" ResourceWarnings.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        text, contacts = loop.run_until_complete(
            asyncio.wait_for(fetch_page_content(url), timeout=20.0)
        )
    except asyncio.TimeoutError:
        print(f"[SCRAPER] 20s kill-switch activated for {url}. "
              f"Chromium teardown was handled in fetch_page_content finally block.")
        text, contacts = "", {}
    except Exception as e:
        print(f"[SCRAPER] Unexpected error for {url}: {e}")
        text, contacts = "", {}
    finally:
        # Drain any pending asyncio callbacks (e.g., connection close callbacks)
        # before destroying the loop to avoid ResourceWarning noise in logs.
        try:
            pending = asyncio.all_tasks(loop)
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
        except Exception:
            pass
        loop.close()

    # Decoupled Webhook Callback via Cloud Tasks
    # FIX 04C: Reuse module-level _TASKS_CLIENT singleton instead of
    # instantiating tasks_v2.CloudTasksClient() on every request.
    payload = {
        "text":               text[:100000] if text else "",
        "emails":             contacts.get("emails", []) if contacts else [],
        "phones":             contacts.get("phones", []) if contacts else [],
        "lead_id":            data.get("lead_id"),
        "tenant_id":          data.get("tenant_id"),
        "campaign_id":        data.get("campaign_id"),
        "bio":                data.get("bio"),
        "url":                url,
        "target_domain":      data.get("target_domain"),
        "preferences_weights": data.get("preferences_weights", {}),
    }

    try:
        project  = os.environ.get("PROJECT_ID",  "sideio-leads-v16")
        location = os.environ.get("LOCATION",    "asia-south1")
        queue    = os.environ.get("QUEUE",        "lead-pipeline-queue")
        parent   = _TASKS_CLIENT.queue_path(project, location, queue)    # reuse singleton

        base_url = os.environ.get("PIPELINE_BASE_URL",
                                  "https://lead-pipeline-main-abc.a.run.app")
        task_def = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url":         f"{base_url}/finalize",
                "headers":     {"Content-Type": "application/json"},
                "body":        json.dumps(payload).encode(),
            }
        }
        _TASKS_CLIENT.create_task(parent=parent, task=task_def)
    except Exception as hook_e:
        print(f"[SCRAPER] Failed to queue finalize webhook: {hook_e}")

    return jsonify({"status": "queued_to_finalize"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
