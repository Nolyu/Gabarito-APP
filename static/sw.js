// GabaritoApp — Service Worker
// Cacheia o essencial (OpenCV.js, página offline) para funcionar sem internet.
// Estratégia: cache-first para bibliotecas grandes e estáticas,
// network-first para páginas HTML e para a API (exceto o pacote offline salvo no IndexedDB).

const CACHE_NAME = "gabaritoapp-v1";
const ARQUIVOS_ESSENCIAIS = [
  "/offline/0",  // será substituído dinamicamente por quiz_id real ao visitar
  "/static/manifest.json",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "https://docs.opencv.org/4.9.0/opencv.js",
];

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      // Tenta cachear o essencial, mas não falha a instalação se algo não carregar agora
      return Promise.allSettled(
        ARQUIVOS_ESSENCIAIS.map((url) => cache.add(url).catch(() => null))
      );
    })
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((nomes) =>
      Promise.all(
        nomes.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = event.request.url;

  // opencv.js: cache-first (arquivo grande, não muda, precisa funcionar offline)
  if (url.includes("opencv.js")) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        if (cached) return cached;
        return fetch(event.request).then((resp) => {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          return resp;
        });
      })
    );
    return;
  }

  // páginas /offline/<id>: network-first, com fallback pro cache se estiver sem internet
  if (url.includes("/offline/")) {
    event.respondWith(
      fetch(event.request)
        .then((resp) => {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          return resp;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  // resto (API, outras páginas): direto na rede, sem cache
});
