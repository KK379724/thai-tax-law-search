// Service Worker — คลังกฎหมายภาษี (network-first, cache หน้าแอปไว้ใช้ตอนออฟไลน์)
const CACHE = 'law-shell-v1';
const SHELL = ['/', '/pwa/icon-192.png', '/pwa/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  const u = new URL(e.request.url);
  if (u.pathname.startsWith('/api/')) return;          // ข้อมูลสด — ไม่แคช
  e.respondWith(
    fetch(e.request)
      .then(r => {
        if (r.ok && u.origin === location.origin) {
          const copy = r.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy)).catch(() => {});
        }
        return r;
      })
      .catch(() => caches.match(e.request).then(m => m || caches.match('/')))
  );
});
