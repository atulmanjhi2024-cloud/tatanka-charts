// The app opens from what this worker saved: instantly, and offline too. The page asks for its files by version
// (app.js?v=<version>, see /sw.js and / on the server), so those never change: taken from here, fetched only once.
// The page itself comes from here as well and is refreshed in the background; an update installs a new worker
// (its CACHE name is the version), which saves the new files, and the open pages reload onto it.
const CACHE = 'charts-v89';
const VER = CACHE.slice('charts-'.length);
const FILES = ['app.js', 'press.js', 'ta.js', 'studies.js', 'drawings.js', 'watchlist.js', 'symsearch.js', 'indicators.js', 'layout.js', 'style.css',
  'vendor/lightweight-charts.standalone.production.js'];
const SHELL = ['./', 'manifest.webmanifest', ...FILES.map((f) => `${f}?v=${VER}`), 'icons/icon-192.png', 'icons/icon-180.png', 'icons/icon-512.png',
  'icons/icon-maskable-512.png', 'icons/mark.png', 'icons/favicon-32.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL.map((u) => new Request(u, { cache: 'reload' }))))
    .then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
const save = (req, r) => { if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); } return r; };
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api') || url.pathname === '/ws') return;
  if (url.searchParams.get('v') === VER) {   // a file of this version: never changes
    e.respondWith(caches.match(e.request).then((c) => c || fetch(e.request).then((r) => save(e.request, r))));
    return;
  }
  if (e.request.mode === 'navigate' && (url.pathname === '/' || url.pathname.endsWith('/index.html'))) {   // the page: the saved one at once, the server's saved for next time
    const net = fetch(e.request).then((r) => save(new Request('./'), r));
    e.respondWith(caches.match('./').then((c) => c || net).catch(() => net));
    e.waitUntil(net.catch(() => {}));
    return;
  }
  // the rest (icons, an older version's file): the server's; on an error page (502 while it restarts) or no network,
  // the saved copy
  const cached = () => caches.match(e.request);
  e.respondWith(fetch(e.request)
    .then((r) => (r.ok ? save(e.request, r) : cached().then((c) => c || r)))
    .catch(cached));
});

// App notifications (Web Push): the server encrypts each alert for this device; show it, and open the app on a tap.
self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(d.title || 'Tatanka Charts', {
    body: d.body || '', tag: d.tag || undefined, renotify: !!d.tag, timestamp: d.ts || Date.now(),
    icon: 'icons/icon-192.png', badge: 'icons/icon-192.png', data: { url: './' },
  }));
});
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
    const w = wins.find((c) => 'focus' in c);
    return w ? w.focus() : self.clients.openWindow(e.notification.data?.url || './');
  }));
});
