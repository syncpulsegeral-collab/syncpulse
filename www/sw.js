// Service Worker do SyncPulse Mobile (PWA).
//
// Estratégia: NETWORK-FIRST para o "shell" da app (mobile.html, manifest,
// ícones) — tenta sempre a rede primeiro, e só usa a cache como reserva se
// estiver offline. Isto evita o problema de ficar preso numa versão antiga:
// basta abrir a app com rede para apanhar sempre o HTML mais recente do
// servidor, sem precisar de 2 recarregamentos nem de limpar cache à mão.
//
// Nunca intercepta chamadas à API nem ao WebSocket — essas vão sempre à
// rede, para o servidor configurado em SERVER_URL (que pode ser de origem
// diferente da PWA).

const CACHE_NAME = 'syncpulse-shell-v4';
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
  if (req.method !== 'GET') return;

  let url;
  try { url = new URL(req.url); } catch (e) { return; }
  if (url.origin !== self.location.origin) return;
  if (!SHELL_FILES.includes(url.pathname)) return;

  event.respondWith(
    fetch(req, { cache: 'no-store' }).then((res) => {
      if (res && res.ok) {
        const clone = res.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(req, clone));
      }
      return res;
    }).catch(() => caches.match(req))
  );
});

// Mensagens vindas da app (mobile.html)
self.addEventListener('message', (event) => {
  const data = event.data;

  // Permite forçar a ativação imediata de uma nova versão do SW
  if (data === 'skipWaiting' || (data && data.type === 'SKIP_WAITING')) {
    self.skipWaiting();
    return;
  }

  // Exibir uma notificação local (disparada pela app via WebSocket) —
  // via SW é mais fiável no Android do que "new Notification()" direto.
  if (data && data.type === 'SHOW_NOTIFICATION') {
    event.waitUntil(
      self.registration.showNotification(data.title || 'SyncPulse', {
        body: data.body || '',
        icon: '/icon-192.png',
        badge: '/icon-192.png',
        tag: data.tag || undefined,
      })
    );
  }
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ('focus' in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow('/mobile.html');
    })
  );
});