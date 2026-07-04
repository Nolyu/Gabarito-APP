// GabaritoApp — Service Worker
// Cacheia o essencial para o APP INTEIRO abrir offline:
// - Páginas (dashboard, quiz, login) ficam salvas e reabrem com os últimos dados vistos
// - Respostas de GET da API (lista de quizzes, folhas, etc.) ficam salvas e servidas offline
// - OpenCV.js fica salvo (arquivo grande, não muda)
// - Ações que exigem internet de verdade (login, criar quiz, escanear online) continuam
//   exigindo conexão, porque mexem com dados no servidor — isso é uma limitação de
//   qualquer sistema, não só deste.

const CACHE_NAME = "gabaritoapp-v2";
const ARQUIVOS_ESSENCIAIS = [
  "/static/manifest.json",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "https://docs.opencv.org/4.9.0/opencv.js",
];

// GETs de API que fazem sentido guardar para reabrir offline (dados de leitura,
// não de ação). Login/registrar/criar/scan online NÃO entram aqui (são mutações).
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

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = req.url;

  // Só interceptamos GET — POST/PUT/DELETE (login, criar, escanear online, sync)
  // sempre precisam de rede de verdade, e falham naturalmente sem internet.
  if (req.method !== "GET") return;

  // opencv.js: cache-first (grande, não muda)
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

  // Navegação entre páginas (dashboard, quiz, offline, login etc.):
  // tenta rede primeiro; se não tiver internet, mostra a última versão salva.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((resp) => {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then((c) => c.put(req, clone));
          return resp;
        })
        .catch(() => caches.match(req).then((cached) => cached || caches.match("/dashboard")))
    );
    return;
  }

  // Dados de leitura da API (listas, detalhes): mesma estratégia —
  // tenta buscar atualizado; se offline, usa o último dado salvo.
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

  // resto (login, registrar, criar, scan online, sync): direto na rede, sem cache
});

