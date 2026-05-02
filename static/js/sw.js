// Service Worker — install only, no offline caching
// Just enables PWA installability and shows offline toast

const CACHE_NAME = 'fussballcamp-v1';

// On install — cache nothing, just activate
self.addEventListener('install', e => {
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  self.clients.claim();
});

// On fetch — pass through all requests, catch network errors
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;

  e.respondWith(
    fetch(e.request).catch(() => {
      // Network failed — post message to page to show toast
      self.clients.matchAll().then(clients => {
        clients.forEach(client => client.postMessage({ type: 'OFFLINE' }));
      });
      // Return a minimal offline response
      return new Response(
        '<html><body style="font-family:sans-serif;text-align:center;padding:3rem">' +
        '<h2>⚡ Keine Verbindung</h2>' +
        '<p>Bitte Internetverbindung prüfen und erneut versuchen.</p>' +
        '<button onclick="location.reload()">Erneut versuchen</button>' +
        '</body></html>',
        { headers: { 'Content-Type': 'text/html' } }
      );
    })
  );
});
