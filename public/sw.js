const CACHE_NAME = 'sideio-v10-3';
const ASSETS_TO_CACHE = [
    '/',
    '/index.html',
    '/app.js',
    '/styles.css',
    '/manifest.json'
];

self.addEventListener('install', event => {
    self.skipWaiting(); // Force the waiting service worker to become the active service worker.
    event.waitUntil(
        caches.open(CACHE_NAME)
        .then(cache => cache.addAll(ASSETS_TO_CACHE))
    );
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(cacheNames => {
            return Promise.all(
                cacheNames.map(cacheName => {
                    if (cacheName !== CACHE_NAME) {
                        return caches.delete(cacheName);
                    }
                })
            );
        }).then(() => self.clients.claim()) // Immediately assert control.
    );
});

// Network-First Strategy (Guarantees fresh UI code deployed via Firebase Hosting)
self.addEventListener('fetch', event => {
    // ── CRITICAL: Bypass cache for all Firebase / Google API traffic ──────────
    // Firestore WebChannel streams (long-poll XHR / WebSocket upgrades) cannot
    // be cloned into a Response object. Intercepting them causes:
    //   "Failed to convert value to 'Response'" + 30-second disconnect loops.
    // Pass these requests straight to the network — never touch the cache.
    const url = new URL(event.request.url);
    if (
        url.hostname.includes('googleapis.com') ||
        url.hostname.includes('google.com')     ||
        url.hostname.includes('firestore')
    ) {
        event.respondWith(fetch(event.request));
        return;
    }

    // All other requests: network-first, fall back to cache
    event.respondWith(
        fetch(event.request).catch(() => {
            return caches.match(event.request);
        })
    );
});
