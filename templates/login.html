<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#2563eb">
<link rel="manifest" href="/static/manifest.json">
<link rel="icon" href="/static/icon-192.png">
<title>GabaritoApp — Entrar</title>
<style>
  :root { --azul:#2563eb; --azul-esc:#1d4ed8; --texto:#1e293b; --borda:#e2e8f0; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body {
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    background:linear-gradient(160deg,#eff6ff,#dbeafe);
    min-height:100vh; display:flex; align-items:center; justify-content:center;
    padding:16px;
  }
  .card {
    background:white; border-radius:20px; padding:32px 24px;
    width:100%; max-width:400px; box-shadow:0 8px 30px rgba(0,0,0,0.1);
  }
  .logo { text-align:center; margin-bottom:24px; }
  .logo h1 { font-size:1.6rem; color:var(--azul); }
  .logo p { color:#64748b; font-size:0.85rem; margin-top:4px; }
  label { font-size:0.85rem; font-weight:600; color:var(--texto); display:block; margin-bottom:5px; }
  input {
    width:100%; padding:13px; border:1.5px solid var(--borda); border-radius:12px;
    font-size:1rem; margin-bottom:14px;
  }
  input:focus { outline:none; border-color:var(--azul); }
  .btn {
    width:100%; padding:14px; border:none; border-radius:12px; font-size:1rem;
    font-weight:700; cursor:pointer; background:var(--azul); color:white;
  }
  .btn:hover { background:var(--azul-esc); }
  .links { text-align:center; margin-top:16px; font-size:0.85rem; color:#64748b; }
  .links a { color:var(--azul); font-weight:600; cursor:pointer; text-decoration:none; display:block; margin-top:6px; }
  .erro {
    background:#fee2e2; color:#dc2626; padding:12px; border-radius:10px;
    font-size:0.88rem; margin-bottom:14px; display:none;
  }
  .banner-risco {
    background:#fef2f2; border:1.5px solid #fecaca; color:#991b1b;
    padding:10px 12px; border-radius:10px; font-size:0.78rem;
    margin-bottom:14px; line-height:1.4; text-align:center;
  }
  .modo { display:none; }
  .modo.ativo { display:block; }
  .codigo-box {
    background:#fefce8; border:2px dashed #eab308; border-radius:14px;
    padding:18px; text-align:center; margin:14px 0;
  }
  .codigo-box .cod { font-size:1.5rem; font-weight:800; letter-spacing:2px; color:#a16207; }
  .codigo-box p { font-size:0.8rem; color:#854d0e; margin-top:8px; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <h1>📋 GabaritoApp</h1>
    <p>Correção automática de provas</p>
  </div>
  <div class="banner-risco" id="banner-risco" style="display:none">
    ⚠️ Banco de dados temporário ativo — dados podem ser perdidos a qualquer reinício.
  </div>
  <div class="erro" id="erro"></div>

  <!-- LOGIN -->
  <div class="modo ativo" id="modo-login">
    <label>Email</label>
    <input type="email" id="l-email" placeholder="seu@email.com">
    <label>Senha</label>
    <input type="password" id="l-senha" placeholder="Sua senha">
    <button class="btn" onclick="fazerLogin()">Entrar</button>
    <div class="links">
      <a onclick="mudarModo('cadastro')">Não tem conta? Cadastre-se</a>
      <a onclick="mudarModo('recuperar')">Esqueci minha senha</a>
    </div>
  </div>

  <!-- CADASTRO -->
  <div class="modo" id="modo-cadastro">
    <label>Seu nome</label>
    <input type="text" id="c-nome" placeholder="Nome completo">
    <label>Email</label>
    <input type="email" id="c-email" placeholder="seu@email.com">
    <label>Senha</label>
    <input type="password" id="c-senha" placeholder="Mínimo 6 caracteres">
    <button class="btn" onclick="fazerCadastro()">Criar conta</button>
    <div class="links">
      <a onclick="mudarModo('login')">Já tem conta? Entrar</a>
    </div>
  </div>

  <!-- CÓDIGO PÓS-CADASTRO -->
  <div class="modo" id="modo-codigo">
    <h3 style="text-align:center;margin-bottom:8px">⚠️ Guarde este código!</h3>
    <div class="codigo-box">
      <div class="cod" id="codigo-mostrado">----</div>
      <p>Este é o seu <b>código de recuperação de senha</b>.
      Anote em local seguro — ele é a única forma de recuperar sua conta
      se esquecer a senha. Ele não será mostrado novamente.</p>
    </div>
    <button class="btn" onclick="location.href='/dashboard'">Anotei, continuar →</button>
  </div>

  <!-- RECUPERAR SENHA -->
  <div class="modo" id="modo-recuperar">
    <p style="font-size:0.85rem;color:#64748b;margin-bottom:12px">
      Use o código de recuperação que você recebeu ao criar a conta.
    </p>
    <label>Email</label>
    <input type="email" id="r-email" placeholder="seu@email.com">
    <label>Código de recuperação</label>
    <input type="text" id="r-codigo" placeholder="XXXX-XXXX" style="text-transform:uppercase">
    <label>Nova senha</label>
    <input type="password" id="r-senha" placeholder="Mínimo 6 caracteres">
    <button class="btn" onclick="recuperar()">Redefinir senha</button>
    <div class="links">
      <a onclick="mudarModo('login')">← Voltar ao login</a>
    </div>
  </div>

  <!-- NOVO CÓDIGO PÓS-RECUPERAÇÃO -->
  <div class="modo" id="modo-novocodigo">
    <h3 style="text-align:center;margin-bottom:8px">✅ Senha redefinida!</h3>
    <div class="codigo-box">
      <div class="cod" id="novo-codigo-mostrado">----</div>
      <p>Este é o seu <b>novo código de recuperação</b> (o antigo não vale mais).
      Anote em local seguro.</p>
    </div>
    <button class="btn" onclick="mudarModo('login')">Ir para o login →</button>
  </div>
</div>

<script>
function mudarModo(m) {
  document.querySelectorAll('.modo').forEach(x => x.classList.remove('ativo'));
  document.getElementById('modo-' + m).classList.add('ativo');
  document.getElementById('erro').style.display = 'none';
}

function mostrarErro(msg) {
  const el = document.getElementById('erro');
  el.textContent = msg;
  el.style.display = 'block';
}

async function fazerLogin() {
  try {
    const res = await fetch('/api/login', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        email: document.getElementById('l-email').value.trim(),
        senha: document.getElementById('l-senha').value,
      })
    });
    const data = await res.json();
    if (data.erro) throw new Error(data.erro);
    location.href = '/dashboard';
  } catch(e) { mostrarErro(e.message); }
}

async function fazerCadastro() {
  try {
    const res = await fetch('/api/registrar', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        nome: document.getElementById('c-nome').value.trim(),
        email: document.getElementById('c-email').value.trim(),
        senha: document.getElementById('c-senha').value,
      })
    });
    const data = await res.json();
    if (data.erro) throw new Error(data.erro);
    document.getElementById('codigo-mostrado').textContent = data.codigo_recuperacao;
    mudarModo('codigo');
  } catch(e) { mostrarErro(e.message); }
}

async function recuperar() {
  try {
    const res = await fetch('/api/recuperar', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        email: document.getElementById('r-email').value.trim(),
        codigo: document.getElementById('r-codigo').value.trim(),
        nova_senha: document.getElementById('r-senha').value,
      })
    });
    const data = await res.json();
    if (data.erro) throw new Error(data.erro);
    document.getElementById('novo-codigo-mostrado').textContent = data.novo_codigo;
    mudarModo('novocodigo');
  } catch(e) { mostrarErro(e.message); }
}

document.getElementById('l-senha').addEventListener('keydown', e => {
  if (e.key === 'Enter') fazerLogin();
});

fetch('/api/status').then(r => r.json()).then(d => {
  if (!d.banco_permanente) document.getElementById('banner-risco').style.display = 'block';
}).catch(()=>{});
</script>
</body>
</html>
