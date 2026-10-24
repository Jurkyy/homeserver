// mediacast portal service worker — caches the static shell only; /api/ always hits the network.
const CACHE = 'mediacast-shell-v2';
const SHELL = [
  '/',
  '/static/style.css',
  '/static/app.js',
  '/static/vendor/qrcode.min.js',
  '/static/icon.svg',
  '/static/icon-192.png',
  '/static/icon-512.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      // addAll is atomic; add individually so one missing asset doesn't break install
      .then((c) => Promise.all(SHELL.map((u) => c.add(u).catch(() => {}))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return; // network only, untouched

  if (req.mode === 'navigate') {
    // Network-first: the page carries the injected token/config, keep it fresh.
    e.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put('/', copy));
          }
          return res;
        })
        .catch(() => caches.match('/').then((r) => r || Response.error()))
    );
    return;
  }

  if (url.pathname.startsWith('/static/')) {
    // Cache-first, revalidated in the background (stale-while-revalidate) so
    // deploys show up on the next load without having to bump CACHE.
    e.respondWith(
      caches.open(CACHE).then((c) => c.match(req).then((hit) => {
        const net = fetch(req).then((res) => {
          if (res.ok) c.put(req, res.clone());
          return res;
        });
        if (hit) { e.waitUntil(net.catch(() => {})); return hit; }
        return net;
      }))
    );
  }
});
