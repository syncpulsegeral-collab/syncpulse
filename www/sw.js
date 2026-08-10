// Service Worker do SyncPulse Mobile (PWA).
// Âmbito: apenas o "shell" da app mobile (mobile.html + ícones + manifest).
// Nunca intercepta chamadas à API nem ao WebSocket — essas vão sempre à rede,
// para o servidor configurado em SERVER_URL (que pode ser de origem diferente).

const CACHE_NAME = 'syncpulse-shell-v3';
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

// ... (mantém todo o teu código de cache acima)

// Ouvinte para mensagens enviadas pela página para disparar notificações
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SHOW_NOTIFICATION') {
    const { title, body, icon } = event.data;
    self.registration.showNotification(title, {
      body: body,
      icon: icon || '/icon-192.png',
      badge: '/logo.svg', // ícone pequeno na barra de status
      vibrate: [200, 100, 200],
      tag: 'syncpulse-status' // impede duplicados da mesma tarefa
    });
  }
});

// Abre a app ao clicar na notificação
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      if (clientList.length > 0) return clientList[0].focus();
      return clients.openWindow('/');
    })
  );
});


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