// Minimal service worker. Its only job is to make the app installable —
// it deliberately does no caching, so staff never see stale prices.
self.addEventListener('install', function (e) { self.skipWaiting(); });
self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', function (e) { return; });