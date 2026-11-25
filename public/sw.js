const CACHE_NAME = 'aslan-pwa-ab0187b8';const APP_SHELL = ['/','/manifest.json','/assets/app.min.css?v=ab0187b8','/assets/app.min.js?v=ab0187b8','/assets/favicon.png','/assets/icon-192.png','/assets/icon-512.png','/assets/icon-maskable-512.png','/assets/apple-touch-icon.png','/assets/logo-mark.png','/assets/logo-banner.png'
];self.addEventListener('install', function (event) {event.waitUntil(caches.open(CACHE_NAME).then(function (cache) {return cache.addAll(APP_SHELL);}).catch(function () {return Promise.resolve();}).then(function () {return self.skipWaiting();})
);});self.addEventListener('activate', function (event) {event.waitUntil(caches.keys().then(function (keys) {return Promise.all(keys.filter(function (k) {return k.indexOf('aslan-pwa-') === 0 && k !== CACHE_NAME;}).map(function (k) {return caches.delete(k);})
);}).then(function () {return self.clients.claim();})
);});self.addEventListener('fetch', function (event) {var req = event.request;if (!req || req.method !== 'GET') return;var url;try {url = new URL(req.url);} catch (e) {return;}
if (url.origin !== self.location.origin) return;if (url.pathname === '/api' || url.pathname.indexOf('/api/') === 0) {event.respondWith(fetch(req).catch(function () {return new Response(JSON.stringify({ error: 'Çevrimdışısınız. İnternet bağlantınızı kontrol edin.' }),{status: 503,headers: {'Content-Type': 'application/json; charset=utf-8','Cache-Control': 'no-store'
}});})
);return;}
if (req.mode === 'navigate') {event.respondWith(fetch(req).then(function (res) {if (res && res.status === 200 && res.type === 'basic') {var copy = res.clone();caches.open(CACHE_NAME).then(function (cache) {cache.put('/', copy);});}
return res;}).catch(function () {return caches.match('/').then(function (cached) {return cached || caches.match('/index.html');});})
);return;}
if (url.pathname.indexOf('/assets/') === 0 ||
url.pathname === '/manifest.json' ||
url.pathname === '/favicon.svg'
) {event.respondWith(caches.match(req).then(function (cached) {var networkFetch = fetch(req).then(function (res) {if (res && res.status === 200 && res.type === 'basic') {var copy = res.clone();caches.open(CACHE_NAME).then(function (cache) {cache.put(req, copy);});}
return res;}).catch(function () {return cached;});return cached || networkFetch;})
);}});self.addEventListener('message', function (event) {if (event && event.data && event.data.type === 'SKIP_WAITING') {self.skipWaiting();}});