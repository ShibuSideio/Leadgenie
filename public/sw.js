const CACHE_NAME = 'sideio-v10-2';
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
    event.respondWith(
        fetch(event.request).catch(() => {
            return caches.match(event.request);
        })
    );
});
