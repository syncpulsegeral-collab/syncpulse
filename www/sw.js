// Service Worker do SyncPulse Mobile (PWA).
// Âmbito: apenas o "shell" da app mobile (mobile.html + ícones + manifest).
// Nunca intercepta chamadas à API nem ao WebSocket — essas vão sempre à rede,
// para o servidor configurado em SERVER_URL (que pode ser de origem diferente).

const CACHE_NAME = 'syncpulse-shell-v2';
const SHELL_FILES = [
  '/mobile.html',
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png',
  '/icon-512-maskable.png',
  '/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;

  // Só GET, só o mesmo domínio, e só ficheiros da shell — tudo o resto
  // (API, WS, index.html, chamadas a outros servidores) segue direto à rede.
  if (req.method !== 'GET') return;

  let url;
  try { url = new URL(req.url); } catch (e) { return; }
  if (url.origin !== self.location.origin) return;
  if (!SHELL_FILES.includes(url.pathname)) return;

  event.respondWith(
    caches.match(req).then((cached) => {
      const network = fetch(req).then((res) => {
        if (res && res.ok) {
          caches.open(CACHE_NAME).then((cache) => cache.put(req, res.clone()));
        }
        return res;
      }).catch(() => cached);
      return cached || network;
    })
  );
});
