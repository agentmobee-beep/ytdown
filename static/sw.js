// MegaSave service worker.
// - Page navigations: stale-while-revalidate. A returning visitor (or
//   the installed app) sees the MegaSave page instantly from cache
//   while a fresh copy loads in the background. This hides the free
//   hosting provider's "application loading" page on the occasions
//   the server has to wake up first.
// - Icons / manifest: network-first with cache fallback.
// - API calls and downloads are never cached or intercepted.
const CACHE = 'megasave-shell-v2';
const SHELL = [
  '/',
  '/static/manifest.webmanifest',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/maskable-512.png'
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return; // downloads / API posts pass through
  if (url.origin !== location.origin) return; // ads and other external scripts pass through
  if (url.pathname.startsWith('/api/')) return; // never cache API answers

  if (e.request.mode === 'navigate') {
    e.respondWith((async () => {
      const cache = await caches.open(CACHE);
      const cached = (await cache.match(e.request, { ignoreSearch: true })) || (await cache.match('/'));
      const network = fetch(e.request)
        .then(async (res) => {
          if (res && res.ok) {
            // While the server wakes, the host answers with its own
            // holding page. Never let that page into the cache - only
            // cache responses that are really ours.
            const probe = res.clone();
            const text = await probe.text().catch(() => '');
            if (text.indexOf('MegaSave') !== -1) {
              cache.put(e.request, res.clone()).catch(() => {});
            }
          }
          return res;
        })
        .catch(() => null);
      if (cached) {
        e.waitUntil(network);
        return cached;
      }
      const res = await network;
      return res || (await cache.match('/')) || Response.error();
    })());
    return;
  }

  // Static files: network-first, cache fallback, and only cache the
  // kinds of files we expect (never the host's holding page).
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const type = (res && res.headers.get('content-type')) || '';
        if (
          res &&
          res.ok &&
          (type.indexOf('image/') === 0 ||
            type.indexOf('application/manifest+json') !== -1 ||
            type.indexOf('javascript') !== -1 ||
            type.indexOf('text/css') !== -1)
        ) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});
