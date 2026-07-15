// GabaritoApp — Service Worker
//
// ESTRATÉGIA: o app abre INSTANTÂNEO, do celular, sem esperar o servidor.
// - /app e opencv.js: cache-first ("stale-while-revalidate"). Abre na hora com o
//   que está salvo e busca a versão nova em segundo plano, para a próxima abertura.
//   Isso evita a espera do servidor acordar (o plano grátis do Render hiberna).
// - Outras páginas (site/dashboard): rede primeiro, cache como reserva.
// - Ações que mexem em dados (login, criar, sincronizar) sempre exigem rede.

const CACHE_NAME = "gabaritoapp-v3";
const ARQUIVOS_ESSENCIAIS = [
  "/app",
  "/static/manifest.json",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "https://docs.opencv.org/4.9.0/opencv.js",
];

const API_CACHEAVEL = [
  "/api/status",
  "/api/quizzes",
  "/api/folhas",
];

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      Promise.allSettled(ARQUIVOS_ESSENCIAIS.map((url) => cache.add(url).catch(() => null)))
    )
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((nomes) =>
      Promise.all(nomes.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

function ehApiCacheavel(url) {
  return API_CACHEAVEL.some((p) => url.includes(p)) ||
    /\/api\/quizzes\/\d+$/.test(url) ||
    /\/api\/quizzes\/\d+\/(scans|estatisticas)$/.test(url) ||
    /\/api\/scans\/\d+$/.test(url);
}

// Entrega o que está salvo AGORA e atualiza o cache em segundo plano.
function cachePrimeiroAtualizandoDepois(req) {
  return caches.open(CACHE_NAME).then((cache) =>
    cache.match(req).then((salvo) => {
      const rede = fetch(req)
        .then((resp) => {
          if (resp && resp.ok) cache.put(req, resp.clone());
          return resp;
        })
        .catch(() => null);
      // se já temos versão salva, devolve na hora (rede segue em segundo plano)
      return salvo || rede.then((r) => r || cache.match("/app"));
    })
  );
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = req.url;

  if (req.method !== "GET") return;

  // opencv.js: cache-first (arquivo grande, não muda)
  if (url.includes("opencv.js")) {
    event.respondWith(
      caches.match(req).then((cached) => cached || fetch(req).then((resp) => {
        const clone = resp.clone();
        caches.open(CACHE_NAME).then((c) => c.put(req, clone));
        return resp;
      }))
    );
    return;
  }

  let caminho = "";
  try { caminho = new URL(url).pathname; } catch (e) {}
  const ehTelaDoApp = req.mode === "navigate" && caminho === "/app";

  // A tela do app: abre instantâneo do cache, atualiza depois.
  if (ehTelaDoApp) {
    event.respondWith(cachePrimeiroAtualizandoDepois(req));
    return;
  }

  // Demais páginas do site: rede primeiro, cache como reserva.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((resp) => {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then((c) => c.put(req, clone));
          return resp;
        })
        .catch(() => caches.match(req).then((cached) => cached || caches.match("/app")))
    );
    return;
  }

  // Dados de leitura da API: rede primeiro, último dado salvo se offline.
  if (ehApiCacheavel(url)) {
    event.respondWith(
      fetch(req)
        .then((resp) => {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then((c) => c.put(req, clone));
          return resp;
        })
        .catch(() => caches.match(req))
    );
    return;
  }
});
