const STATIC_CACHE = 'sr-db-static-v1';
const PAGES_CACHE = 'sr-db-pages-v1';
const API_CACHE = 'sr-db-api-v1';
const ALL_CACHES = [STATIC_CACHE, PAGES_CACHE, API_CACHE];

const PRECACHE_ASSETS = [
  '/static/style.css?v=4.6',
  '/static/style.css?v=4.5',
  '/static/insights.css?v=1',
  '/static/offline_cache.js?v=1',
  '/static/app.js?v=4.5',
  '/static/custom_fields.js?v=4',
  '/static/well_history.js?v=3',
  '/static/manifest.json',
  '/static/icon.ico',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) =>
      Promise.allSettled(PRECACHE_ASSETS.map((url) => cache.add(url)))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => !ALL_CACHES.includes(k))
          .map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

function isStaticRequest(url) {
  return (
    url.pathname.startsWith('/static/') ||
    url.pathname === '/manifest.json' ||
    url.pathname === '/favicon.ico'
  );
}

function isApiRequest(url) {
  return url.pathname.startsWith('/api/');
}

function isHtmlNavigation(request) {
  if (request.mode === 'navigate') return true;
  const accept = request.headers.get('accept') || '';
  return accept.includes('text/html');
}

function buildOfflineFallbackResponse() {
  const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Offline Mode — SR Database Server</title>
  <link rel="stylesheet" href="/static/style.css?v=4.6" />
</head>
<body style="display:block; padding:24px; background:#f1f5f9;">
  <div style="max-width:720px; margin:24px auto;" class="card">
    <div style="display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; margin-bottom:12px;">
      <span class="badge badge-amber" style="font-size:12.5px; padding:5px 12px;">Offline Mode - Cached Data</span>
      <a href="/" class="btn btn-outline">Go to Cached Dashboard</a>
    </div>
    <h3>This page was not cached before going offline</h3>
    <p style="font-size:13px; color:#475569; line-height:1.5;">
      You are currently disconnected from the server. You can return to the Dashboard or Wells page you previously visited,
      or search your locally cached well records below.
    </p>
    <div id="sw-offline-explorer"></div>
  </div>
  <script src="/static/offline_cache.js?v=1"></script>
</body>
</html>`;
  return new Response(html, {
    status: 200,
    headers: { 'Content-Type': 'text/html; charset=utf-8' }
  });
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname === '/sw.js') return;

  // 1. Static assets: Cache-first with background refresh
  if (isStaticRequest(url)) {
    event.respondWith(
      caches.open(STATIC_CACHE).then((cache) =>
        cache.match(req).then((cached) => {
          const networkFetch = fetch(req)
            .then((res) => {
              if (res && res.ok) {
                cache.put(req, res.clone());
              }
              return res;
            })
            .catch(() => cached);
          return cached || networkFetch;
        })
      )
    );
    return;
  }

  // 2. API GET requests: Network-first with cache fallback
  if (isApiRequest(url)) {
    event.respondWith(
      caches.open(API_CACHE).then((cache) =>
        fetch(req)
          .then((res) => {
            if (res && res.ok) {
              cache.put(req, res.clone());
            }
            return res;
          })
          .catch(() =>
            cache.match(req).then(
              (cached) =>
                cached ||
                new Response(
                  JSON.stringify({ ok: false, offline: true, error: 'Offline and no cached data for this request.' }),
                  { status: 503, headers: { 'Content-Type': 'application/json' } }
                )
            )
          )
      )
    );
    return;
  }

  // 3. HTML pages / navigation: Network-first, fall back to cached HTML
  if (isHtmlNavigation(req)) {
    event.respondWith(
      caches.open(PAGES_CACHE).then((cache) =>
        fetch(req)
          .then((res) => {
            if (res && res.ok) {
              const finalPath = new URL(res.url || req.url).pathname;
              const reqPath = url.pathname;
              // Avoid overwriting authenticated pages with a redirected /login screen
              if (finalPath === reqPath || reqPath === '/login' || reqPath === '/setup') {
                cache.put(req, res.clone());
              }
            }
            return res;
          })
          .catch(async () => {
            const exact = await cache.match(req);
            if (exact) return exact;
            const pathOnly = await cache.match(url.pathname);
            if (pathOnly) return pathOnly;
            const rootCached = await cache.match('/');
            if (rootCached) return rootCached;
            return buildOfflineFallbackResponse();
          })
      )
    );
  }
});
