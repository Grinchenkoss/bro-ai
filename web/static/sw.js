// SW v3: сеть-первым, кэш кода НЕ держим (чтобы обновления всегда подгружались).
// При активации чистим все старые кэши от прошлых версий (в т.ч. fc-shell).
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(ks => Promise.all(ks.map(k => caches.delete(k)))).then(() => self.clients.claim())
  );
});
// Пустой fetch-обработчик — приложение остаётся устанавливаемым, но НЕ кэширует (всегда сеть).
self.addEventListener('fetch', () => {});
