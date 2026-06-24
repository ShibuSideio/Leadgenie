/**
 * Sideio Visitor Intelligence Tracker v1.0
 * Embed on your website to identify visiting companies.
 * Privacy-first: no cookies, no PII, company resolution server-side.
 *
 * Usage:
 *   <script src="https://lead-sniper-prod.web.app/sideio-tracker.js"
 *           data-tid="YOUR_TENANT_ID" async></script>
 */
(function() {
    'use strict';
    var ENDPOINT = 'https://orchestrator-222247989819.asia-south1.run.app/api/visitor-signals';
    var script = document.currentScript;
    if (!script) return;
    var tid = script.getAttribute('data-tid');
    if (!tid) return;

    // Debounce: only fire once per session
    var KEY = 'sio_v_' + tid;
    if (sessionStorage.getItem(KEY)) return;
    sessionStorage.setItem(KEY, '1');

    var payload = {
        tenant_id: tid,
        page_url: window.location.href,
        referrer: document.referrer || '',
        page_title: document.title || '',
        screen_width: screen.width,
        timestamp: new Date().toISOString()
    };

    // Use sendBeacon for reliability (fires even on page unload)
    if (navigator.sendBeacon) {
        navigator.sendBeacon(ENDPOINT, JSON.stringify(payload));
    } else {
        var xhr = new XMLHttpRequest();
        xhr.open('POST', ENDPOINT, true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.send(JSON.stringify(payload));
    }
})();
