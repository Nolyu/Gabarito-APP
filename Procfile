<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#2563eb">
<link rel="manifest" href="/static/manifest.json">
<link rel="icon" href="/static/icon-192.png">
<title>{{ quiz_nome }} — GabaritoApp</title>
<style>
  :root { --azul:#2563eb; --verde:#16a34a; --vermelho:#dc2626; --laranja:#ea580c;
          --dourado:#ca8a04; --texto:#1e293b; --borda:#e2e8f0; --cinza:#f1f5f9; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:#f8fafc; color:var(--texto); }
  header {
    background:var(--azul); color:white; padding:12px 16px;
    display:flex; align-items:center; gap:12px; position:sticky; top:0; z-index:100;
  }
  header a { color:white; text-decoration:none; font-size:1.3rem; }
  header h1 { font-size:1rem; flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .container { max-width:600px; margin:0 auto; padding:12px; }

  .abas { display:flex; background:white; border-radius:14px; padding:4px;
    margin-bottom:14px; border:1px solid var(--borda); }
  .aba { flex:1; text-align:center; padding:10px 4px; font-size:0.78rem; font-weight:700;
    color:#64748b; border-radius:10px; cursor:pointer; }
  .aba.ativa { background:var(--azul); color:white; }

  .card { background:white; border-radius:16px; padding:16px; margin-bottom:12px;
    border:1px solid var(--borda); }
  .card h2 { font-size:0.95rem; margin-bottom:10px; }
  .btn { display:block; width:100%; padding:14px; border:none; border-radius:12px;
    font-size:0.98rem; font-weight:700; cursor:pointer; text-align:center; }
  .btn-azul { background:var(--azul); color:white; }
  .btn-cinza { background:var(--cinza); color:var(--texto); margin-top:8px; }

  /* gabarito avançado */
  .gq { border:1px solid var(--borda); border-radius:12px; padding:10px; margin-bottom:8px; }
  .gq-topo { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .gq-num { font-weight:800; font-size:0.9rem; min-width:26px; }
  .gq-alts { display:flex; gap:5px; flex-wrap:wrap; flex:1; }
  .alt-btn {
    width:34px; height:34px; border-radius:50%; border:2px solid var(--borda);
    background:white; font-weight:700; font-size:0.85rem; cursor:pointer; color:#64748b;
  }
  .alt-btn.sel { background:var(--verde); border-color:var(--verde); color:white; }
  .gq-extras { display:flex; align-items:center; gap:12px; margin-top:8px; font-size:0.78rem; }
  .gq-extras label { display:flex; align-items:center; gap:5px; color:#64748b; font-weight:600; }
  .gq-extras input[type=checkbox] { width:17px; height:17px; }
  .gq-extras input[type=number] {
    width:64px; padding:6px; border:1.5px solid var(--borda); border-radius:8px; font-size:0.82rem;
  }
  .gq.anulada-on { background:#fefce8; border-color:#eab308; }

  .scan-resultado {
    background:linear-gradient(135deg,#eff6ff,#dbeafe); border-radius:14px;
    padding:16px; text-align:center; margin-bottom:10px;
  }
  .scan-resultado .nota { font-size:2.6rem; font-weight:800; color:var(--azul); }
  .scan-resultado .sub-nota { font-size:0.82rem; color:#64748b; }
  .scan-badges { display:flex; gap:6px; justify-content:center; flex-wrap:wrap; margin-top:8px; }
  .sbadge { font-size:0.75rem; padding:4px 10px; border-radius:20px; font-weight:700; }
  .sb-verde { background:#dcfce7; color:var(--verde); }
  .sb-vermelho { background:#fee2e2; color:var(--vermelho); }
  .sb-cinza { background:#e2e8f0; color:#475569; }
  .sb-laranja { background:#ffedd5; color:var(--laranja); }
  .sb-dourado { background:#fef9c3; color:var(--dourado); }
  .sb-azul { background:#dbeafe; color:var(--azul); font-family:monospace; }
  .nome-crop { width:100%; border-radius:10px; border:1px solid var(--borda); margin-top:10px; }
  .debug-img { width:100%; border-radius:12px; margin-top:10px; }

  .scan-item {
    display:flex; align-items:center; gap:10px; background:white;
    border:1px solid var(--borda); border-radius:12px; padding:10px; margin-bottom:8px;
    cursor:pointer;
  }
  .scan-item img { width:105px; border-radius:8px; border:1px solid var(--borda); }
  .scan-item .info { flex:1; }
  .scan-item .nota-mini { font-size:1.25rem; font-weight:800; color:var(--azul); }
  .scan-item .meta { font-size:0.7rem; color:#94a3b8; }
  .scan-item .del { background:none; border:none; color:#cbd5e1; font-size:1.05rem; cursor:pointer; }

  .stat-resumo { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-bottom:12px; }
  .stat-box { background:var(--cinza); border-radius:12px; padding:10px; text-align:center; }
  .stat-box .v { font-size:1.15rem; font-weight:800; color:var(--azul); }
  .stat-box .l { font-size:0.66rem; color:#64748b; margin-top:2px; }
  .q-stat { margin-bottom:12px; }
  .q-stat .titulo { font-size:0.83rem; font-weight:700; margin-bottom:4px;
    display:flex; justify-content:space-between; }
  .barra { display:flex; height:17px; border-radius:9px; overflow:hidden; background:var(--cinza); }
  .barra div { height:100%; }
  .b-certo { background:var(--verde); }
  .b-errado { background:var(--vermelho); }
  .b-branco { background:#94a3b8; }
  .b-multi { background:var(--laranja); }
  .alts { font-size:0.7rem; color:#64748b; margin-top:3px; }

  .loading { display:none; text-align:center; padding:20px; }
  .spinner { width:36px; height:36px; border:4px solid var(--borda);
    border-top-color:var(--azul); border-radius:50%;
    animation:spin .8s linear infinite; margin:0 auto 8px; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .erro-box { background:#fee2e2; color:var(--vermelho); padding:12px;
    border-radius:10px; font-size:0.88rem; margin-top:8px; display:none; }
  .msg-ok { background:#dcfce7; color:var(--verde); padding:12px;
    border-radius:10px; font-size:0.88rem; margin-top:8px; display:none; }
  .painel { display:none; }
  .painel.ativo { display:block; }
  .vazio { text-align:center; color:#94a3b8; padding:30px; font-size:0.88rem; }

  .modal-bg { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.55);
    z-index:300; align-items:flex-start; justify-content:center; padding:16px; overflow-y:auto; }
  .modal-bg.aberto { display:flex; }
  .modal { background:white; border-radius:18px; padding:18px; width:100%;
    max-width:500px; margin:20px 0; }
  .tabela { width:100%; border-collapse:collapse; font-size:0.83rem; margin-top:10px; }
  .tabela th { background:var(--cinza); padding:7px; font-size:0.7rem;
    text-transform:uppercase; color:#64748b; text-align:left; }
  .tabela td { padding:7px; border-bottom:1px solid var(--borda); }
  .tag { padding:2px 8px; border-radius:16px; font-size:0.7rem; font-weight:700; }
  .t-certo { background:#dcfce7; color:var(--verde); }
  .t-errado { background:#fee2e2; color:var(--vermelho); }
  .t-branco { background:#f1f5f9; color:#64748b; }
  .t-multimarcada { background:#ffedd5; color:var(--laranja); }
  .t-anulada { background:#fef9c3; color:var(--dourado); }
</style>
</head>
<body>
<header>
  <a href="/dashboard">←</a>
  <h1>{{ quiz_nome }}</h1>
</header>

<div class="container">
  <div class="abas">
    <div class="aba ativa" onclick="mudarAba('gabarito', this)">✏️ Gabarito</div>
    <div class="aba" onclick="mudarAba('scan', this)">📸 Escanear</div>
    <div class="aba" onclick="mudarAba('scans', this)">📚 Resultados</div>
    <div class="aba" onclick="mudarAba('stats', this)">📊 Estatísticas</div>
  </div>

  <!-- GABARITO -->
  <div class="painel ativo" id="painel-gabarito">
    <div class="card">
      <h2>Gabarito correto</h2>
      <p style="font-size:0.8rem;color:#64748b;margin-bottom:10px">
        Toque nas letras corretas (pode marcar mais de uma).
        Marque "Anulada" para dar ponto a todos. Ajuste os pontos de cada questão.
      </p>
      <div id="gab-lista"></div>
      <button class="btn btn-azul" style="margin-top:12px" onclick="salvarGabarito()">Salvar gabarito</button>
      <div class="msg-ok" id="gab-ok">✅ Gabarito salvo!</div>
      <div class="erro-box" id="gab-erro"></div>
    </div>
  </div>

  <!-- SCAN -->
  <div class="painel" id="painel-scan">
    <div class="card">
      <h2>Escanear folha de resposta</h2>
      <p style="font-size:0.82rem;color:#64748b;margin-bottom:10px">
        Fotografe uma folha por vez. O resultado fica salvo automaticamente.
      </p>
      <button class="btn btn-azul" onclick="document.getElementById('input-foto').click()">
        📷 Fotografar / escolher foto
      </button>
      <a href="/offline/{{ quiz_id }}" class="btn btn-cinza" style="text-decoration:none;display:block">
        📡 Usar modo offline (sem internet)
      </a>
      <input type="file" id="input-foto" accept="image/*" capture="environment"
             style="display:none" onchange="escanear(this)">
      <div class="loading" id="scan-loading">
        <div class="spinner"></div><p style="font-size:0.85rem">Lendo folha...</p>
      </div>
      <div class="erro-box" id="scan-erro"></div>
    </div>
    <div id="scan-resultado-area"></div>
  </div>

  <!-- CORREÇÕES -->
  <div class="painel" id="painel-scans">
    <div id="lista-scans"></div>
  </div>

  <!-- ESTATÍSTICAS -->
  <div class="painel" id="painel-stats">
    <div id="stats-area"></div>
  </div>
</div>

<div class="modal-bg" id="modal-scan" onclick="if(event.target===this)this.classList.remove('aberto')">
  <div class="modal" id="modal-scan-conteudo"></div>
</div>

<script>
const QUIZ_ID = {{ quiz_id }};
let LABELS = {};       // {"1": "ABCDE", ...}
let GABARITO = {};     // {"1": {corretas:[], anulada:false, pontos:1}, ...}

function mudarAba(nome, el) {
  document.querySelectorAll('.aba').forEach(a => a.classList.remove('ativa'));
  document.querySelectorAll('.painel').forEach(p => p.classList.remove('ativo'));
  el.classList.add('ativa');
  document.getElementById('painel-' + nome).classList.add('ativo');
  if (nome === 'scans') carregarScans();
  if (nome === 'stats') carregarStats();
}

// ─── GABARITO ───
async function carregarGabarito() {
  const res = await fetch(`/api/quizzes/${QUIZ_ID}`);
  const quiz = await res.json();
  LABELS = quiz.labels;
  GABARITO = quiz.gabarito || {};

  const lista = document.getElementById('gab-lista');
  const nums = Object.keys(LABELS).sort((a, b) => parseInt(a) - parseInt(b));
  lista.innerHTML = nums.map(n => {
    const g = GABARITO[n] || {corretas: [], anulada: false, pontos: 1};
    const labels = LABELS[n];
    return `
      <div class="gq ${g.anulada ? 'anulada-on' : ''}" id="gq-${n}">
        <div class="gq-topo">
          <span class="gq-num">${String(n).padStart(2,'0')}</span>
          <div class="gq-alts">
            ${[...labels].map(L => `
              <button class="alt-btn ${g.corretas.includes(L) ? 'sel' : ''}"
                onclick="toggleAlt('${n}','${L}',this)">${L}</button>`).join('')}
          </div>
        </div>
        <div class="gq-extras">
          <label>
            <input type="checkbox" id="anulada-${n}" ${g.anulada ? 'checked' : ''}
              onchange="toggleAnulada('${n}', this)"> Anulada
          </label>
          <label>Pontos:
            <input type="number" id="pontos-${n}" value="${g.pontos}" min="0" step="0.25">
          </label>
        </div>
      </div>`;
  }).join('');
}

function toggleAlt(n, L, btn) {
  if (!GABARITO[n]) GABARITO[n] = {corretas: [], anulada: false, pontos: 1};
  const c = GABARITO[n].corretas;
  const i = c.indexOf(L);
  if (i >= 0) { c.splice(i, 1); btn.classList.remove('sel'); }
  else { c.push(L); btn.classList.add('sel'); }
}

function toggleAnulada(n, chk) {
  if (!GABARITO[n]) GABARITO[n] = {corretas: [], anulada: false, pontos: 1};
  GABARITO[n].anulada = chk.checked;
  document.getElementById('gq-' + n).classList.toggle('anulada-on', chk.checked);
}

async function salvarGabarito() {
  const nums = Object.keys(LABELS);
  const gabarito = {};
  const semResposta = [];
  for (const n of nums) {
    const g = GABARITO[n] || {corretas: [], anulada: false, pontos: 1};
    g.pontos = parseFloat(document.getElementById('pontos-' + n).value) || 1;
    g.anulada = document.getElementById('anulada-' + n).checked;
    if (!g.anulada && !g.corretas.length) semResposta.push(n);
    gabarito[n] = g;
  }
  const erro = document.getElementById('gab-erro');
  const ok = document.getElementById('gab-ok');
  if (semResposta.length) {
    erro.textContent = 'Questões sem resposta correta (marque uma letra ou anule): ' + semResposta.join(', ');
    erro.style.display = 'block';
    ok.style.display = 'none';
    return;
  }
  const res = await fetch(`/api/quizzes/${QUIZ_ID}/gabarito`, {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({gabarito})
  });
  const data = await res.json();
  if (data.erro) {
    erro.textContent = data.erro; erro.style.display = 'block'; ok.style.display = 'none';
  } else {
    erro.style.display = 'none'; ok.style.display = 'block';
    setTimeout(() => ok.style.display = 'none', 2500);
  }
}

// ─── SCAN ───
async function escanear(input) {
  const foto = input.files[0];
  if (!foto) return;
  input.value = '';
  document.getElementById('scan-loading').style.display = 'block';
  document.getElementById('scan-erro').style.display = 'none';
  const form = new FormData();
  form.append('foto', foto);
  try {
    const res = await fetch(`/api/quizzes/${QUIZ_ID}/scan`, {method:'POST', body:form});
    const data = await res.json();
    if (data.erro) throw new Error(data.erro);
    mostrarScanResultado(data);
  } catch(e) {
    const el = document.getElementById('scan-erro');
    el.textContent = '❌ ' + e.message;
    el.style.display = 'block';
  } finally {
    document.getElementById('scan-loading').style.display = 'none';
  }
}

function badgesDe(d) {
  return `
    <span class="sbadge sb-verde">✔ ${d.acertos}</span>
    <span class="sbadge sb-vermelho">✘ ${d.erros}</span>
    ${d.brancos ? `<span class="sbadge sb-cinza">⬜ ${d.brancos} branco</span>` : ''}
    ${d.multimarcadas ? `<span class="sbadge sb-laranja">⚠ ${d.multimarcadas} multim.</span>` : ''}
    ${d.anuladas ? `<span class="sbadge sb-dourado">◎ ${d.anuladas} anulada</span>` : ''}
    ${d.aluno_id ? `<span class="sbadge sb-azul">🆔 ${d.aluno_id}</span>` : ''}
    ${d.versao ? `<span class="sbadge sb-azul">Versão ${d.versao}</span>` : ''}`;
}

function mostrarScanResultado(d) {
  const area = document.getElementById('scan-resultado-area');
  area.innerHTML = `
    <div class="card">
      <div class="scan-resultado">
        <div class="nota">${d.nota.toFixed(1)}</div>
        <div class="sub-nota">de ${d.pontos_total.toFixed(1)} pontos</div>
        <div class="scan-badges">${badgesDe(d)}</div>
      </div>
      ${d.nome_img ? `<img class="nome-crop" src="data:image/jpeg;base64,${d.nome_img}">` : ''}
      <img class="debug-img" src="data:image/jpeg;base64,${d.debug_img}">
      <button class="btn btn-azul" style="margin-top:12px"
        onclick="document.getElementById('input-foto').click()">
        📷 Escanear próxima folha
      </button>
    </div>`;
  area.scrollIntoView({behavior:'smooth'});
}

// ─── CORREÇÕES ───
async function carregarScans() {
  const res = await fetch(`/api/quizzes/${QUIZ_ID}/scans`);
  const scans = await res.json();
  const el = document.getElementById('lista-scans');
  if (!scans.length) {
    el.innerHTML = '<div class="vazio">Nenhuma prova corrigida ainda.</div>';
    return;
  }
  el.innerHTML = scans.map(s => `
    <div class="scan-item" onclick="abrirScan(${s.id})">
      ${s.nome_img ? `<img src="data:image/jpeg;base64,${s.nome_img}">` : ''}
      <div class="info">
        <div class="nota-mini">${s.nota.toFixed(1)}</div>
        <div class="meta">✔${s.acertos} ✘${s.erros}${s.brancos?` ⬜${s.brancos}`:''}${s.multimarcadas?` ⚠${s.multimarcadas}`:''}${s.aluno_id?` · 🆔${s.aluno_id}`:''}${s.versao?` · v.${s.versao}`:''}</div>
        <div class="meta">${new Date(s.criado_em.replace(' ','T') + 'Z').toLocaleString('pt-BR')}</div>
      </div>
      <button class="del" onclick="event.stopPropagation(); deletarScan(${s.id})">🗑</button>
    </div>`).join('');
}

async function deletarScan(id) {
  if (!confirm('Apagar esta correção?')) return;
  await fetch(`/api/scans/${id}`, {method:'DELETE'});
  carregarScans();
}

async function abrirScan(id) {
  const res = await fetch(`/api/scans/${id}`);
  const d = await res.json();
  document.getElementById('modal-scan-conteudo').innerHTML = `
    <div class="scan-resultado">
      <div class="nota">${d.nota.toFixed(1)}</div>
      <div class="scan-badges">${badgesDe(d)}</div>
    </div>
    ${d.nome_img ? `<img class="nome-crop" src="data:image/jpeg;base64,${d.nome_img}">` : ''}
    <table class="tabela">
      <thead><tr><th>Q</th><th>Marcou</th><th>Corretas</th><th>Pts</th><th>Status</th></tr></thead>
      <tbody>
        ${d.detalhe.map(x => `
          <tr>
            <td><b>${x.questao}</b></td>
            <td>${x.marcadas.length ? x.marcadas.join(',') : '—'}</td>
            <td>${x.anulada ? '◎' : (x.corretas || []).join(',')}</td>
            <td>${x.pontos ?? 1}</td>
            <td><span class="tag t-${x.status}">${x.status}</span></td>
          </tr>`).join('')}
      </tbody>
    </table>
    <img class="debug-img" src="data:image/jpeg;base64,${d.debug_img}">`;
  document.getElementById('modal-scan').classList.add('aberto');
}

// ─── ESTATÍSTICAS ───
async function carregarStats() {
  const res = await fetch(`/api/quizzes/${QUIZ_ID}/estatisticas`);
  const d = await res.json();
  const el = document.getElementById('stats-area');
  if (!d.n_scans) {
    el.innerHTML = '<div class="vazio">Sem correções ainda.</div>';
    return;
  }
  el.innerHTML = `
    <div class="card">
      <div class="stat-resumo">
        <div class="stat-box"><div class="v">${d.n_scans}</div><div class="l">Provas</div></div>
        <div class="stat-box"><div class="v">${d.media.toFixed(1)}</div><div class="l">Média</div></div>
        <div class="stat-box"><div class="v">${d.maior.toFixed(1)}</div><div class="l">Maior</div></div>
        <div class="stat-box"><div class="v">${d.menor.toFixed(1)}</div><div class="l">Menor</div></div>
      </div>
    </div>
    <div class="card">
      <h2>Desempenho por questão</h2>
      ${d.questoes.map(q => `
        <div class="q-stat">
          <div class="titulo">
            <span>Q${q.questao} ${q.anulada ? '(anulada)' : q.corretas.length ? `(correta: ${q.corretas.join(',')})` : ''}</span>
            <span style="color:var(--verde)">${q.pct_acerto}%</span>
          </div>
          <div class="barra">
            <div class="b-certo" style="width:${q.pct_acerto}%"></div>
            <div class="b-errado" style="width:${q.pct_erro}%"></div>
            <div class="b-branco" style="width:${q.pct_branco}%"></div>
            <div class="b-multi" style="width:${q.pct_multi}%"></div>
          </div>
          <div class="alts">
            Marcações: ${Object.entries(q.alternativas).map(([a,n]) => `${a}:${n}`).join(' · ')}
          </div>
        </div>`).join('')}
      <p style="font-size:0.7rem;color:#94a3b8;margin-top:8px">
        🟩 acerto/anulada · 🟥 erro · ⬜ branco · 🟧 multimarcada
      </p>
    </div>`;
}

carregarGabarito();

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
</script>
</body>
</html>
