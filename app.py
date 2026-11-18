"""
GabaritoApp v3
- Folhas de resposta personalizáveis (cabeçalho, matrícula, versão, blocos de questões)
- Código localizador + QR + link de compartilhamento entre professores
- PDF com 1 ou 2 cópias por página e níveis de escurecimento
- Quizzes vinculados a folhas, com gabarito avançado:
  questão anulada, múltiplas alternativas corretas, pontuação por questão
- Recuperação de senha por código
"""

import base64
import io
import json
import math
import os
import secrets
import sqlite3
import string
import time
import re
from datetime import datetime, timedelta
from functools import wraps

import cv2
import numpy as np
import qrcode
from flask import (Flask, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

DB_PATH = os.environ.get("DB_PATH", "gabarito.db")

# ══════════════════════════════════════════════
# BANCO DE DADOS (SQLite local / PostgreSQL no Render)
# ══════════════════════════════════════════════

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USA_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

# ── Segurança do cookie de login e cabeçalhos ──
# Em produção (Render, com PostgreSQL) o site é sempre HTTPS. Ali o cookie de
# login ganha a marca "Secure" (só viaja criptografado). No computador, rodando
# local em http://, essa marca impediria o login — por isso só vale em produção.
EM_PRODUCAO = USA_POSTGRES or bool(os.environ.get("RENDER"))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,     # o JavaScript da página não lê o cookie
    SESSION_COOKIE_SAMESITE="Lax",    # outro site não consegue agir "em nome" do professor
    SESSION_COOKIE_SECURE=EM_PRODUCAO,
)

if USA_POSTGRES:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


class CursorWrapper:
    def __init__(self, cur, lastrowid=None):
        self._cur = cur
        self.lastrowid = lastrowid

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class PgConnWrapper:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=RealDictCursor)
        sql_pg = sql.replace("?", "%s")
        eh_insert = sql_pg.strip().upper().startswith("INSERT")
        if eh_insert:
            sql_pg += " RETURNING id"
        cur.execute(sql_pg, params)
        lastrowid = cur.fetchone()["id"] if eh_insert else None
        return CursorWrapper(cur, lastrowid)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_db():
    if "db" not in g:
        if USA_POSTGRES:
            g.db = PgConnWrapper(psycopg2.connect(DATABASE_URL))
        else:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _exec_ignore(conn_exec, sql):
    """Executa SQL ignorando erro (para migrações ALTER TABLE idempotentes)."""
    try:
        conn_exec(sql)
        return True
    except Exception:
        return False


def init_db():
    serial = "SERIAL PRIMARY KEY" if USA_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts = "TIMESTAMP" if USA_POSTGRES else "TEXT"
    tabelas = f"""
    CREATE TABLE IF NOT EXISTS users (
        id {serial},
        nome TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        senha_hash TEXT NOT NULL,
        codigo_recuperacao TEXT,
        criado_em {ts} DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS folhas (
        id {serial},
        user_id INTEGER NOT NULL REFERENCES users(id),
        nome TEXT NOT NULL,
        config TEXT NOT NULL,
        layout TEXT NOT NULL,
        share_token TEXT,
        copiada_de_token TEXT,
        criado_em {ts} DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS folhas_compartilhadas (
        id {serial},
        user_id INTEGER NOT NULL REFERENCES users(id),
        folha_id INTEGER NOT NULL REFERENCES folhas(id)
    );
    CREATE TABLE IF NOT EXISTS quizzes (
        id {serial},
        user_id INTEGER NOT NULL REFERENCES users(id),
        nome TEXT NOT NULL,
        n_questoes INTEGER NOT NULL,
        gabarito TEXT DEFAULT '{{}}',
        layout TEXT NOT NULL,
        folha_id INTEGER,
        criado_em {ts} DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS tentativas (
        id {serial},
        chave TEXT NOT NULL,
        momento BIGINT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tentativas_chave ON tentativas (chave);
    CREATE TABLE IF NOT EXISTS scans (
        id {serial},
        quiz_id INTEGER NOT NULL REFERENCES quizzes(id),
        respostas TEXT NOT NULL,
        acertos INTEGER, erros INTEGER, brancos INTEGER, multimarcadas INTEGER,
        nota REAL,
        debug_img TEXT,
        nome_img TEXT,
        caixas_img TEXT,
        aluno_id TEXT,
        versao TEXT,
        criado_em {ts} DEFAULT CURRENT_TIMESTAMP
    );
    """
    if USA_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        # O Render liga o app em 2 processos AO MESMO TEMPO, e os dois rodam
        # esta preparação do banco. Quando há uma tabela nova para criar, os
        # dois tentam criá-la juntos e um deles quebra ("already exists"),
        # derrubando a inicialização. Este "cadeado" do PostgreSQL faz um
        # processo esperar o outro terminar. Ele é solto sozinho quando a
        # conexão fecha, no fim desta função.
        cur.execute("SELECT pg_advisory_lock(727274)")
        cur.execute(tabelas)
        conn.commit()
        # migrações de versões antigas
        for alter in [
            "ALTER TABLE users ADD COLUMN codigo_recuperacao TEXT",
            "ALTER TABLE quizzes ADD COLUMN folha_id INTEGER",
            "ALTER TABLE quizzes ADD COLUMN gabarito_ts BIGINT DEFAULT 0",
            "ALTER TABLE scans ADD COLUMN aluno_id TEXT",
            "ALTER TABLE scans ADD COLUMN versao TEXT",
            "ALTER TABLE scans ADD COLUMN caixas_img TEXT",
            "ALTER TABLE folhas ADD COLUMN copiada_de_token TEXT",
            "ALTER TABLE scans ADD COLUMN img_recortada INTEGER DEFAULT 0",
        ]:
            try:
                cur.execute(alter)
                conn.commit()
            except Exception:
                conn.rollback()
        conn.close()
    else:
        db = sqlite3.connect(DB_PATH)
        db.executescript(tabelas)
        db.commit()
        for alter in [
            "ALTER TABLE users ADD COLUMN codigo_recuperacao TEXT",
            "ALTER TABLE quizzes ADD COLUMN folha_id INTEGER",
            "ALTER TABLE quizzes ADD COLUMN gabarito_ts INTEGER DEFAULT 0",
            "ALTER TABLE scans ADD COLUMN aluno_id TEXT",
            "ALTER TABLE scans ADD COLUMN versao TEXT",
            "ALTER TABLE scans ADD COLUMN caixas_img TEXT",
            "ALTER TABLE folhas ADD COLUMN copiada_de_token TEXT",
            "ALTER TABLE scans ADD COLUMN img_recortada INTEGER DEFAULT 0",
        ]:
            _exec_ignore(db.execute, alter)
        db.commit()
        db.close()


init_db()


def resolver_localizadores_validos(db, folha_id, copiada_de_token, limite=30):
    """Monta a lista de códigos (localizadores) que devem ser ACEITOS como
    válidos na leitura de uma folha.

    Por que isso existe: compartilhar um gabarito cria uma CÓPIA com id (e
    código de barras) novo — decisão de projeto, para o colega poder editar e
    apagar a dele sem afetar a do dono. Só que, na prática, é comum um só
    professor imprimir a pilha de folhas e vários corrigirem: aí a folha
    impressa carrega o código do DONO, mas o app do colega só aceitava o
    código da cópia dele.

    Aqui subimos a cadeia de "copiada_de_token" (a cópia guarda o token de
    quem originou ela) e juntamos o id de cada folha da cadeia — dono
    original, e qualquer repasse no meio, se o colega compartilhar de novo.
    `limite` é só uma trava de segurança contra token apontando para si
    mesmo (não deveria acontecer, mas evita loop infinito se acontecer)."""
    ids = {folha_id}
    token = copiada_de_token
    for _ in range(limite):
        if not token:
            break
        origem = db.execute(
            "SELECT id, copiada_de_token FROM folhas WHERE share_token=?", (token,)
        ).fetchone()
        if not origem or origem["id"] in ids:
            break
        ids.add(origem["id"])
        token = origem["copiada_de_token"]
    return sorted(ids)


def migrar_localizadores_validos():
    """Preenche 'localizadores_validos' nas folhas antigas (de antes desta
    correção), que não têm esse campo no layout salvo. Sem isso, só as
    folhas criadas/compartilhadas DEPOIS do deploy ganhariam a correção —
    as que já existiam continuariam recusando a folha do dono."""
    if USA_POSTGRES:
        db = PgConnWrapper(psycopg2.connect(DATABASE_URL))
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        db = conn
    try:
        folhas = db.execute("SELECT id, layout, copiada_de_token FROM folhas").fetchall()
        for f in folhas:
            try:
                layout = json.loads(f["layout"]) if f["layout"] else {}
            except Exception:
                continue
            if not layout or "localizadores_validos" in layout:
                continue
            layout["localizadores_validos"] = resolver_localizadores_validos(
                db, f["id"], f["copiada_de_token"])
            db.execute("UPDATE folhas SET layout=? WHERE id=?", (json.dumps(layout), f["id"]))
        db.commit()
    finally:
        db.close()


migrar_localizadores_validos()

if USA_POSTGRES:
    print("=" * 60)
    print("✅ BANCO PERMANENTE (PostgreSQL) conectado com sucesso.")
    print("   Os dados sobrevivem a reinícios e deploys.")
    print("=" * 60)
else:
    print("=" * 60)
    print("⚠️  ATENÇÃO: rodando com banco TEMPORÁRIO (SQLite local).")
    print("   TODOS OS DADOS SERÃO PERDIDOS no próximo redeploy/reinício!")
    print("   Configure a variável DATABASE_URL para usar o PostgreSQL.")
    print("=" * 60)


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"erro": "Não autenticado"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return wrapper


@app.after_request
def cabecalhos_de_seguranca(resp):
    """Cabeçalhos que o navegador obedece para proteger as páginas.
    - nosniff: o navegador não "adivinha" o tipo do arquivo (evita que um
      texto seja executado como script);
    - X-Frame-Options: nenhum outro site pode mostrar o app dentro de um
      quadro escondido para enganar o professor a clicar em algo;
    - Referrer-Policy: os endereços do app (inclusive o link de compartilhar
      gabarito) não vazam para outros sites;
    - HSTS (só em produção): o celular passa a usar sempre HTTPS."""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if EM_PRODUCAO:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


@app.errorhandler(413)
def envio_grande_demais(_e):
    return jsonify({"erro": "Envio grande demais. Se estiver sincronizando muitas "
                            "provas de uma vez, tente de novo: o app manda em partes."}), 413


@app.errorhandler(500)
def erro_interno(_e):
    # Nas rotas da API, o app espera JSON. Sem isto o servidor devolvia uma
    # página HTML de erro e o app mostrava uma mensagem sem sentido.
    if request.path.startswith("/api/"):
        return jsonify({"erro": "Erro inesperado no servidor. Tente de novo."}), 500
    return "Erro inesperado no servidor.", 500


def _json():
    """Corpo da requisição como dicionário — ou {} se vier vazio, quebrado ou
    em outro formato. Antes, um corpo inválido derrubava a rota com erro 500."""
    dados = request.get_json(silent=True)
    return dados if isinstance(dados, dict) else {}


def _int_arg(nome, padrao, minimo, maximo):
    """Número inteiro vindo do endereço (?copias=2), dentro de uma faixa.
    Valor inválido (ex.: ?copias=abc) vira o padrão em vez de erro 500."""
    try:
        v = int(request.args.get(nome, padrao))
    except (TypeError, ValueError):
        return padrao
    return max(minimo, min(maximo, v))


def _texto(valor, limite):
    """Texto limpo: sem caracteres de controle e com tamanho máximo."""
    if not isinstance(valor, str):
        valor = "" if valor is None else str(valor)
    valor = re.sub(r"[\x00-\x1f\x7f]", " ", valor).strip()
    return valor[:limite]


# ══════════════════════════════════════════════
# LIMITE DE TENTATIVAS (senha, código de recuperação, cadastro)
# ══════════════════════════════════════════════
#
# Sem limite, um robô podia testar milhares de senhas por minuto (nos testes:
# 40 senhas erradas em menos de 3 segundos, sem bloqueio nenhum). As
# tentativas ficam no BANCO, e não na memória do servidor, porque o Render roda
# o app em 2 processos ao mesmo tempo e reinicia sozinho: na memória cada
# processo teria a sua conta e ela zeraria a cada reinício.

JANELA_TENTATIVAS = 15 * 60          # segundos


def _ip_cliente():
    """IP de quem fez o pedido. No Render o app fica atrás de um intermediário,
    então o IP verdadeiro vem no cabeçalho X-Forwarded-For. Esse cabeçalho pode
    ser falsificado — por isso o limite que realmente protege é o por email; o
    por IP é só uma camada a mais."""
    xff = request.headers.get("X-Forwarded-For", "")
    ip = xff.split(",")[0].strip() if xff else ""
    return (ip or request.remote_addr or "?")[:64]


def _tentativas_recentes(db, chave, janela):
    desde = int(time.time()) - janela
    return db.execute("SELECT COUNT(*) AS n FROM tentativas WHERE chave=? AND momento>?",
                      (chave, desde)).fetchone()["n"]


def _bloqueado(db, limites, janela=JANELA_TENTATIVAS):
    """limites: [(chave, máximo), ...]. True se alguma chave estourou."""
    return any(_tentativas_recentes(db, ch, janela) >= mx for ch, mx in limites)


def _anotar_tentativa(db, *chaves):
    agora = int(time.time())
    for ch in chaves:
        db.execute("INSERT INTO tentativas (chave, momento) VALUES (?,?)", (ch, agora))
    # faxina: o que tem mais de um dia não serve mais para nada
    db.execute("DELETE FROM tentativas WHERE momento<?", (agora - 86400,))
    db.commit()


def _esquecer_tentativas(db, chave):
    db.execute("DELETE FROM tentativas WHERE chave=?", (chave,))
    db.commit()


def _resposta_bloqueio():
    return jsonify({"erro": "Muitas tentativas erradas. Por segurança, espere "
                            "15 minutos e tente de novo."}), 429


def _limites_senha(email):
    # 8 erros por email OU 40 por IP, em 15 minutos
    return [("senha:email:" + email, 8), ("senha:ip:" + _ip_cliente(), 40)]


def _chaves_senha(email):
    return ("senha:email:" + email, "senha:ip:" + _ip_cliente())


# ══════════════════════════════════════════════
# LIMITES DE TAMANHO E QUANTIDADE
# ══════════════════════════════════════════════
#
# Qualquer pessoa pode criar conta, então o servidor precisa se proteger de
# quem tente encher o banco ou travar o servidor de propósito. Os limites
# abaixo ficam MUITO acima do uso normal de um professor.

MAX_FOLHAS_POR_CONTA = 500
MAX_CORRECOES_POR_CONTA = 2000
MAX_PROVAS_POR_CONTA = 20000
MAX_PROVAS_POR_ENVIO = 500
# fotos guardadas (base64). Medido nas provas reais: a foto das bolinhas tem
# de 100 a 400 KB e o recorte do nome uns 30 KB.
MAX_FOTO_PROVA = 1_500_000
MAX_FOTO_CAIXA = 300_000
MAX_GABARITO_JSON = 200_000


def _contar_provas_da_conta(db, uid):
    return db.execute("SELECT COUNT(*) AS n FROM scans s JOIN quizzes q ON q.id=s.quiz_id "
                      "WHERE q.user_id=?", (uid,)).fetchone()["n"]


def _limpar_gabarito(gab):
    """O gabarito é guardado como veio do app. Aqui só garantimos que é um
    dicionário de tamanho razoável (senão vira um gabarito vazio)."""
    if not isinstance(gab, dict):
        return {}
    try:
        if len(json.dumps(gab)) > MAX_GABARITO_JSON:
            return {}
    except (TypeError, ValueError):
        return {}
    return gab


def _num(v, inteiro=False):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0
    if x != x or x in (float("inf"), float("-inf")):
        return 0
    return int(x) if inteiro else round(x, 4)


def _limpar_resultado(r):
    """Confere uma prova corrigida no celular antes de guardar.

    Importante: NUNCA descarta a prova em si. Se uma foto vier grande demais,
    só a FOTO fica de fora — a nota e as respostas são guardadas. Assim um
    limite nunca faz o professor perder uma correção."""
    if not isinstance(r, dict):
        return None
    detalhe = r.get("detalhe") if isinstance(r.get("detalhe"), list) else []
    detalhe = [d for d in detalhe[:300] if isinstance(d, dict)]
    try:
        if len(json.dumps(detalhe)) > 300_000:
            detalhe = []
    except (TypeError, ValueError):
        detalhe = []

    def foto(v, limite):
        v = so_base64(v) if isinstance(v, str) else None
        return v if v and len(v) <= limite else None

    caixas = []
    for cx in (r.get("caixas_img") if isinstance(r.get("caixas_img"), list) else [])[:8]:
        if isinstance(cx, dict):
            caixas.append({"rotulo": _texto(cx.get("rotulo"), 40),
                           "img": foto(cx.get("img"), MAX_FOTO_CAIXA)})
    # matrícula só tem dígitos (e "·" no dígito que não foi lido); versão só letras
    aluno = r.get("aluno_id")
    aluno = re.sub(r"[^0-9·]", "", aluno)[:12] or None if isinstance(aluno, str) else None
    versao = r.get("versao")
    versao = re.sub(r"[^A-Z]", "", versao.upper())[:6] or None if isinstance(versao, str) else None
    return {
        "detalhe": detalhe,
        "acertos": _num(r.get("acertos"), True), "erros": _num(r.get("erros"), True),
        "brancos": _num(r.get("brancos"), True), "multimarcadas": _num(r.get("multimarcadas"), True),
        "nota": _num(r.get("nota")),
        "debug_img": foto(r.get("debug_img"), MAX_FOTO_PROVA),
        "nome_img": foto(r.get("nome_img"), MAX_FOTO_CAIXA),
        "caixas_img": caixas,
        "aluno_id": aluno, "versao": versao,
        "img_recortada": 1 if r.get("img_recortada") else 0,
    }


def validar_config(config):
    """Confere e LIMPA a configuração de um gabarito novo. Devolve (config, erro).

    Por que isso importa: o nome do gabarito e os rótulos das caixas aparecem
    na tela de OUTROS professores quando o gabarito é compartilhado. Antes
    aceitávamos qualquer coisa — inclusive campos extras de megabytes e
    alternativas com símbolos. Agora só passam os campos que o app usa, com
    tamanho limitado, e as alternativas só podem ter letras e números."""
    if not isinstance(config, dict):
        return None, "Configuração inválida."
    nome = _texto(config.get("nome"), 200)
    if not nome:
        return None, "Dê um nome à folha."
    if len(nome) > 100:
        return None, "O nome do gabarito pode ter no máximo 100 caracteres."

    cab = []
    for c in (config.get("cabecalho") if isinstance(config.get("cabecalho"), list) else [])[:6]:
        if not isinstance(c, dict):
            continue
        rot = _texto(c.get("rotulo"), 30)
        larg = c.get("largura") if c.get("largura") in ("pequena", "media", "grande") else "media"
        cab.append({"rotulo": rot, "largura": larg, "ativo": bool(c.get("ativo")) and bool(rot)})

    ida = config.get("id_aluno") if isinstance(config.get("id_aluno"), dict) else {}
    try:
        dig = max(3, min(8, int(ida.get("digitos", 4))))
    except (TypeError, ValueError):
        dig = 4
    id_aluno = {"ativo": bool(ida.get("ativo")), "digitos": dig,
                "rotulo": _texto(ida.get("rotulo"), 30) or "Matrícula"}

    ver = config.get("versao") if isinstance(config.get("versao"), dict) else {}
    letras = ""
    for ch in str(ver.get("letras") or "AB").upper():
        if "A" <= ch <= "Z" and ch not in letras:
            letras += ch
    letras = letras[:6] or "AB"
    if ver.get("ativo") and len(letras) < 2:
        return None, "A versão de prova precisa de pelo menos 2 letras (ex.: AB)."
    versao = {"ativo": bool(ver.get("ativo")), "letras": letras,
              "rotulo": _texto(ver.get("rotulo"), 30) or "Versão"}

    questoes_in = config.get("questoes") if isinstance(config.get("questoes"), list) else []
    if not questoes_in or len(questoes_in) > 150:
        return None, "Adicione entre 1 e 150 questões."
    questoes, vistos = [], set()
    for q in questoes_in:
        if not isinstance(q, dict):
            return None, "Lista de questões inválida."
        try:
            num = int(q.get("numero"))
        except (TypeError, ValueError):
            return None, "Número de questão inválido."
        labels = str(q.get("labels") or "").upper().strip()
        if not (1 <= num <= 999) or num in vistos:
            return None, "Número de questão inválido ou repetido."
        if not re.fullmatch(r"[A-Z0-9]{2,10}", labels) or len(set(labels)) != len(labels):
            return None, ("As alternativas precisam ter de 2 a 10 letras ou números, "
                          "sem repetir (ex.: ABCDE).")
        vistos.add(num)
        questoes.append({"numero": num, "labels": labels})

    formato = "compacta" if config.get("formato") == "compacta" else "padrao"
    limpo = {"nome": nome, "cabecalho": cab, "id_aluno": id_aluno, "versao": versao,
             "questoes": questoes}
    # só grava "formato" quando veio: mantém idêntica a configuração dos
    # gabaritos criados pelo app antigo (que não mandava esse campo)
    if "formato" in config:
        limpo["formato"] = formato
    return limpo, None


def codigo_barras(folha_id):
    """Número que vai no código de barras lateral da folha.

    O código de barras tem 12 barras de dados: só cabe de 1 a 4095. Antes ele
    gravava o número da folha "cortado", e a partir da folha 4096 dava errado
    (a 4096 virava 0, ilegível; a 4097 virava 1 e TODA folha impressa era
    recusada como "de outro gabarito").

    Agora o número dá a volta: 4096 vira 1, 4097 vira 2... Até 4095 nada muda —
    as folhas já impressas continuam valendo exatamente como estão. O código de
    barras serve para avisar "folha de outro gabarito"; dois gabaritos só teriam
    o mesmo código se os números deles diferirem em exatamente 4095."""
    return (int(folha_id) - 1) % 4095 + 1


# ══════════════════════════════════════════════
# CONSTRUÇÃO DE FOLHAS (layout + PDF)
# ══════════════════════════════════════════════

PAGE_W, PAGE_H = A4
MARGIN = 12 * mm
MARKER_SIZE = 10 * mm
CONTENT_X = 24 * mm
CONTENT_W = PAGE_W - 43 * mm
BUBBLE_R = 4.1 * mm
BUBBLE_SP = 11.5 * mm
ROW_H = 11.5 * mm
DIGIT_R = 2.2 * mm
DIGIT_VSP = 5.4 * mm
DIGIT_CSP = 6.8 * mm

LARGURAS = {"grande": 1.0, "media": 0.48, "pequena": 0.30}


def fr(x, y):
    """Converte coords pt (origem inferior-esq) em frações da página."""
    return {"x": x / PAGE_W, "y": y / PAGE_H}


def compute_layout(config, folha_id):
    """
    Calcula posições de todos os elementos.
    Retorna (layout, erro). Coordenadas em frações; y cresce pra cima (padrão PDF).
    """
    # Folha compacta (para recortar e colar na prova) tem regras próprias de
    # tamanho; o resultado tem o mesmo formato, então a leitura é a mesma.
    if config.get("formato") == "compacta":
        return compute_layout_compacto(config, folha_id)
    layout = {
        "tipo": "folha",
        "folha_id": folha_id,
        "page_width_pt": PAGE_W,
        "page_height_pt": PAGE_H,
        "marker_size_pt": MARKER_SIZE,
        "bubble_radius_pt": BUBBLE_R,
        "digit_radius_pt": DIGIT_R,
        "questoes": {},
        "labels_questoes": {},
    }

    # marcadores
    positions = [
        (MARGIN, PAGE_H - MARGIN - MARKER_SIZE),
        (PAGE_W - MARGIN - MARKER_SIZE, PAGE_H - MARGIN - MARKER_SIZE),
        (MARGIN, MARGIN),
        (PAGE_W - MARGIN - MARKER_SIZE, MARGIN),
    ]
    layout["markers"] = [fr(x, y) for x, y in positions]

    # QR topo central
    qr_size = 15 * mm
    qr_x = PAGE_W / 2 - qr_size / 2
    qr_y = PAGE_H - MARGIN - qr_size
    layout["qr"] = {"x": qr_x / PAGE_W, "y": qr_y / PAGE_H,
                    "w": qr_size / PAGE_W, "h": qr_size / PAGE_H}

    # ─── código de barras lateral (identifica o gabarito, legível offline) ───
    # 14 células verticais na margem esquerda: 12 bits do folha_id + 2 bits de paridade.
    BC_N = 14
    bc_w = 5 * mm
    bc_h = 4 * mm
    bc_gap = 2.5 * mm
    bc_total = BC_N * bc_h + (BC_N - 1) * bc_gap
    bc_x = MARGIN
    bc_y_topo = PAGE_H / 2 + bc_total / 2
    cod = codigo_barras(folha_id)
    bits = [(cod >> (11 - i)) & 1 for i in range(12)]
    p1 = sum(bits[0:6]) % 2
    p2 = sum(bits[6:12]) % 2
    valores = bits + [p1, p2]
    cells = []
    for i in range(BC_N):
        cy_topo = bc_y_topo - i * (bc_h + bc_gap)
        cells.append({
            "x": bc_x / PAGE_W, "y": (cy_topo - bc_h) / PAGE_H,
            "w": bc_w / PAGE_W, "h": bc_h / PAGE_H,
            "v": valores[i],
        })
    layout["barcode"] = {"cells": cells, "bits": BC_N}

    y = qr_y - 6 * mm  # linha do título
    layout["titulo_y"] = y / PAGE_H
    y -= 9 * mm

    # ─── caixas de cabeçalho ───
    caixas = [c for c in config.get("cabecalho", []) if c.get("ativo")]
    layout["caixas"] = []
    cursor_x = CONTENT_X
    linha_y = y
    caixa_h = 11.5 * mm
    label_h = 6.0 * mm
    for c in caixas:
        w = LARGURAS.get(c.get("largura", "media"), 0.48) * CONTENT_W
        if cursor_x + w > CONTENT_X + CONTENT_W + 1:
            cursor_x = CONTENT_X
            linha_y -= (caixa_h + label_h + 4 * mm)
        box_y = linha_y - caixa_h - label_h
        layout["caixas"].append({
            "rotulo": c["rotulo"],
            "x": cursor_x / PAGE_W, "y": box_y / PAGE_H,
            "w": w / PAGE_W, "h": caixa_h / PAGE_H,
        })
        cursor_x += w + 4 * mm
    if caixas:
        y = linha_y - caixa_h - label_h - 6 * mm

    # ─── matrícula (ID) e versão ───
    id_cfg = config.get("id_aluno", {})
    ver_cfg = config.get("versao", {})
    secao_top = y
    secao_bottom = y

    if id_cfg.get("ativo"):
        digitos = int(id_cfg.get("digitos", 4))
        layout["id_digitos"] = {}
        x0 = CONTENT_X
        top = secao_top - 5 * mm
        for d in range(digitos):
            cx = x0 + 6 * mm + d * DIGIT_CSP
            layout["id_digitos"][str(d)] = {}
            for n in range(10):
                cy = top - 4 * mm - n * DIGIT_VSP
                layout["id_digitos"][str(d)][str(n)] = fr(cx, cy)
        layout["id_rotulo"] = id_cfg.get("rotulo", "Matrícula")
        layout["id_top_y"] = top / PAGE_H
        secao_bottom = min(secao_bottom, top - 4 * mm - 9 * DIGIT_VSP - 5 * mm)

    if ver_cfg.get("ativo"):
        letras = str(ver_cfg.get("letras", "AB"))[:6]
        layout["versao_bolhas"] = {}
        x0 = PAGE_W - CONTENT_X - len(letras) * BUBBLE_SP
        cy = secao_top - 12 * mm
        for i, L in enumerate(letras):
            cx = x0 + i * BUBBLE_SP + BUBBLE_SP / 2
            layout["versao_bolhas"][L] = fr(cx, cy)
        layout["versao_rotulo"] = ver_cfg.get("rotulo", "Versão")
        layout["versao_y"] = cy / PAGE_H
        secao_bottom = min(secao_bottom, cy - 8 * mm)

    y = secao_bottom - 4 * mm if (id_cfg.get("ativo") or ver_cfg.get("ativo")) else y - 2 * mm

    # ─── questões ───
    questoes = config.get("questoes", [])
    if not questoes:
        return None, "Adicione pelo menos uma questão."
    max_labels = max(len(q["labels"]) for q in questoes)
    nq = len(questoes)

    header_y = y
    y_min_permitido = MARGIN + MARKER_SIZE + 4 * mm
    altura_disp = header_y - y_min_permitido - 6 * mm
    largura_disp = CONTENT_W
    if altura_disp < 10 * mm:
        return None, "Cabeçalho ocupou a página toda — desative algum campo."

    # ─────────────────────────────────────────────────────────────
    # Dimensionamento: a bolinha fica NO MAIOR tamanho que a página
    # aguentar, até o teto confortável. Ela só encolhe quando não há
    # outro jeito — e o mínimo possível. Testamos de 1 a 4 colunas e
    # ficamos com o arranjo que permite a maior bolinha.
    # ─────────────────────────────────────────────────────────────
    D_MAX = 8.2 * mm          # diâmetro confortável padrão (o que você aprovou)
    # Em 1 ou 2 colunas sobra largura, então até 50 questões dá pra deixar a
    # bolinha maior ainda — é o caso mais comum (gabarito de coluna dupla).
    # Com 3-4 colunas ou mais questões o teto continua o de sempre, senão a
    # bolinha esbarra na vizinha.
    D_MAX_COLUNA_DUPLA = 11.0 * mm
    NQ_LIMITE_COLUNA_DUPLA = 50
    D_MIN = 4.2 * mm          # piso: abaixo disso fica difícil de preencher
    K_H = 1.15                # distância entre bolinhas ÷ diâmetro
    K_V = 1.30                # distância entre linhas ÷ diâmetro
    NUM_W = 5.6 * mm          # espaço reservado ao número da questão
    FOLGA = 3 * mm            # respiro entre colunas

    melhor = None
    for n_col in (1, 2, 3, 4):
        linhas = math.ceil(nq / n_col)
        teto = (D_MAX_COLUNA_DUPLA
                if (n_col <= 2 and nq <= NQ_LIMITE_COLUNA_DUPLA)
                else D_MAX)
        # maior diâmetro que cabe na LARGURA desta configuração
        d_larg = (largura_disp / n_col - NUM_W - FOLGA) / ((max_labels - 1) * K_H + 1)
        # maior diâmetro que cabe na ALTURA
        d_alt = (altura_disp / linhas) / K_V
        d = min(d_larg, d_alt, teto)
        if d < D_MIN:
            continue
        # melhor = maior bolinha; empate resolve com menos colunas
        if melhor is None or d > melhor[0] + 0.01:
            melhor = (d, n_col, linhas)

    if melhor is None:
        return None, ("Muitas questões para uma página. Reduza a quantidade "
                      "ou desative algum campo do cabeçalho.")

    diam, n_col, por_col = melhor
    r_b = diam / 2
    sp = diam * K_H
    rh = diam * K_V
    layout["bubble_radius_pt"] = r_b   # a leitura usa este raio

    altura_usada = (por_col - 1) * rh + diam
    topo_questoes = header_y - 6 * mm

    # Centraliza também na horizontal.
    col_w = largura_disp / n_col
    larg_bloco = NUM_W + (max_labels - 1) * sp + diam
    # centraliza levemente dentro da coluna, sem descolar do cabeçalho
    recuo_col = min(max(0, (col_w - larg_bloco - FOLGA) / 2), 6 * mm)

    for col in range(n_col):
        x_base = CONTENT_X + col * col_w + recuo_col
        for row in range(por_col):
            idx = col * por_col + row
            if idx >= nq:
                break
            q = questoes[idx]
            num = str(q["numero"])
            labels = q["labels"]
            qy = topo_questoes - row * rh
            layout["questoes"][num] = {}
            layout["labels_questoes"][num] = labels
            for i, L in enumerate(labels):
                cx = x_base + NUM_W + r_b + i * sp
                layout["questoes"][num][L] = fr(cx, qy)

    header_y = topo_questoes + 6 * mm
    layout["questoes_header_y"] = header_y / PAGE_H
    layout["n_colunas"] = n_col
    layout["por_coluna"] = por_col

    # ─── centraliza o conjunto na página ───
    # Sem isso, tudo fica grudado no topo e a folha parece desequilibrada:
    # uma prova de 10 questões deixaria meia página em branco embaixo.
    # Marcadores de canto e código de barras NÃO se movem (a leitura depende
    # deles estarem sempre no mesmo lugar).
    topo_conteudo = layout["qr"]["y"] * PAGE_H + layout["qr"]["h"] * PAGE_H
    base_conteudo = topo_questoes - (por_col - 1) * rh - r_b
    limite_baixo = MARGIN + MARKER_SIZE + 4 * mm
    sobra = base_conteudo - limite_baixo
    desloc = max(0.0, sobra / 2)

    if desloc > 1:
        d = desloc / PAGE_H
        layout["qr"]["y"] -= d
        layout["titulo_y"] -= d
        for cx in layout.get("caixas", []):
            cx["y"] -= d
        for alts in layout["questoes"].values():
            for L in alts:
                alts[L]["y"] -= d
        if "id_digitos" in layout:
            layout["id_top_y"] -= d
            for nums in layout["id_digitos"].values():
                for n_ in nums:
                    nums[n_]["y"] -= d
        if "versao_bolhas" in layout:
            layout["versao_y"] -= d
            for L in layout["versao_bolhas"]:
                layout["versao_bolhas"][L]["y"] -= d
        layout["questoes_header_y"] -= d

    return layout, None


def desenhar_folha(c, config, layout, folha_id, cinza=0.62, versao_marcada=None):
    """Desenha uma cópia da folha no canvas (coordenadas absolutas A4)."""
    # marcadores sempre pretos
    c.setFillColorRGB(0, 0, 0)
    for m in layout["markers"]:
        c.rect(m["x"] * PAGE_W, m["y"] * PAGE_H, MARKER_SIZE, MARKER_SIZE, fill=1, stroke=0)

    # QR
    qr_img = qrcode.make(f"FOLHA:{folha_id}", box_size=4, border=1)
    qr_buf = io.BytesIO()
    qr_img.save(qr_buf, format="PNG")
    qr_buf.seek(0)
    q = layout["qr"]
    c.drawImage(ImageReader(qr_buf), q["x"] * PAGE_W, q["y"] * PAGE_H,
                q["w"] * PAGE_W, q["h"] * PAGE_H)

    # título
    c.setFillColorRGB(0, 0, 0)
    loc = f"{folha_id:04d}"
    c.setFont("Helvetica-Bold", 13)
    nome_titulo = config["nome"]
    if versao_marcada:
        nome_titulo += f"  —  VERSÃO {versao_marcada}"
    c.drawCentredString(PAGE_W / 2, layout["titulo_y"] * PAGE_H, nome_titulo)
    c.setFont("Helvetica", 8)
    c.setFillColorRGB(0.45, 0.45, 0.45)
    c.drawCentredString(PAGE_W / 2, layout["titulo_y"] * PAGE_H - 4.5 * mm, f"Localizador {loc}")
    c.setFillColorRGB(0, 0, 0)

    # código de barras lateral (identificação do gabarito)
    if "barcode" in layout:
        for cell in layout["barcode"]["cells"]:
            x = cell["x"] * PAGE_W
            y = cell["y"] * PAGE_H
            w = cell["w"] * PAGE_W
            h = cell["h"] * PAGE_H
            if cell["v"]:
                c.setFillColorRGB(0, 0, 0)
                c.rect(x, y, w, h, fill=1, stroke=0)
            else:
                c.setStrokeColorRGB(0.82, 0.82, 0.82)
                c.setLineWidth(0.5)
                c.rect(x, y, w, h, fill=0, stroke=1)

    # texto vertical na lateral direita (estilo ZipGrade)
    c.saveState()
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica", 9)
    c.translate(PAGE_W - MARGIN - 2 * mm, PAGE_H / 2)
    c.rotate(90)
    c.drawCentredString(0, 0, f"{config['nome'][:40]}  ({loc})")
    c.restoreState()

    tom = (cinza, cinza, cinza)
    # todas as bolinhas da folha usam o mesmo raio (o que a leitura espera)
    r_bolha = layout.get("bubble_radius_pt", BUBBLE_R)

    # caixas de cabeçalho — rótulo grande, como no ZipGrade
    c.setLineWidth(1.4)
    for cx in layout.get("caixas", []):
        x, y = cx["x"] * PAGE_W, cx["y"] * PAGE_H
        w, h = cx["w"] * PAGE_W, cx["h"] * PAGE_H
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica", 15)
        c.drawString(x, y + h + 1.8 * mm, cx["rotulo"])
        c.setStrokeColorRGB(0.15, 0.15, 0.15)
        c.rect(x, y, w, h, fill=0, stroke=1)

    # matrícula
    if "id_digitos" in layout:
        c.setFillColorRGB(0.15, 0.15, 0.15)
        c.setFont("Helvetica-Bold", 8.5)
        top = layout["id_top_y"] * PAGE_H
        c.drawString(CONTENT_X, top + 1 * mm, layout.get("id_rotulo", "Matrícula").upper())
        c.setFont("Helvetica", 6)
        for d, nums in layout["id_digitos"].items():
            for n, pos in nums.items():
                cxp, cyp = pos["x"] * PAGE_W, pos["y"] * PAGE_H
                c.setStrokeColorRGB(0.55, 0.55, 0.55)
                c.circle(cxp, cyp, DIGIT_R, fill=0, stroke=1)
                c.setFillColorRGB(0.55, 0.55, 0.55)
                c.drawCentredString(cxp, cyp - 1.8, n)
                c.setFillColorRGB(0, 0, 0)

    # versão
    if "versao_bolhas" in layout:
        c.setFillColorRGB(0.15, 0.15, 0.15)
        c.setFont("Helvetica-Bold", 8.5)
        vy = layout["versao_y"] * PAGE_H
        primeiro_x = min(p["x"] for p in layout["versao_bolhas"].values()) * PAGE_W
        rotulo_ver = layout.get("versao_rotulo", "Versão").upper()
        if versao_marcada:
            rotulo_ver += f"  ({versao_marcada})"
        c.drawString(primeiro_x - 2 * mm, vy + 5 * mm, rotulo_ver)
        c.setFillColorRGB(0, 0, 0)
        for L, pos in layout["versao_bolhas"].items():
            cxp, cyp = pos["x"] * PAGE_W, pos["y"] * PAGE_H
            if versao_marcada and L == versao_marcada:
                # bolinha já preenchida: o aluno não precisa marcar nada
                c.setFillColorRGB(0, 0, 0)
                c.setStrokeColorRGB(0, 0, 0)
                c.circle(cxp, cyp, r_bolha, fill=1, stroke=1)
            else:
                c.setStrokeColorRGB(0.55, 0.55, 0.55)
                c.circle(cxp, cyp, r_bolha, fill=0, stroke=1)
                c.setFillColorRGB(0.55, 0.55, 0.55)
                c.setFont("Helvetica", 8)
                c.drawCentredString(cxp, cyp - 2.6, L)
            c.setFillColorRGB(0, 0, 0)

    # questões — tamanho de fonte acompanha o raio da faixa de densidade
    header_y = layout["questoes_header_y"] * PAGE_H
    r_q = r_bolha
    # fontes acompanham o tamanho da bolinha, sem exageros nos extremos
    f_num = max(8.0, min(15.0, r_q / mm * 3.4))
    f_letra = max(5.5, min(9.5, r_q / mm * 2.1))
    dy_letra = f_letra * 0.34
    c.setStrokeColorRGB(*tom)
    for num, alts in layout["questoes"].items():
        primeiro = min(alts.values(), key=lambda p: p["x"])
        qx = primeiro["x"] * PAGE_W - r_q - 2.0 * mm
        qy = primeiro["y"] * PAGE_H
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica", f_num)
        c.drawRightString(qx, qy - 2.0 * mm, f"{int(num)}")
        for L, pos in alts.items():
            cxp, cyp = pos["x"] * PAGE_W, pos["y"] * PAGE_H
            c.circle(cxp, cyp, r_q, fill=0, stroke=1)
            # letra num cinza médio: visível para o aluno, mas some na leitura
            c.setFillColorRGB(0.55, 0.55, 0.55)
            c.setFont("Helvetica", f_letra)
            c.drawCentredString(cxp, cyp - dy_letra, L)
            c.setFillColorRGB(0, 0, 0)


def gerar_pdf_folha(config, layout, folha_id, copias=1, escuro=0, versao_marcada=None,
                    so_folha=False):
    """copias: 1 ou 2 por página. escuro: 0 (padrão) a 3 (mais escuro).
    so_folha só vale para a folha compacta (ver gerar_pdf_folha_compacta)."""
    tons = [0.62, 0.45, 0.28, 0.0]
    cinza = tons[max(0, min(3, escuro))]
    if layout.get("formato") == "compacta":
        return gerar_pdf_folha_compacta(config, layout, folha_id, copias=copias,
                                        cinza=cinza, versao_marcada=versao_marcada,
                                        so_folha=so_folha)
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    if copias == 1:
        desenhar_folha(c, config, layout, folha_id, cinza, versao_marcada)
    else:
        escala = (PAGE_H / 2) / PAGE_H  # 0.5
        for i in range(2):
            c.saveState()
            offset_y = PAGE_H / 2 if i == 0 else 0
            offset_x = (PAGE_W - PAGE_W * escala) / 2
            c.translate(offset_x, offset_y)
            c.scale(escala, escala)
            desenhar_folha(c, config, layout, folha_id, cinza, versao_marcada)
            c.restoreState()
        # linha de corte
        c.setDash(3, 3)
        c.setStrokeColorRGB(0.6, 0.6, 0.6)
        c.line(0, PAGE_H / 2, PAGE_W, PAGE_H / 2)
    c.save()
    return buf.getvalue()


# ══════════════════════════════════════════════
# FOLHA COMPACTA (para recortar e colar na prova)
# ══════════════════════════════════════════════
#
# A folha padrão ocupa a A4 inteira e AUMENTA a bolinha até o limite. Aqui é o
# contrário: a bolinha tem tamanho FIXO e a folha tem o tamanho do conteúdo —
# 20 questões cabem em cerca de 10 x 11 cm.
#
# O truque que dispensa mexer na leitura: a folha compacta é tratada como uma
# "página" própria, do tamanho dela. Todas as posições continuam sendo frações
# (0 a 1) dessa página, e é só isso que o app e o servidor usam para ler. Então
# a câmera, os alvos na tela e a leitura das bolinhas funcionam sem alteração —
# e a folha pode até ser redimensionada no Word que continua lendo, porque tudo
# é medido em relação aos 4 marcadores, não em milímetros.

C_DIAM = 5.0 * mm            # diâmetro fixo da bolinha (ZipGrade usa 4,2)
C_KH = 1.25                  # distância entre bolinhas ÷ diâmetro
C_KV = 1.50                  # distância entre linhas ÷ diâmetro
C_BORDA = 3.0 * mm           # faixa em branco entre o corte e os marcadores
C_FOLGA_COL = 3.5 * mm       # respiro entre colunas de questões
C_CAIXA_H = 8.0 * mm         # altura das caixas (Nome, Turma...)
C_ROTULO_H = 3.8 * mm        # espaço do rótulo acima da caixa
C_LARGURAS = {"grande": 50 * mm, "media": 36 * mm, "pequena": 22 * mm}
C_DIGIT_R = 2.2 * mm         # bolinhas da matrícula
C_DIGIT_VSP = 5.4 * mm
C_DIGIT_CSP = 5.6 * mm
C_BC_N = 14                  # 12 bits do número da folha + 2 de paridade
C_BC_W = 5.0 * mm            # largura de cada barra
C_BC_X = 1.5 * mm            # distância da barra até a borda esquerda da folha
C_BC_H = 2.6 * mm            # altura mínima de cada barra
# Altura máxima: a barra tem que ser bem mais LARGA do que alta. Uma barra
# quase quadrada passa pelos filtros de "marcador de canto" e, numa foto torta
# em que um canto de verdade falha, o leitor a escolhia como canto — a folha
# era endireitada errado e dezenas de questões saíam trocadas (apareceu nos
# testes com a folha de 50 questões, quando a barra tinha 3,6 x 3,4 mm).
C_BC_H_MAX = 3.0 * mm
C_BC_VAO = 0.62              # vão entre barras ÷ altura da barra (igual à folha padrão)
# Maior folha que ainda cabe numa A4 respeitando a margem que a impressora
# não imprime (uns 5 mm de cada lado).
C_MAX_W = 200 * mm
C_MAX_H = 287 * mm
C_PROPORCAO_ALVO = 0.90      # largura ÷ altura "ideal"; de 0,55 a 1,5 não há penalidade


def _compacta_medidas(nq, n_col, max_labels, caixas, id_cfg, ver_cfg):
    """Calcula as dimensões da folha compacta para um certo número de colunas.
    Devolve um dicionário com tudo em pontos, ou None se não couber na A4."""
    r = C_DIAM / 2
    sp = C_DIAM * C_KH
    rh = C_DIAM * C_KV
    por_col = math.ceil(nq / n_col)
    # "100" precisa de mais espaço que "99"
    num_w = (8.0 if nq >= 100 else 6.0) * mm
    col_w = num_w + (max_labels - 1) * sp + C_DIAM
    larg_questoes = n_col * col_w + (n_col - 1) * C_FOLGA_COL
    alt_questoes = (por_col - 1) * rh + C_DIAM

    # faixa lateral à direita das questões: versão em cima, matrícula embaixo
    faixa_w = 0.0
    faixa_h = 0.0
    letras = str(ver_cfg.get("letras", "AB"))[:6] if ver_cfg.get("ativo") else ""
    digitos = int(id_cfg.get("digitos", 4)) if id_cfg.get("ativo") else 0
    alt_versao = (3.5 * mm + C_DIAM + 3.0 * mm) if letras else 0.0
    # rótulo (3,5) + folga dentro da moldura (1) + bolinhas + folga (1)
    alt_id = (3.5 * mm + 1.0 * mm + 9 * C_DIGIT_VSP + 2 * C_DIGIT_R + 1.5 * mm) if digitos else 0.0
    if letras:
        faixa_w = max(faixa_w, (len(letras) - 1) * sp + C_DIAM)
    if digitos:
        faixa_w = max(faixa_w, (digitos - 1) * C_DIGIT_CSP + 2 * C_DIGIT_R + 2.0 * mm)
    faixa_h = alt_versao + alt_id
    folga_faixa = 5.0 * mm if faixa_w else 0.0

    larg_conteudo = larg_questoes + folga_faixa + faixa_w
    if caixas:
        # largura mínima para o cabeçalho não virar uma pilha de caixas estreitas
        larg_conteudo = max(larg_conteudo, 60 * mm)

    # marcadores um pouco maiores nas folhas grandes: de longe, um quadrado de
    # 6 mm numa folha de 19 cm ficaria pequeno demais para a câmera achar
    marker = max(6.0 * mm, min(8.0 * mm, larg_conteudo * 0.05))
    # A leitura do código de barras procura a barra numa faixa que vai até 1,4
    # largura à direita dela. Nada pode entrar nessa faixa (número de questão,
    # bolinha marcada), senão vira "barra" falsa. Por isso o conteúdo só começa
    # depois dela.
    x_conteudo = max(C_BC_X + C_BC_W + 1.4 * C_BC_W + 0.6 * mm,
                     C_BORDA + marker + 3.0 * mm)
    # 3 mm de respiro até a coluna dos marcadores: uma bolinha pintada colada
    # no quadrado preto poderia "grudar" nele numa foto borrada
    margem_dir = C_BORDA + marker + 3.0 * mm
    largura = x_conteudo + larg_conteudo + margem_dir

    # cabeçalho: distribui as caixas em linhas
    # Se encolhendo um pouco a caixa grande (o Nome) tudo couber numa linha só,
    # melhor: economiza 1,3 cm de altura e evita uma caixa sozinha na 2ª linha.
    larguras = dict(C_LARGURAS)
    minimo = sum((40 * mm if cx.get("largura") == "grande"
                  else C_LARGURAS.get(cx.get("largura", "media"), 36 * mm)) for cx in caixas)
    if caixas and minimo + 2.0 * mm * (len(caixas) - 1) <= larg_conteudo + 0.5:
        larguras["grande"] = 40 * mm
    linhas_cab = []
    atual, usado = [], 0.0
    for cx in caixas:
        w = min(larguras.get(cx.get("largura", "media"), 36 * mm), larg_conteudo)
        extra = (2.0 * mm if atual else 0.0) + w
        if atual and usado + extra > larg_conteudo + 0.5:
            linhas_cab.append(atual)
            atual, usado, extra = [], 0.0, w
        atual.append({"rotulo": cx["rotulo"], "w": w, "grande": cx.get("largura") == "grande"})
        usado += extra
    if atual:
        linhas_cab.append(atual)
    # a caixa "grande" (o Nome) estica até ocupar o resto da linha dela
    for linha in linhas_cab:
        sobra = larg_conteudo - sum(b["w"] for b in linha) - 2.0 * mm * (len(linha) - 1)
        grandes = [b for b in linha if b["grande"]]
        if sobra > 0 and grandes:
            grandes[0]["w"] += sobra
    alt_linha_cab = C_ROTULO_H + C_CAIXA_H + 1.5 * mm
    alt_cab = len(linhas_cab) * alt_linha_cab + (2.5 * mm if linhas_cab else 0.0)

    alt_corpo = max(alt_questoes, faixa_h)
    altura = C_BORDA + alt_cab + alt_corpo + C_BORDA + 1.0 * mm
    # o código de barras fica entre os dois marcadores da esquerda: a folha
    # precisa ter altura para ele, mesmo com poucas questões
    bc_min = C_BC_N * C_BC_H + (C_BC_N - 1) * C_BC_H * C_BC_VAO
    altura = max(altura, 2 * C_BORDA + 2 * marker + 2 * 5.0 * mm + bc_min)

    if largura > C_MAX_W or altura > C_MAX_H:
        return None
    return {
        "r": r, "sp": sp, "rh": rh, "por_col": por_col, "num_w": num_w, "col_w": col_w,
        "larg_questoes": larg_questoes, "larg_conteudo": larg_conteudo,
        "faixa_w": faixa_w, "folga_faixa": folga_faixa,
        "alt_versao": alt_versao, "letras": letras, "digitos": digitos,
        "marker": marker, "x_conteudo": x_conteudo,
        "largura": largura, "altura": altura,
        "linhas_cab": linhas_cab, "alt_linha_cab": alt_linha_cab, "alt_cab": alt_cab,
    }


def compute_layout_compacto(config, folha_id):
    """Layout da folha compacta. Mesmo formato de saída do compute_layout, mas a
    "página" é a própria folha (page_width_pt/page_height_pt = tamanho dela)."""
    questoes = config.get("questoes", [])
    if not questoes:
        return None, "Adicione pelo menos uma questão."
    nq = len(questoes)
    max_labels = max(len(q["labels"]) for q in questoes)
    caixas = [c for c in config.get("cabecalho", []) if c.get("ativo")]
    id_cfg = config.get("id_aluno", {}) or {}
    ver_cfg = config.get("versao", {}) or {}

    # Testa de 1 a 5 colunas e fica com a que gasta MENOS PAPEL — é o que
    # importa para recortar e colar. Só penaliza formato exagerado (tira muito
    # comprida ou muito larga), que é ruim de enquadrar na câmera.
    # Empate: menos colunas.
    melhor = None
    for n_col in (1, 2, 3, 4, 5):
        if n_col > nq:
            break
        m = _compacta_medidas(nq, n_col, max_labels, caixas, id_cfg, ver_cfg)
        if m is None:
            continue
        desvio = abs(math.log((m["largura"] / m["altura"]) / C_PROPORCAO_ALVO))
        nota = m["largura"] * m["altura"] * (1 + 2 * max(0.0, desvio - 0.5))
        if melhor is None or nota < melhor[0] * 0.995:
            melhor = (nota, n_col, m)
    if melhor is None:
        return None, ("Muitas questões para a folha compacta. Reduza a quantidade "
                      "ou use o formato padrão (página inteira).")
    _, n_col, m = melhor
    W, H = m["largura"], m["altura"]
    marker = m["marker"]

    def f(x, t):
        """x a partir da esquerda e t a partir do TOPO (em pontos) → frações,
        com y crescendo para cima, como no restante do app."""
        return {"x": x / W, "y": (H - t) / H}

    layout = {
        "tipo": "folha",
        "formato": "compacta",
        "folha_id": folha_id,
        "page_width_pt": W,
        "page_height_pt": H,
        "marker_size_pt": marker,
        "bubble_radius_pt": m["r"],
        "digit_radius_pt": C_DIGIT_R,
        "questoes": {},
        "labels_questoes": {},
        "n_colunas": n_col,
        "por_coluna": m["por_col"],
    }

    # marcadores: MESMA ordem da folha padrão (sup-esq, sup-dir, inf-esq,
    # inf-dir) — a correção de perspectiva conta com ela. Guardamos o canto
    # inferior-esquerdo de cada quadrado, também como na folha padrão.
    xe, xd = C_BORDA, W - C_BORDA - marker
    yb, yt = C_BORDA, H - C_BORDA - marker
    layout["markers"] = [{"x": x / W, "y": y / H}
                         for x, y in ((xe, yt), (xd, yt), (xe, yb), (xd, yb))]

    # ─── código de barras lateral, entre os dois marcadores da esquerda ───
    espaco = H - 2 * C_BORDA - 2 * marker - 2 * 5.0 * mm
    bc_h = espaco * 0.62 / (C_BC_N + (C_BC_N - 1) * C_BC_VAO)
    bc_h = max(C_BC_H, min(C_BC_H_MAX, bc_h))
    bc_vao = bc_h * C_BC_VAO
    bc_total = C_BC_N * bc_h + (C_BC_N - 1) * bc_vao
    bc_x = C_BC_X
    bc_topo = H / 2 + bc_total / 2          # em y "para cima"
    cod = codigo_barras(folha_id)
    bits = [(cod >> (11 - i)) & 1 for i in range(12)]
    valores = bits + [sum(bits[0:6]) % 2, sum(bits[6:12]) % 2]
    layout["barcode"] = {"bits": C_BC_N, "cells": [{
        "x": bc_x / W, "y": (bc_topo - i * (bc_h + bc_vao) - bc_h) / H,
        "w": C_BC_W / W, "h": bc_h / H, "v": valores[i],
    } for i in range(C_BC_N)]}

    # ─── caixas de cabeçalho ───
    x0 = m["x_conteudo"]
    layout["caixas"] = []
    t = C_BORDA
    for linha in m["linhas_cab"]:
        cx = x0
        for b in linha:
            topo_caixa = t + C_ROTULO_H
            layout["caixas"].append({
                "rotulo": b["rotulo"],
                "x": cx / W, "y": (H - topo_caixa - C_CAIXA_H) / H,
                "w": b["w"] / W, "h": C_CAIXA_H / H,
            })
            cx += b["w"] + 2.0 * mm
        t += m["alt_linha_cab"]
    t_corpo = C_BORDA + m["alt_cab"] + (0.5 * mm if not m["linhas_cab"] else 0.0)
    layout["questoes_header_y"] = (H - t_corpo) / H

    # ─── questões ───
    for col in range(n_col):
        x_base = x0 + col * (m["col_w"] + C_FOLGA_COL)
        for row in range(m["por_col"]):
            idx = col * m["por_col"] + row
            if idx >= nq:
                break
            q = questoes[idx]
            num = str(q["numero"])
            layout["questoes"][num] = {}
            layout["labels_questoes"][num] = q["labels"]
            tq = t_corpo + m["r"] + row * m["rh"]
            for i, L in enumerate(q["labels"]):
                layout["questoes"][num][L] = f(x_base + m["num_w"] + m["r"] + i * m["sp"], tq)

    # ─── faixa lateral: versão e matrícula ───
    x_faixa = x0 + m["larg_questoes"] + m["folga_faixa"]
    t_faixa = t_corpo
    if m["letras"]:
        ty = t_faixa + 3.5 * mm + m["r"]
        layout["versao_bolhas"] = {
            L: f(x_faixa + m["r"] + i * m["sp"], ty) for i, L in enumerate(m["letras"])}
        layout["versao_rotulo"] = ver_cfg.get("rotulo", "Versão")
        layout["versao_y"] = (H - ty) / H
        t_faixa += m["alt_versao"]
    if m["digitos"]:
        layout["id_digitos"] = {}
        topo = t_faixa + 3.5 * mm          # onde começa a moldura
        for d in range(m["digitos"]):
            cx = x_faixa + 1.0 * mm + C_DIGIT_R + d * C_DIGIT_CSP
            layout["id_digitos"][str(d)] = {
                str(n): f(cx, topo + 1.0 * mm + C_DIGIT_R + n * C_DIGIT_VSP) for n in range(10)}
        layout["id_rotulo"] = id_cfg.get("rotulo", "Matrícula")
        layout["id_top_y"] = (H - topo) / H
        layout["id_x"] = x_faixa / W
    return layout, None


def desenhar_folha_compacta(c, config, layout, folha_id, cinza=0.62, versao_marcada=None):
    """Desenha UMA folha compacta com a origem no canto inferior-esquerdo dela.
    Quem chama posiciona a folha na A4 com c.translate()."""
    W, H = layout["page_width_pt"], layout["page_height_pt"]
    marker = layout["marker_size_pt"]
    r = layout["bubble_radius_pt"]
    loc = f"{folha_id:04d}"

    # marcadores sempre pretos
    c.setFillColorRGB(0, 0, 0)
    for mk in layout["markers"]:
        c.rect(mk["x"] * W, mk["y"] * H, marker, marker, fill=1, stroke=0)

    # código de barras lateral: barra "0" é só um contorno clarinho
    for cell in layout["barcode"]["cells"]:
        x, y, w, h = cell["x"] * W, cell["y"] * H, cell["w"] * W, cell["h"] * H
        if cell["v"]:
            c.setFillColorRGB(0, 0, 0)
            c.rect(x, y, w, h, fill=1, stroke=0)
        else:
            c.setStrokeColorRGB(0.82, 0.82, 0.82)
            c.setLineWidth(0.4)
            c.rect(x, y, w, h, fill=0, stroke=1)

    # nome do gabarito + localizador, na vertical, entre os marcadores da direita
    nome = config["nome"]
    sufixo = f" — versão {versao_marcada}  ({loc})" if versao_marcada else f"  ({loc})"
    espaco = H - 2 * C_BORDA - 2 * marker - 8 * mm
    tam = 7.5
    # nome comprido demais para o espaço: encurta com reticências (o localizador
    # nunca é cortado — é ele que identifica a folha)
    while len(nome) > 3 and c.stringWidth(nome + sufixo, "Helvetica", tam) > espaco:
        nome = nome[:-2].rstrip() + "…"
    texto = nome + sufixo
    c.saveState()
    c.setFillColorRGB(0.25, 0.25, 0.25)
    c.setFont("Helvetica", tam)
    c.translate(W - C_BORDA - marker / 2 + tam * 0.35, H / 2)
    c.rotate(90)
    c.drawCentredString(0, 0, texto)
    c.restoreState()

    # Traço fino: é ele (junto com o espaçamento folgado) que deixa a folha
    # "leve". Como o traço é fino, não deixo ficar mais claro que 0,55 — senão
    # some em impressora fraca ou em xerox de xerox.
    tom = min(cinza, 0.55)
    tom_letra = min(cinza, 0.55)

    # caixas de cabeçalho: rótulo pequeno em cima, caixa de 8 mm
    for cx in layout.get("caixas", []):
        x, y, w, h = cx["x"] * W, cx["y"] * H, cx["w"] * W, cx["h"] * H
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica", 9)
        c.drawString(x, y + h + 1.0 * mm, cx["rotulo"])
        c.setStrokeColorRGB(0.1, 0.1, 0.1)
        c.setLineWidth(0.9)
        c.rect(x, y, w, h, fill=0, stroke=1)

    # versão
    if "versao_bolhas" in layout:
        vy = layout["versao_y"] * H
        primeiro_x = min(p["x"] for p in layout["versao_bolhas"].values()) * W
        rot = layout.get("versao_rotulo", "Versão")
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica", 7.5)
        c.drawString(primeiro_x - r, vy + r + 1.2 * mm, rot)
        for L, pos in layout["versao_bolhas"].items():
            cxp, cyp = pos["x"] * W, pos["y"] * H
            if versao_marcada and L == versao_marcada:
                # já vem preenchida: o aluno não precisa marcar
                c.setFillColorRGB(0, 0, 0)
                c.setStrokeColorRGB(0, 0, 0)
                c.circle(cxp, cyp, r, fill=1, stroke=1)
            else:
                c.setLineWidth(0.7)
                c.setStrokeColorRGB(tom, tom, tom)
                c.circle(cxp, cyp, r, fill=0, stroke=1)
                c.setFillColorRGB(tom_letra, tom_letra, tom_letra)
                c.setFont("Helvetica", 6.5)
                c.drawCentredString(cxp, cyp - 2.3, L)

    # matrícula: moldura clarinha em volta, para o bloco não se confundir com
    # as questões ao lado
    if "id_digitos" in layout:
        topo = layout["id_top_y"] * H
        xs = [p["x"] * W for nums in layout["id_digitos"].values() for p in nums.values()]
        ys = [p["y"] * H for nums in layout["id_digitos"].values() for p in nums.values()]
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica", 7.5)
        c.drawString(min(xs) - C_DIGIT_R - 1.0 * mm, topo + 1.0 * mm,
                     layout.get("id_rotulo", "Matrícula"))
        c.setStrokeColorRGB(0.75, 0.75, 0.75)
        c.setLineWidth(0.4)
        c.rect(min(xs) - C_DIGIT_R - 1.0 * mm, min(ys) - C_DIGIT_R - 1.0 * mm,
               max(xs) - min(xs) + 2 * C_DIGIT_R + 2.0 * mm,
               max(ys) - min(ys) + 2 * C_DIGIT_R + 2.0 * mm, fill=0, stroke=1)
        c.setLineWidth(0.7)
        c.setFont("Helvetica", 5.5)
        for nums in layout["id_digitos"].values():
            for n, pos in nums.items():
                cxp, cyp = pos["x"] * W, pos["y"] * H
                c.setStrokeColorRGB(tom, tom, tom)
                c.circle(cxp, cyp, C_DIGIT_R, fill=0, stroke=1)
                c.setFillColorRGB(tom_letra, tom_letra, tom_letra)
                c.drawCentredString(cxp, cyp - 1.9, n)

    # questões
    for num, alts in layout["questoes"].items():
        primeiro = min(alts.values(), key=lambda p: p["x"])
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica", 10)
        c.drawRightString(primeiro["x"] * W - r - 1.4 * mm, primeiro["y"] * H - 3.5, f"{int(num)}")
        c.setLineWidth(0.7)
        for L, pos in alts.items():
            cxp, cyp = pos["x"] * W, pos["y"] * H
            c.setStrokeColorRGB(tom, tom, tom)
            c.circle(cxp, cyp, r, fill=0, stroke=1)
            # letra em cinza: o aluno enxerga, mas quase não pesa na leitura
            c.setFillColorRGB(tom_letra, tom_letra, tom_letra)
            c.setFont("Helvetica", 6.5)
            c.drawCentredString(cxp, cyp - 2.3, L)
    c.setFillColorRGB(0, 0, 0)


def gerar_pdf_folha_compacta(config, layout, folha_id, copias=1, cinza=0.62,
                             versao_marcada=None, so_folha=False):
    """PDF da folha compacta, em tamanho real.

    copias=1 → uma folha no alto da A4.
    copias=2 → quantas couberem na A4 (20 questões sem matrícula: 4 por página),
               com linha pontilhada de corte.
    so_folha=True → a "página" do PDF é só a folha, sem A4 em volta. É o que
               vira a imagem (PNG) para colar direto no arquivo da prova."""
    W, H = layout["page_width_pt"], layout["page_height_pt"]
    buf = io.BytesIO()
    if so_folha:
        c = rl_canvas.Canvas(buf, pagesize=(W, H))
        desenhar_folha_compacta(c, config, layout, folha_id, cinza, versao_marcada)
        c.save()
        return buf.getvalue()

    c = rl_canvas.Canvas(buf, pagesize=A4)
    # A folha já tem 3 mm em branco em volta dos marcadores; com mais 2 mm de
    # cada lado da A4 ficamos fora da área que a impressora não alcança.
    util_w, util_h = PAGE_W - 4 * mm, PAGE_H - 4 * mm
    if copias == 1:
        nx = ny = 1
    else:
        nx = max(1, int(util_w // W))
        ny = max(1, int(util_h // H))
    x_ini = (PAGE_W - nx * W) / 2
    # uma só: no alto da página; várias: centralizadas
    y_topo = PAGE_H - (10 * mm if (nx * ny == 1 and H < PAGE_H - 20 * mm) else (PAGE_H - ny * H) / 2)
    for j in range(ny):
        for i in range(nx):
            ox = x_ini + i * W
            oy = y_topo - (j + 1) * H
            c.saveState()
            c.translate(ox, oy)
            desenhar_folha_compacta(c, config, layout, folha_id, cinza, versao_marcada)
            c.restoreState()
            # linha de corte
            c.saveState()
            c.setDash(2, 3)
            c.setLineWidth(0.4)
            c.setStrokeColorRGB(0.7, 0.7, 0.7)
            c.rect(ox, oy, W, H, fill=0, stroke=1)
            c.restoreState()
    c.save()
    return buf.getvalue()


# ══════════════════════════════════════════════
# ALGORITMO OMR
# ══════════════════════════════════════════════

LIMIAR_MARCACAO = 0.35


def _candidatos_marcadores(cinza):
    """Procura quadrados pretos usando VÁRIAS binarizações.

    Um limiar fixo quebra quando a sombra do celular cobre parte da folha: o
    papel sombreado cai abaixo do limiar e vira uma mancha preta única, que
    engole os marcadores. O limiar adaptativo compara cada ponto com a
    vizinhança, então funciona mesmo com um lado na sombra e o outro na luz.
    Juntamos os achados de cada método e removemos repetidos."""
    h_img, w_img = cinza.shape
    area_min = (w_img * 0.015) * (h_img * 0.015) * 0.3
    suave = cv2.GaussianBlur(cinza, (5, 5), 0)

    binarias = []
    # bloco grande o bastante para conter o marcador e o papel ao redor
    bloco = max(31, (int(min(w_img, h_img) * 0.08) // 2) * 2 + 1)
    binarias.append(cv2.adaptiveThreshold(suave, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                          cv2.THRESH_BINARY_INV, bloco, 12))
    binarias.append(cv2.threshold(suave, 0, 255,
                                  cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1])
    binarias.append(cv2.threshold(suave, 100, 255, cv2.THRESH_BINARY_INV)[1])

    achados = []
    for binaria in binarias:
        # RETR_LIST (todos os contornos) e não RETR_EXTERNAL (só os de fora):
        # quando a folha aparece INTEIRA sobre um fundo escuro (mesa escura,
        # folha compacta recortada), o fundo forma um "anel" escuro em volta
        # dela. Pedindo só os contornos de fora, vinha apenas esse anel e os
        # marcadores, que ficam dentro dele, nem eram examinados. Os contornos
        # a mais que aparecem (miolo de bolinha, interior das caixas) são
        # barrados pelos filtros abaixo: tamanho, formato quadrado,
        # preenchimento e miolo escuro.
        contornos, _ = cv2.findContours(binaria, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in contornos:
            x, y, w, h = cv2.boundingRect(c)
            # Triagem rápida pela caixa "em pé" (barata). Os limites aqui são
            # folgados de propósito: quem decide é a medida girada, logo abaixo.
            if w * h <= area_min or not (0.6 < w / float(h) < 1.67):
                continue
            if w * h > w_img * h_img * 0.10:      # mancha grande demais
                continue
            # Mede o quadrado NA INCLINAÇÃO DELE (menor retângulo que o envolve,
            # girado), e não numa caixa "em pé". Com a caixa em pé, um marcador
            # inclinado só 7° já parecia "pouco preenchido" (os cantos da caixa
            # sobram em branco) e era descartado — a folha um pouco torta era
            # recusada. Medido assim, o quadrado continua quadrado em qualquer
            # ângulo, e a barra do código lateral continua comprida (não passa
            # por canto, mesmo girada).
            (mx, my), (rw, rh), _ang = cv2.minAreaRect(c)
            if rw < 1 or rh < 1:
                continue
            area_ret = rw * rh
            if area_ret <= area_min or area_ret > w_img * h_img * 0.05:
                continue
            if not (0.7 < rw / rh < 1.3):
                continue
            # Bolinha de resposta marcada é REDONDA, marcador de canto é
            # QUADRADO. Com bolinhas grandes (8,2mm), uma prova com muitas
            # respostas marcadas pode ter bolinhas com área igual ou maior
            # que a dos marcadores reais — sem este filtro elas empurram os
            # marcadores de verdade para fora do grupo dos "12 maiores" e a
            # leitura falha ("não separei os 4 cantos"). Preenchimento = área
            # do contorno / área do retângulo: quadrado cheio fica perto de
            # 0.85+, círculo inscrito no mesmo retângulo fica perto de 0.78
            # (π/4), em qualquer ângulo. O limite era 0.80 quando a medida
            # usava a caixa em pé; a medida girada "abraça" melhor a forma e
            # dá valores uns 0,04 maiores para TUDO (cantos e bolinhas), então
            # o limite subiu junto para 0.84. Sem isso, bolinhas pintadas
            # passavam a contar como candidatas a canto e, numa folha com um
            # canto fora da foto, uma delas era escolhida no lugar dele.
            # Nos testes: cantos de 0,83 a 1,00 (99% acima de 0,86); bolinhas
            # pintadas em torno de 0,79.
            preenchimento = cv2.contourArea(c) / area_ret
            if preenchimento < 0.84:
                continue
            # Quadrado encostado na borda da foto pode estar cortado: aí o
            # centro medido não é o centro do marcador e a folha sairia torta.
            # Só aceita se ainda estiver praticamente quadrado (corte de no
            # máximo ~15% do lado, que desloca o centro menos de 1 mm).
            if (x <= 1 or y <= 1 or x + w >= w_img - 1 or y + h >= h_img - 1) \
                    and not (0.85 < rw / rh < 1.18):
                continue
            # o miolo precisa ser mesmo escuro em relação ao próprio entorno
            if not _parece_marcador(cinza, x, y, w, h):
                continue
            # Guarda o centro e o tamanho VERDADEIROS do quadrado (os da medida
            # girada). A caixa em pé de um quadrado inclinado é maior que ele,
            # o que distorceria a comparação de tamanhos entre os 4 cantos.
            achados.append((mx - rw / 2.0, my - rh / 2.0, rw, rh))

    # remove repetidos (o mesmo marcador aparece em mais de uma binarização)
    unicos = []
    for cand in achados:
        cx, cy = cand[0] + cand[2] / 2, cand[1] + cand[3] / 2
        dup = False
        for u in unicos:
            ux, uy = u[0] + u[2] / 2, u[1] + u[3] / 2
            if math.hypot(cx - ux, cy - uy) < max(cand[2], cand[3]) * 0.6:
                dup = True
                break
        if not dup:
            unicos.append(cand)
    return unicos


def _parece_marcador(cinza, x, y, w, h):
    """Confere se o retângulo é bem mais escuro que o papel logo ao redor.
    Isso descarta sombras e manchas grandes, que escurecem tudo por igual."""
    hi, wi = cinza.shape
    miolo = cinza[y:y + h, x:x + w]
    if miolo.size == 0:
        return False
    m = int(max(w, h) * 0.8)
    x0, x1 = max(0, x - m), min(wi, x + w + m)
    y0, y1 = max(0, y - m), min(hi, y + h + m)
    volta = cinza[y0:y1, x0:x1]
    if volta.size <= miolo.size:
        return False
    soma_volta = float(volta.sum()) - float(miolo.sum())
    n_volta = volta.size - miolo.size
    if n_volta <= 0:
        return False
    brilho_volta = soma_volta / n_volta
    return (brilho_volta - float(miolo.mean())) > 35


def encontrar_marcadores(cinza, layout=None, varios=False):
    """Acha os 4 cantos. Normalmente devolve só a melhor combinação (lista de 4
    pontos). Com varios=True devolve as 5 melhores, cada uma com os pontos e o
    tamanho medido de cada quadrado — é o que localizar_folha usa para conferir
    a escolha e, se preciso, tentar a seguinte."""
    h_img, w_img = cinza.shape
    candidatos = _candidatos_marcadores(cinza)
    # proporção esperada entre altura e largura do retângulo dos marcadores
    razao_folha = 1.49
    if layout:
        try:
            mk = layout["markers"]
            xs = [m["x"] for m in mk]
            ys = [m["y"] for m in mk]
            lw = (max(xs) - min(xs)) * layout["page_width_pt"]
            lh = (max(ys) - min(ys)) * layout["page_height_pt"]
            if lw > 0 and lh > 0:
                razao_folha = lh / lw
        except Exception:
            pass
    if len(candidatos) < 4:
        raise ValueError(f"Só encontrei {len(candidatos)} marcadores de canto. Melhore a iluminação/enquadramento.")

    # Escolher o candidato mais próximo de cada canto da IMAGEM falha quando a
    # folha aparece cortada: texto ou sombra viram "marcador" e a perspectiva
    # sai distorcida. Testamos combinações e ficamos com a que melhor forma um
    # retângulo com marcadores de tamanho parecido.
    import itertools
    por_area = sorted(candidatos, key=lambda c: c[2] * c[3], reverse=True)[:16]

    def centro(r):
        return (r[0] + r[2] / 2.0, r[1] + r[3] / 2.0)

    combinacoes = []
    for grupo in itertools.combinations(por_area, 4):
        areas = [g[2] * g[3] for g in grupo]
        razao_area = max(areas) / max(1e-6, min(areas))
        if razao_area > 5:
            continue
        # Ordena por soma/diferença das coordenadas: aguenta a folha girada.
        # Separar por Y trocaria os cantos quando a foto sai inclinada.
        pts = [centro(g) for g in grupo]
        soma = [p[0] + p[1] for p in pts]
        dif = [p[0] - p[1] for p in pts]
        i_se = soma.index(min(soma))
        i_id = soma.index(max(soma))
        i_sd = dif.index(max(dif))
        i_ie = dif.index(min(dif))
        if len({i_se, i_sd, i_ie, i_id}) != 4:
            continue
        ordem = [pts[i_se], pts[i_sd], pts[i_ie], pts[i_id]]

        def dd(a, b):
            return math.hypot(a[0] - b[0], a[1] - b[1])

        w_top, w_bot = dd(ordem[0], ordem[1]), dd(ordem[2], ordem[3])
        h_esq, h_dir = dd(ordem[0], ordem[2]), dd(ordem[1], ordem[3])
        if min(w_top, w_bot, h_esq, h_dir) < 1:
            continue
        sim_w = min(w_top, w_bot) / max(w_top, w_bot)
        sim_h = min(h_esq, h_dir) / max(h_esq, h_dir)
        if sim_w < 0.70 or sim_h < 0.70:
            continue
        # diagonais parecidas: num retângulo real elas quase se igualam
        dg1, dg2 = dd(ordem[0], ordem[3]), dd(ordem[1], ordem[2])
        if min(dg1, dg2) / max(dg1, dg2) < 0.78:
            continue
        razao = ((h_esq + h_dir) / 2) / ((w_top + w_bot) / 2)
        if razao < razao_folha * 0.62 or razao > razao_folha * 1.60:
            continue
        area = ((w_top + w_bot) / 2) * ((h_esq + h_dir) / 2)
        if area < w_img * h_img * 0.07:
            continue
        nota = ((sim_w + sim_h) * 2
                - abs(razao - razao_folha) * 1.5
                - (razao_area - 1) * 0.3
                + (area / (w_img * h_img)) * 1.5)
        tamanhos = [math.sqrt(grupo[i][2] * grupo[i][3]) for i in (i_se, i_sd, i_ie, i_id)]
        combinacoes.append((nota, ordem, tamanhos))

    if not combinacoes:
        raise ValueError("Não consegui identificar os 4 cantos da folha. "
                         "Envie a folha INTEIRA, sem cortar as bordas.")
    combinacoes.sort(key=lambda t: t[0], reverse=True)
    if varios:
        return [{"pts": o, "tam": t} for _, o, t in combinacoes[:5]]
    return combinacoes[0][1]


def _tamanho_dos_cantos_ok(pts, tam, layout):
    """Os 4 quadrados escolhidos têm o tamanho que um marcador de canto DEVE ter
    para o tamanho em que a folha aparece na foto?

    Na folha, a relação entre o lado do marcador e a distância entre os cantos
    é conhecida (vem do layout). Se um dos "cantos" for na verdade uma bolinha
    pintada — o que acontecia quando um canto de verdade ficava fora da foto —
    ele é menor (ou maior) do que deveria, e a conta não fecha.
    Nos testes: com cantos de verdade a relação ficou entre 0,92 e 1,19 do
    esperado; com bolinha no lugar de canto, 0,52 / 0,65 / 1,33 / 1,38."""
    def dd(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])
    cima, baixo = dd(pts[0], pts[1]), dd(pts[2], pts[3])
    esq, dire = dd(pts[0], pts[2]), dd(pts[1], pts[3])
    lados = [(cima, esq), (cima, dire), (baixo, esq), (baixo, dire)]
    try:
        k = [tam[i] / math.sqrt(lados[i][0] * lados[i][1]) for i in range(4)]
        mk = layout["markers"]
        lw = abs(mk[1]["x"] - mk[0]["x"]) * layout["page_width_pt"]
        lh = abs(mk[0]["y"] - mk[2]["y"]) * layout["page_height_pt"]
        k_esp = layout["marker_size_pt"] / math.sqrt(lw * lh)
    except Exception:
        return True          # layout antigo sem essas medidas: não bloqueia
    rel = [x / k_esp for x in k]
    return min(rel) >= 0.80 and max(rel) <= 1.30 and max(k) / min(k) <= 1.38


# Nota mínima de "os círculos impressos estão onde deveriam" para aceitar uma
# folha cujo código de barras não pôde ser lido. Ver nota_alinhamento.
ALINHAMENTO_MIN = 0.045
_ANG_ANEL = [(math.cos(2 * math.pi * i / 16), math.sin(2 * math.pi * i / 16)) for i in range(16)]
_ANG_FORA = [(math.cos(math.pi / 4 + i * math.pi / 2), math.sin(math.pi / 4 + i * math.pi / 2))
             for i in range(4)]


def nota_alinhamento(corrigida, layout, W, H):
    """Confere se a folha foi endireitada certo, olhando o que está IMPRESSO:
    em cada bolinha do layout, o contorno do círculo tem que estar lá.

    Para cada bolinha, compara 16 pontos em cima do contorno com 4 pontos de
    papel logo fora dele (nas diagonais, onde não há bolinha vizinha). Com a
    folha no lugar, o contorno é mais escuro que o papel em TODAS as bolinhas
    e a média fica alta. Com os cantos errados, os pontos caem em lugares
    quaisquer e a média fica perto de zero.
    Nos testes: cantos certos de 0,09 (foto bem borrada) a 0,30; cantos
    errados no máximo 0,02 — por isso o corte em 0,045."""
    raio = layout["bubble_radius_pt"] / layout["page_width_pt"] * W
    h, w = corrigida.shape
    total, n = 0.0, 0
    for alts in layout["questoes"].values():
        for pos in alts.values():
            cx, cy = pos["x"] * W, (1 - pos["y"]) * H
            anel = 0.0
            for dx, dy in _ANG_ANEL:
                anel += corrigida[min(h - 1, max(0, int(round(cy + raio * dy)))),
                                  min(w - 1, max(0, int(round(cx + raio * dx))))]
            fora = 0.0
            for dx, dy in _ANG_FORA:
                fora += corrigida[min(h - 1, max(0, int(round(cy + 1.3 * raio * dy)))),
                                  min(w - 1, max(0, int(round(cx + 1.3 * raio * dx))))]
            anel /= 16.0
            fora /= 4.0
            total += (fora - anel) / max(fora, 40.0)
            n += 1
    return total / n if n else 0.0


def localizar_folha(cinza, layout, W, H, validos):
    """Acha os cantos, endireita a folha e CONFERE se a escolha faz sentido.
    Devolve (imagem endireitada, número lido no código de barras ou None).

    Por que conferir: quando um canto fica fora da foto, sobram 3 marcadores e
    o leitor completava o retângulo com uma bolinha pintada. A folha era
    endireitada torta e saíam dezenas de questões erradas — em vez de recusar.

    Para cada combinação de cantos (da melhor para a pior):
     1. Código de barras lateral lido e batendo com esta correção → cantos
        certos, aceita na hora (com a folha torta as barras não caem no lugar).
     2. Sem essa confirmação, aceita se os círculos impressos estiverem onde o
        layout diz (nota_alinhamento). Código de barras ilegível, sozinho,
        continua NÃO sendo motivo para recusar: em foto real ele falha às vezes.
    Se nenhuma combinação passar, a foto é recusada."""
    opcoes = encontrar_marcadores(cinza, layout, varios=True)
    tem_barra = bool(layout.get("barcode"))
    primeira = None
    for i, op in enumerate(opcoes):
        corrigida = corrigir_perspectiva(cinza, op["pts"], W, H, layout)
        id_barcode = ler_barcode(corrigida, layout, W, H) if tem_barra else None
        if id_barcode is not None and (not validos or id_barcode in validos):
            return corrigida, id_barcode
        if nota_alinhamento(corrigida, layout, W, H) >= ALINHAMENTO_MIN:
            # folha no lugar, mas sem código confirmado (ilegível, ou de outro
            # gabarito com o mesmo desenho — quem chama trata os dois casos)
            return corrigida, id_barcode
        if i == 0:
            primeira = (corrigida, id_barcode, op)
    # Nada passou. Se a melhor combinação tinha cantos de tamanho coerente e um
    # código de barras legível de OUTRO gabarito, o mais provável é o professor
    # ter pego a folha errada: devolvemos assim mesmo para ele receber o aviso
    # certo ("esta folha é do gabarito tal").
    corrigida, id_barcode, op = primeira
    if id_barcode is not None and _tamanho_dos_cantos_ok(op["pts"], op["tam"], layout):
        return corrigida, id_barcode
    raise ValueError("Não consegui confirmar os 4 cantos da folha. "
                     "Enquadre a folha INTEIRA, sem cortar as bordas.")


def corrigir_perspectiva(cinza, marcadores, W, H, layout):
    ms = layout["marker_size_pt"]
    pw, ph = layout["page_width_pt"], layout["page_height_pt"]
    origem = np.array(marcadores, dtype="float32")
    destino = np.array([
        [(m["x"] + ms / (2 * pw)) * W, (1 - m["y"] - ms / (2 * ph)) * H]
        for m in layout["markers"]
    ], dtype="float32")
    matriz = cv2.getPerspectiveTransform(origem, destino)
    return cv2.warpPerspective(cinza, matriz, (W, H))


def intensidade_circulo(img, cx, cy, raio):
    mask = np.zeros(img.shape, dtype="uint8")
    cv2.circle(mask, (cx, cy), raio, 255, -1)
    return 1 - (cv2.mean(img, mask=mask)[0] / 255)


def preenchimento_local(img, cx, cy, raio):
    """Retorna (fill, contraste). contraste = quanto a bolinha é mais escura que
    o papel ao redor. Imune a sombra suave (papel e bolinha escurecem juntos).

    Lê os pixels direto no quadradinho ao redor da bolinha, em vez de criar
    máscaras do tamanho da imagem inteira. Além de muito mais rápido, é o mesmo
    cálculo que o app faz no celular — antes o OpenCV arredondava a borda do
    círculo de um jeito ligeiramente diferente e os dois discordavam em casos
    no limite."""
    h, w = img.shape
    r_out = int(round(raio * 2.0))
    r_in = int(round(raio * 1.4))
    x0, x1 = max(0, cx - r_out), min(w - 1, cx + r_out)
    y0, y1 = max(0, cy - r_out), min(h - 1, cy + r_out)
    if x1 <= x0 or y1 <= y0:
        return 0.0, 0.0
    recorte = img[y0:y1 + 1, x0:x1 + 1].astype(np.float64)
    yy, xx = np.ogrid[y0:y1 + 1, x0:x1 + 1]
    dist2 = (xx - cx) ** 2 + (yy - cy) ** 2
    miolo = dist2 <= raio * raio
    if not miolo.any():
        return 0.0, 0.0
    brilho_miolo = recorte[miolo].mean()
    fill = 1 - (brilho_miolo / 255)
    papel = (dist2 >= r_in * r_in) & (dist2 <= r_out * r_out)
    if not papel.any():
        return fill, 0.0
    brilho_papel = recorte[papel].mean()
    # Contraste RELATIVO ao papel local, não em valor absoluto. Na sombra o
    # papel e a marcação escurecem juntos: a diferença absoluta despenca e a
    # marcação passaria por branco. Dividindo pelo brilho do papel ao redor, a
    # medida fica igual na sombra e na luz.
    ref = max(brilho_papel, 40.0)
    return fill, max(0.0, (brilho_papel - brilho_miolo) / ref)


def medir_vizinhanca(img, cx, cy, raio, passo_max=None):
    # Papel curvado desloca as bolinhas do meio da folha. Procuramos numa
    # vizinhança para absorver isso. O passo é limitado pelo espaçamento da
    # grade — passar disso invadiria a bolinha vizinha.
    # arredondamento igual ao do app: truncar aqui e arredondar lá fazia os
    # dois amostrarem posições diferentes e discordarem em casos no limite
    passo = round(raio * 0.55)
    if passo_max:
        passo = min(passo, round(passo_max))
    passo = max(2, int(passo))
    melhor = (0.0, 0.0)
    for dx in (0, -passo, passo):
        for dy in (0, -passo, passo):
            x, y = cx + dx, cy + dy
            if x - raio < 0 or y - raio < 0 or x + raio >= img.shape[1] or y + raio >= img.shape[0]:
                continue
            m = preenchimento_local(img, x, y, raio)
            if m[1] > melhor[1]:
                melhor = m
    return melhor


# Contraste a partir do qual uma bolinha conta como PINTADA por si só, sem
# depender da comparação com a mais escura da questão. Ver decidir_marcadas.
CONTRASTE_CERTO = 0.28


def decidir_marcadas(medidas):
    """medidas: {alt: (fill, contraste)}. Usa contraste local (imune a sombra)."""
    contrastes = {a: m[1] for a, m in medidas.items()}
    if not contrastes:
        return []
    max_c = max(contrastes.values())
    CONTRASTE_MIN = 0.16
    if max_c < CONTRASTE_MIN:
        return []
    # Uma bolinha conta como marcada em dois casos:
    #  (a) é parecida com a mais escura da questão (pelo menos 55% dela) — é a
    #      regra de sempre, que ignora a marca fraquinha de borracha; ou
    #  (b) está claramente pintada por si só (28% mais escura que o papel).
    # A regra (b) é nova. Sem ela, quando o aluno pintava DUAS bolinhas, uma
    # bem forte e outra mais clara, a clara ficava abaixo dos 55% e a questão
    # era lida como resposta única — podendo dar ponto para quem marcou duas.
    # Nos testes, bolinha vazia mede até 0,06, marca de borracha até 0,21 e
    # bolinha pintada a lápis claro de 0,24 para cima.
    return [a for a, c in contrastes.items()
            if c >= CONTRASTE_MIN and (c >= max_c * 0.55 or c >= CONTRASTE_CERTO)]


def _passo_maximo(layout, W, raio):
    """Até onde dá para procurar sem que a amostra encoste na bolinha vizinha.
    Se passarmos disso, a marcação do lado vaza para dentro da medição e vira
    multimarcação falsa — foi o que os testes com folha curvada mostraram."""
    menor = None
    for alts in layout["questoes"].values():
        xs = sorted(p["x"] for p in alts.values())
        for a, b in zip(xs, xs[1:]):
            d = (b - a) * W
            if menor is None or d < menor:
                menor = d
        break
    if not menor:
        return None
    return max(2.0, (menor - 2 * raio) * 0.9)


def ler_questoes(corrigida, layout, W, H):
    raio = int(layout["bubble_radius_pt"] / layout["page_width_pt"] * W * 0.95)
    passo_max = _passo_maximo(layout, W, raio)
    resultados = {}
    for q_num, alts in layout["questoes"].items():
        medidas = {}
        for alt, pos in alts.items():
            cx, cy = round(pos["x"] * W), round((1 - pos["y"]) * H)
            medidas[alt] = medir_vizinhanca(corrigida, cx, cy, raio, passo_max)
        marcadas = decidir_marcadas(medidas)
        resultados[q_num] = {
            "marcadas": marcadas,
            "resposta": marcadas[0] if len(marcadas) == 1 else None,
            "multi": len(marcadas) > 1,
        }
    return resultados


# Contraste mínimo para aceitar um dígito da matrícula (ou a versão) como
# marcado. Um pouco acima do das questões porque o número impresso dentro da
# bolinha pequena ocupa proporcionalmente mais espaço.
CONTRASTE_MIN_ID = 0.20


def _passo_maximo_pontos(pontos, W, raio):
    """Como _passo_maximo, mas para um conjunto qualquer de bolinhas (matrícula,
    versão): até onde dá para procurar sem encostar na bolinha vizinha."""
    pts = [(p["x"] * W, p["y"] * W) for p in pontos]   # mesma escala nos 2 eixos basta p/ comparar
    menor = None
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d = math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
            if menor is None or d < menor:
                menor = d
    if not menor:
        return None
    return max(2.0, (menor - 2 * raio) * 0.9)


def _mais_marcada(corrigida, opcoes, W, H, raio, passo_max):
    """Entre várias bolinhas (os 10 dígitos de uma coluna, ou as letras da
    versão), devolve a mais marcada — ou None se nenhuma se destaca do papel.

    Usa o MESMO critério das questões: contraste em relação ao papel ao redor.
    Antes a matrícula e a versão usavam o escuro absoluto da bolinha, e por
    isso erravam na sombra (o papel sombreado parecia "pintado") e com lápis
    fraco (a marca não chegava ao limite fixo)."""
    melhor, melhor_c = None, 0.0
    for nome, pos in opcoes.items():
        cx, cy = round(pos["x"] * W), round((1 - pos["y"]) * H)
        c = medir_vizinhanca(corrigida, cx, cy, raio, passo_max)[1]
        if c > melhor_c:
            melhor, melhor_c = nome, c
    return melhor if melhor_c >= CONTRASTE_MIN_ID else None


def ler_matricula(corrigida, layout, W, H):
    if "id_digitos" not in layout:
        return None
    raio = int(layout.get("digit_radius_pt", 6) / layout["page_width_pt"] * W * 0.95)
    # y em frações da ALTURA: converte para a escala da largura antes de medir distâncias
    todos = [{"x": p["x"], "y": p["y"] * H / W}
             for nums in layout["id_digitos"].values() for p in nums.values()]
    passo_max = _passo_maximo_pontos(todos, W, raio)
    digitos = []
    for d in sorted(layout["id_digitos"], key=int):
        n = _mais_marcada(corrigida, layout["id_digitos"][d], W, H, raio, passo_max)
        digitos.append(n if n is not None else "·")
    resultado = "".join(digitos)
    return resultado if resultado.strip("·") else None


def ler_versao(corrigida, layout, W, H):
    if "versao_bolhas" not in layout:
        return None
    raio = int(layout["bubble_radius_pt"] / layout["page_width_pt"] * W * 0.95)
    todos = [{"x": p["x"], "y": p["y"] * H / W} for p in layout["versao_bolhas"].values()]
    passo_max = _passo_maximo_pontos(todos, W, raio)
    return _mais_marcada(corrigida, layout["versao_bolhas"], W, H, raio, passo_max)


def recortar_regiao(corrigida, regiao, W, H):
    x1 = int(regiao["x"] * W)
    y1 = int((1 - regiao["y"] - regiao["h"]) * H)
    x2 = int((regiao["x"] + regiao["w"]) * W)
    y2 = int((1 - regiao["y"]) * H)
    pad = 4
    crop = corrigida[max(0, y1 - pad):y2 + pad, max(0, x1 - pad):x2 + pad]
    if crop.size == 0:
        return None
    _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return base64.b64encode(buf).decode()


def ler_qr(img):
    detector = cv2.QRCodeDetector()
    # O leitor de QR do OpenCV às vezes QUEBRA (em vez de só não achar nada)
    # em certas fotos — apareceu nos testes com a folha sobre fundo escuro.
    # Sem esta proteção o servidor respondia "erro 500" e a foto não era lida.
    # O QR é só uma conferência extra: se falhar, seguimos sem ele (o código
    # de barras lateral continua conferindo a folha).
    try:
        data, _, _ = detector.detectAndDecode(img)
    except cv2.error:
        return None, None
    if data:
        for prefixo in ("FOLHA:", "GABARITO:"):
            if data.startswith(prefixo):
                try:
                    return prefixo[:-1], int(data.split(":")[1])
                except (ValueError, IndexError):
                    return None, None
    return None, None


def normalizar_gabarito(gabarito):
    """Aceita formato antigo {'1':'A'} e novo {'1':{'corretas':['A'],'anulada':False,'pontos':1}}."""
    norm = {}
    for q, v in gabarito.items():
        if q == "_versoes":
            continue
        if isinstance(v, str):
            norm[q] = {"corretas": [v], "anulada": False, "pontos": 1.0}
        else:
            norm[q] = {
                "corretas": v.get("corretas", []),
                "anulada": bool(v.get("anulada", False)),
                "pontos": float(v.get("pontos", 1.0)),
            }
    return norm


def gabarito_tem_versoes(gabarito):
    """True se o gabarito guarda uma chave de respostas por versão de prova."""
    return isinstance(gabarito, dict) and isinstance(gabarito.get("_versoes"), dict)


def normalizar_gabarito_completo(gabarito):
    """Normaliza tanto o formato simples quanto o com versões.
    Retorna {'_versoes': {'A': {...}, 'B': {...}}} ou {questao: {...}}."""
    if gabarito_tem_versoes(gabarito):
        return {"_versoes": {v: normalizar_gabarito(g)
                             for v, g in gabarito["_versoes"].items()}}
    return normalizar_gabarito(gabarito)


def gabarito_da_versao(gabarito, versao):
    """Escolhe o conjunto de respostas certo para a versão lida na folha.
    Se o gabarito não tem versões, devolve ele mesmo. Se tem, mas a versão
    não foi lida ou não existe, devolve a primeira versão como aproximação."""
    if not gabarito_tem_versoes(gabarito):
        return normalizar_gabarito(gabarito), None
    versoes = gabarito["_versoes"]
    if versao and versao in versoes:
        return normalizar_gabarito(versoes[versao]), versao
    if not versoes:
        return {}, None
    primeira = sorted(versoes.keys())[0]
    return normalizar_gabarito(versoes[primeira]), primeira


def ler_barcode(corrigida, layout, W, H):
    """Lê o código de barras lateral da folha corrigida. Retorna folha_id ou None."""
    bc = layout.get("barcode")
    if not bc:
        return None
    medidas = []
    for cell in bc["cells"]:
        x1 = int(cell["x"] * W)
        y1 = int((1 - cell["y"] - cell["h"]) * H)
        x2 = int((cell["x"] + cell["w"]) * W)
        y2 = int((1 - cell["y"]) * H)
        if x2 <= x1 or y2 <= y1:
            return None
        # Procura a barra numa FAIXA em volta da posição prevista, ficando com o
        # trecho mais escuro. Um pequeno desvio de alinhamento — ou uma folha
        # impressa com barras de outra largura — desloca a barra alguns
        # milímetros, e exigir a posição exata fazia a leitura falhar.
        larg_cel = max(3, x2 - x1)
        busca_ini = max(0, x1 - int(larg_cel * 0.6))
        busca_fim = min(W, x2 + int(larg_cel * 1.4))
        regiao = None
        menor = None
        passo = max(1, larg_cel // 4)
        for xa in range(busca_ini, max(busca_ini + 1, busca_fim - larg_cel + 1), passo):
            trecho = corrigida[max(0, y1):y2, xa:xa + larg_cel]
            if trecho.size == 0:
                continue
            mt = float(trecho.mean())
            if menor is None or mt < menor:
                menor, regiao = mt, trecho
        if regiao is None or regiao.size == 0:
            return None

        # Referência = o VÃO entre as barras (acima e abaixo), na mesma faixa.
        # Esse espaço é sempre papel branco, em qualquer versão da folha.
        alt = y2 - y1
        refs = []
        ya1, ya2 = max(0, y1 - int(alt * 0.75)), max(0, y1 - int(alt * 0.15))
        if ya2 > ya1:
            r_ac = corrigida[ya1:ya2, busca_ini:busca_fim]
            if r_ac.size:
                refs.append(float(r_ac.mean()))
        yb1, yb2 = min(H - 1, y2 + int(alt * 0.15)), min(H, y2 + int(alt * 0.75))
        if yb2 > yb1:
            r_ab = corrigida[yb1:yb2, busca_ini:busca_fim]
            if r_ab.size:
                refs.append(float(r_ab.mean()))
        media_cel = float(regiao.mean())
        ref = max(max(refs), 40.0) if refs else 255.0
        medidas.append(max(0.0, (ref - media_cel) / ref))

    # Decide o que é barra pintada comparando com o contraste MAIS FORTE da
    # própria folha. Numa foto desfocada o preto "borra" e fica bem mais claro
    # (medimos 20% em vez de 60%), então um limite fixo perderia as barras.
    if len(medidas) != 14:
        return None
    forte = max(medidas)
    if forte < 0.07:                 # nenhuma barra visível
        return None
    corte = max(0.05, forte * 0.45)
    bits = [1 if v >= corte else 0 for v in medidas]
    if len(bits) != 14:
        return None
    dados, p1, p2 = bits[:12], bits[12], bits[13]
    if sum(dados[0:6]) % 2 != p1 or sum(dados[6:12]) % 2 != p2:
        return None
    valor = 0
    for b in dados:
        valor = (valor << 1) | b
    return valor if valor > 0 else None


# Uma foto de celular tem no máximo uns 50 megapixels (8160 x 6120). Arquivos
# acima disso não são foto: são "bombas" — arquivo pequeno que vira uma imagem
# enorme na memória. Nos testes, um PNG de 166 KB travou a leitura por mais de
# 9 minutos. Para PNG e outros formatos o limite é menor, porque eles são
# abertos inteiros na memória antes de reduzir.
MAX_PIXELS_JPEG = 52_000_000
MAX_PIXELS_OUTROS = 20_000_000
# A leitura não precisa de mais que isto: acima, a foto é aberta já reduzida.
# Fotos até 3000 px (como as que o app tira) são lidas exatamente como antes.
LADO_MAX_LEITURA = 3000


def abrir_foto(foto_bytes):
    """Abre a foto enviada com segurança. Devolve a imagem colorida (BGR)."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(foto_bytes)) as im:
            larg, alt = im.size
            formato = im.format
    except Image.DecompressionBombError:
        raise ValueError("Esta imagem tem resolução grande demais para ser uma foto da folha.")
    except Exception:
        raise ValueError("Não consegui abrir a imagem enviada.")
    limite = MAX_PIXELS_JPEG if formato == "JPEG" else MAX_PIXELS_OUTROS
    if larg * alt > limite:
        raise ValueError("Esta imagem tem resolução grande demais para ser uma foto da folha.")
    # Reduz já na abertura (o JPEG é decodificado direto no tamanho menor, sem
    # passar pela imagem cheia na memória).
    reducao = 1
    while max(larg, alt) / reducao > LADO_MAX_LEITURA and reducao < 8:
        reducao *= 2
    flag = {1: cv2.IMREAD_COLOR, 2: cv2.IMREAD_REDUCED_COLOR_2,
            4: cv2.IMREAD_REDUCED_COLOR_4, 8: cv2.IMREAD_REDUCED_COLOR_8}[reducao]
    img = cv2.imdecode(np.frombuffer(foto_bytes, dtype=np.uint8), flag)
    if img is None:
        raise ValueError("Não consegui abrir a imagem enviada.")
    return img


def processar_scan(foto_bytes, layout, gabarito_raw, folha_esperada=None):
    img = abrir_foto(foto_bytes)
    cinza = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Uma cópia compartilhada tem id (e código) próprio, mas quem aplica a
    # prova pode ter impresso a folha de QUALQUER ponto da cadeia de
    # compartilhamento (o dono original, ou um colega que repassou pra
    # frente). "localizadores_validos" — gravado no layout quando a folha é
    # criada/copiada — lista todos os códigos aceitos para esta folha. Se não
    # existir (folha antiga, de antes desta mudança), cai de volta para
    # aceitar só o próprio folha_esperada, como antes.
    validos = layout.get("localizadores_validos") or (
        [folha_esperada] if folha_esperada is not None else None)

    tipo_qr, qr_id = ler_qr(img)
    if validos and qr_id is not None and qr_id not in validos:
        raise ValueError(f"Esta folha tem localizador {qr_id:04d}, mas o quiz usa a folha "
                         f"{folha_esperada:04d}. Verifique se pegou a folha certa.")

    W = 1000
    H = int(W * layout["page_height_pt"] / layout["page_width_pt"])
    # acha os cantos, endireita e confere a escolha (código de barras + tamanho)
    # O código de barras guarda o número "reduzido" (ver codigo_barras), então
    # a comparação é feita com os números reduzidos dos gabaritos aceitos.
    validos_cod = [codigo_barras(v) for v in validos] if validos else None
    corrigida, id_barcode = localizar_folha(cinza, layout, W, H, validos_cod)

    # verificação pelo código de barras (funciona mesmo sem QR legível)
    if validos_cod and id_barcode is not None and id_barcode not in validos_cod:
        raise ValueError(f"Esta folha é de outro gabarito (código {id_barcode:04d}), mas esta "
                         f"correção usa o gabarito {folha_esperada:04d}. Verifique a folha.")
    # Código de barras ilegível não é motivo para recusar: em foto real (com
    # desfoque, granulação e textura do papel) ele falha com alguma frequência,
    # e recusar deixaria o professor sem conseguir corrigir. Seguimos em frente
    # e apenas sinalizamos que a folha não pôde ser confirmada.
    folha_confirmada = not (layout.get("barcode") and id_barcode is None)

    leitura = ler_questoes(corrigida, layout, W, H)
    aluno_id = ler_matricula(corrigida, layout, W, H)
    versao = ler_versao(corrigida, layout, W, H)

    # recorta TODAS as caixas do cabeçalho (Nome, Turma, Nº...) para conferência
    nome_img = None
    caixas_img = []
    caixas = layout.get("caixas", [])
    if caixas:
        for cx in caixas:
            img_b64 = recortar_regiao(corrigida, cx, W, H)
            caixas_img.append({"rotulo": cx.get("rotulo", ""), "img": img_b64})
        nome_img = caixas_img[0]["img"]  # compatibilidade
    elif "campo_nome" in layout:  # compat v2
        nome_img = recortar_regiao(corrigida, layout["campo_nome"], W, H)
        caixas_img = [{"rotulo": "Nome", "img": nome_img}]

    gabarito, versao_usada = gabarito_da_versao(gabarito_raw, versao)

    debug = cv2.cvtColor(corrigida, cv2.COLOR_GRAY2BGR)
    raio = int(layout["bubble_radius_pt"] / layout["page_width_pt"] * W * 1.25)

    acertos = erros = brancos = multi = anuladas = 0
    pontos_ganhos = 0.0
    pontos_total = 0.0
    detalhe = []

    for q in sorted(leitura, key=lambda x: int(x)):
        if q not in gabarito:
            continue
        info = leitura[q]
        gq = gabarito[q]
        pontos_total += gq["pontos"]
        if gq["anulada"]:
            status = "anulada"
            anuladas += 1
            pontos_ganhos += gq["pontos"]
        elif info["multi"]:
            status = "multimarcada"
            multi += 1
        elif info["resposta"] is None:
            status = "branco"
            brancos += 1
        elif info["resposta"] in gq["corretas"]:
            status = "certo"
            acertos += 1
            pontos_ganhos += gq["pontos"]
        else:
            status = "errado"
            erros += 1
        detalhe.append({
            "questao": q,
            "marcadas": info["marcadas"],
            "resposta": info["resposta"],
            "corretas": gq["corretas"],
            "anulada": gq["anulada"],
            "pontos": gq["pontos"],
            "status": status,
        })
        cores = {"certo": (0, 200, 0), "anulada": (200, 160, 0),
                 "multimarcada": (0, 165, 255)}
        for alt, pos in layout["questoes"][q].items():
            if alt in info["marcadas"]:
                cx, cy = round(pos["x"] * W), round((1 - pos["y"]) * H)
                cv2.circle(debug, (cx, cy), raio, cores.get(status, (0, 0, 220)), 3)

    nota = round(pontos_ganhos, 2)
    _, dbuf = cv2.imencode(".jpg", debug, [cv2.IMWRITE_JPEG_QUALITY, 82])

    return {
        "acertos": acertos, "erros": erros, "brancos": brancos,
        "multimarcadas": multi, "anuladas": anuladas,
        "nota": nota, "pontos_total": round(pontos_total, 2),
        "detalhe": detalhe,
        "debug_img": base64.b64encode(dbuf).decode(),
        "nome_img": nome_img,
        "caixas_img": caixas_img,
        "aluno_id": aluno_id,
        "versao": versao,
        "versao_usada": versao_usada,
        "folha_confirmada": folha_confirmada,
    }


# ══════════════════════════════════════════════
# PÁGINAS
# ══════════════════════════════════════════════

@app.route("/api/status")
def api_status():
    return jsonify({"banco_permanente": USA_POSTGRES})


@app.route("/.well-known/assetlinks.json")
def assetlinks():
    resp = send_from_directory("static", "assetlinks.json")
    resp.headers["Content-Type"] = "application/json"
    return resp


@app.route("/sw.js")
def service_worker():
    resp = send_from_directory("static", "sw.js")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Content-Type"] = "application/javascript"
    return resp


@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


@app.route("/login")
def login_page():
    return render_template("login.html")


# Email de contato mostrado na política de privacidade e na página de
# exclusão. Fica numa variável de ambiente do Render (CONTATO_EMAIL) para
# poder trocar sem mexer no código.
def _contato_email():
    return os.environ.get("CONTATO_EMAIL", "").strip()


@app.route("/privacidade")
def privacidade_page():
    # página pública: a Play Store abre este link sem estar logada
    return render_template("privacidade.html", contato=_contato_email())


@app.route("/excluir-conta")
def excluir_conta_page():
    return render_template("excluir_conta.html", contato=_contato_email())


@app.route("/dashboard")
@login_required
def dashboard():
    # Se o professor chegou por um link de gabarito compartilhado e precisou
    # fazer login no caminho, importamos agora — senão o link se perderia.
    token = session.pop("gabarito_pendente", None)
    if token:
        return _importar_gabarito(token)
    return render_template("dashboard.html", user_nome=session.get("user_nome", ""))


@app.route("/folha/nova")
@login_required
def folha_nova_page():
    return render_template("folha_nova.html")


@app.route("/quiz/<int:quiz_id>")
@login_required
def quiz_page(quiz_id):
    db = get_db()
    quiz = db.execute("SELECT * FROM quizzes WHERE id=? AND user_id=?",
                      (quiz_id, session["user_id"])).fetchone()
    if not quiz:
        return redirect(url_for("dashboard"))
    return render_template("quiz.html", quiz_id=quiz_id, quiz_nome=quiz["nome"])


@app.route("/app")
@login_required
def app_offline():
    try:
        caminho = os.path.join(app.root_path, "templates", "corrigir.html")
        marca = str(int(os.path.getmtime(caminho)))
    except Exception:
        marca = "0"
    return render_template("corrigir.html", versao_app=marca)


@app.route("/offline/<int:quiz_id>")
@login_required
def offline_page(quiz_id):
    """Tela antiga de correção, mantida só como atalho.

    Ela usava a câmera do sistema (uma foto por vez, sem captura automática) e
    convivia com a tela nova — quem caísse nela achava que o app tinha voltado
    a uma versão anterior. Agora leva direto para o app de correção."""
    return redirect(url_for("app_offline"))


@app.route("/f/<token>")
def compartilhar_folha(token):
    # Sem login, guardamos o link e mandamos entrar. Antes o token se perdia
    # aqui: o professor entrava e o gabarito simplesmente não aparecia.
    if "user_id" not in session:
        session["gabarito_pendente"] = token
        return redirect(url_for("login_page"))
    return _importar_gabarito(token)


def _importar_gabarito(token):
    db = get_db()
    uid = session["user_id"]
    origem = db.execute("SELECT * FROM folhas WHERE share_token=?", (token,)).fetchone()
    if not origem:
        return redirect(url_for("dashboard"))

    # é a própria folha do usuário? só volta ao painel
    if origem["user_id"] == uid:
        return redirect(url_for("dashboard"))

    # já importou este token antes? não duplica
    ja = db.execute("SELECT id FROM folhas WHERE user_id=? AND copiada_de_token=?",
                    (uid, token)).fetchone()
    if ja:
        return redirect(url_for("dashboard"))
    if db.execute("SELECT COUNT(*) AS n FROM folhas WHERE user_id=?",
                  (uid,)).fetchone()["n"] >= MAX_FOLHAS_POR_CONTA:
        return redirect(url_for("dashboard"))

    # cria uma CÓPIA independente, dona de quem recebeu, com localizador e
    # código de barras próprios (o barcode carrega o id da folha, então precisa
    # ser recalculado para o novo id — senão duas folhas teriam o mesmo código).
    config = json.loads(origem["config"])
    novo_token = secrets.token_urlsafe(12)
    cur = db.execute(
        "INSERT INTO folhas (user_id, nome, config, layout, share_token, copiada_de_token) "
        "VALUES (?,?,?,?,?,?)",
        (uid, origem["nome"], origem["config"], "{}", novo_token, token))
    nova_id = cur.lastrowid
    layout, erro = compute_layout(config, nova_id)
    if erro:
        db.execute("DELETE FROM folhas WHERE id=?", (nova_id,))
        db.commit()
        return redirect(url_for("dashboard"))
    # aqui está a correção do problema do localizador: a cópia sobe a cadeia
    # de compartilhamento e aceita o código de QUALQUER folha impressa por
    # ela (dono original ou repasses no meio), não só o código dela mesma.
    layout["localizadores_validos"] = resolver_localizadores_validos(db, nova_id, token)
    db.execute("UPDATE folhas SET layout=? WHERE id=?", (json.dumps(layout), nova_id))
    db.commit()
    return redirect(url_for("dashboard"))


# ══════════════════════════════════════════════
# API — AUTENTICAÇÃO
# ══════════════════════════════════════════════

def gerar_codigo():
    alfabeto = string.ascii_uppercase + string.digits
    return "-".join("".join(secrets.choice(alfabeto) for _ in range(4)) for _ in range(2))


@app.route("/api/registrar", methods=["POST"])
def api_registrar():
    data = _json()
    nome = _texto(data.get("nome"), 80)
    email = _texto(data.get("email"), 200).lower()
    senha = data.get("senha") if isinstance(data.get("senha"), str) else ""
    if not nome or not email or len(senha) < 6:
        return jsonify({"erro": "Preencha nome, email e senha (mín. 6 caracteres)."}), 400
    if len(email) > 120 or "@" not in email or " " in email:
        return jsonify({"erro": "Email inválido."}), 400
    if len(senha) > 200:
        return jsonify({"erro": "A senha pode ter no máximo 200 caracteres."}), 400
    db = get_db()
    # Cadastro é aberto: limita quantas contas um mesmo lugar cria por hora,
    # para um robô não conseguir criar milhares de contas e encher o banco.
    chave_cad = "cadastro:ip:" + _ip_cliente()
    if _bloqueado(db, [(chave_cad, 10)], janela=3600):
        return jsonify({"erro": "Muitos cadastros feitos daqui em pouco tempo. "
                                "Espere uma hora e tente de novo."}), 429
    _anotar_tentativa(db, chave_cad)
    codigo = gerar_codigo()
    existente = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if existente:
        return jsonify({"erro": "Este email já está cadastrado."}), 409
    cur = db.execute(
        "INSERT INTO users (nome, email, senha_hash, codigo_recuperacao) VALUES (?,?,?,?)",
        (nome, email, generate_password_hash(senha), generate_password_hash(codigo)))
    db.commit()
    session.permanent = True
    session["user_id"] = cur.lastrowid
    session["user_nome"] = nome
    return jsonify({"ok": True, "codigo_recuperacao": codigo})


@app.route("/api/login", methods=["POST"])
def api_login():
    data = _json()
    email = _texto(data.get("email"), 200).lower()
    senha = data.get("senha") if isinstance(data.get("senha"), str) else ""
    db = get_db()
    # Bloqueado: recusa ATÉ a senha certa. Senão o robô continuaria testando e
    # descobriria a senha quando ela fosse aceita.
    if _bloqueado(db, _limites_senha(email)):
        return _resposta_bloqueio()
    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or len(senha) > 200 or not check_password_hash(user["senha_hash"], senha):
        _anotar_tentativa(db, *_chaves_senha(email))
        return jsonify({"erro": "Email ou senha incorretos."}), 401
    _esquecer_tentativas(db, "senha:email:" + email)
    session.permanent = True
    session["user_id"] = user["id"]
    session["user_nome"] = user["nome"]
    return jsonify({"ok": True})


@app.route("/api/recuperar", methods=["POST"])
def api_recuperar():
    data = _json()
    email = _texto(data.get("email"), 200).lower()
    codigo = _texto(data.get("codigo"), 40).upper()
    nova = data.get("nova_senha") if isinstance(data.get("nova_senha"), str) else ""
    if len(nova) < 6:
        return jsonify({"erro": "A nova senha precisa de pelo menos 6 caracteres."}), 400
    if len(nova) > 200:
        return jsonify({"erro": "A senha pode ter no máximo 200 caracteres."}), 400
    db = get_db()
    limites = [("recuperar:email:" + email, 5), ("recuperar:ip:" + _ip_cliente(), 20)]
    if _bloqueado(db, limites):
        return _resposta_bloqueio()
    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not user["codigo_recuperacao"] or \
       not check_password_hash(user["codigo_recuperacao"], codigo):
        _anotar_tentativa(db, *(ch for ch, _ in limites))
        return jsonify({"erro": "Email ou código de recuperação incorretos."}), 401
    _esquecer_tentativas(db, "recuperar:email:" + email)
    novo_codigo = gerar_codigo()
    db.execute("UPDATE users SET senha_hash=?, codigo_recuperacao=? WHERE id=?",
               (generate_password_hash(nova), generate_password_hash(novo_codigo), user["id"]))
    db.commit()
    return jsonify({"ok": True, "novo_codigo": novo_codigo})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


# ══════════════════════════════════════════════
# API — FOLHAS
# ══════════════════════════════════════════════

@app.route("/api/folhas", methods=["GET"])
@login_required
def api_listar_folhas():
    db = get_db()
    uid = session["user_id"]
    minhas = db.execute("SELECT * FROM folhas WHERE user_id=? ORDER BY id DESC", (uid,)).fetchall()
    compartilhadas = db.execute("""
        SELECT f.*, u.nome as autor FROM folhas_compartilhadas fc
        JOIN folhas f ON f.id = fc.folha_id
        JOIN users u ON u.id = f.user_id
        WHERE fc.user_id=? ORDER BY f.id DESC
    """, (uid,)).fetchall()

    def resumo(f, autor=None):
        cfg = json.loads(f["config"])
        try:
            lay = json.loads(f["layout"])
        except Exception:
            lay = {}
        return {
            "id": f["id"], "nome": f["nome"],
            "localizador": f"{f['id']:04d}",
            "n_questoes": len(cfg.get("questoes", [])),
            "share_token": f["share_token"],
            "autor": autor,
            "versoes": sorted(lay.get("versao_bolhas", {}).keys()),
            # para o painel mostrar "folha compacta" / "folha grande"
            "formato": "compacta" if lay.get("formato") == "compacta" else "grande",
        }
    return jsonify({
        "minhas": [resumo(f) for f in minhas],
        "compartilhadas": [resumo(f, f["autor"]) for f in compartilhadas],
    })


@app.route("/api/folhas", methods=["POST"])
@login_required
def api_criar_folha():
    config, erro = validar_config(_json())
    if erro:
        return jsonify({"erro": erro}), 400
    nome = config["nome"]

    db = get_db()
    if db.execute("SELECT COUNT(*) AS n FROM folhas WHERE user_id=?",
                  (session["user_id"],)).fetchone()["n"] >= MAX_FOLHAS_POR_CONTA:
        return jsonify({"erro": f"Limite de {MAX_FOLHAS_POR_CONTA} gabaritos por conta. "
                                "Apague alguns que não usa mais."}), 400
    token = secrets.token_urlsafe(12)
    cur = db.execute("INSERT INTO folhas (user_id, nome, config, layout, share_token) VALUES (?,?,?,?,?)",
                     (session["user_id"], nome, json.dumps(config), "{}", token))
    folha_id = cur.lastrowid
    layout, erro = compute_layout(config, folha_id)
    if erro:
        db.execute("DELETE FROM folhas WHERE id=?", (folha_id,))
        db.commit()
        return jsonify({"erro": erro}), 400
    # folha original: por enquanto o único código válido é o dela mesma (se
    # for compartilhada depois, quem receber ganha a lista completa).
    layout["localizadores_validos"] = resolver_localizadores_validos(db, folha_id, None)
    db.execute("UPDATE folhas SET layout=? WHERE id=?", (json.dumps(layout), folha_id))
    db.commit()
    return jsonify({"ok": True, "folha_id": folha_id, "localizador": f"{folha_id:04d}"})


@app.route("/api/folhas/<int:folha_id>/pdf", methods=["GET"])
@login_required
def api_folha_pdf(folha_id):
    copias = _int_arg("copias", 1, 1, 2)
    escuro = _int_arg("escuro", 0, 0, 3)
    versao = (request.args.get("versao") or "").strip().upper()[:6] or None
    db = get_db()
    uid = session["user_id"]
    f = db.execute("""
        SELECT f.* FROM folhas f
        LEFT JOIN folhas_compartilhadas fc ON fc.folha_id = f.id AND fc.user_id=?
        WHERE f.id=? AND (f.user_id=? OR fc.id IS NOT NULL)
    """, (uid, folha_id, uid)).fetchone()
    if not f:
        return jsonify({"erro": "Folha não encontrada"}), 404
    layout = json.loads(f["layout"])
    # só aceita uma versão que realmente existe nesta folha
    if versao and versao not in layout.get("versao_bolhas", {}):
        versao = None
    quer_png = (request.args.get("formato") or "").lower() == "png"
    # Imagem da folha compacta: sai SÓ a folha (sem a A4 em branco em volta),
    # em tamanho real — é para colar direto no arquivo da prova.
    pdf = gerar_pdf_folha(json.loads(f["config"]), layout,
                          folha_id, copias=copias, escuro=escuro, versao_marcada=versao,
                          so_folha=quer_png)
    # ?formato=png devolve a MESMA folha como imagem. Converter o próprio PDF
    # (em vez de desenhar a folha de novo com outra biblioteca) garante que a
    # imagem é idêntica ao que já funciona na leitura: marcadores, código de
    # barras e bolinhas nas mesmas posições.
    if quer_png:
        return jsonify({"png": base64.b64encode(pdf_para_png(pdf)).decode()})
    return jsonify({"pdf": base64.b64encode(pdf).decode()})


def pdf_para_png(pdf_bytes, dpi=300):
    """Converte a 1ª página do PDF da folha em PNG.

    300 DPI = qualidade de impressão (A4 vira 2480 x 3508 pixels): as bordas
    das bolinhas e as barras do código lateral saem nítidas mesmo se a imagem
    for impressa ou colada num documento. PNG (e não JPEG) porque JPEG borra
    bordas finas e cria "sujeira" em volta das barras, o que atrapalharia a
    leitura do código de barras. Como a folha é quase toda branca, o PNG fica
    pequeno (poucas centenas de KB).
    """
    # import aqui dentro: se a biblioteca faltar no servidor, só o botão de
    # imagem falha — o PDF e o resto do app continuam funcionando.
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        pagina = doc[0]
        # o PDF trabalha em pontos (72 por polegada); escala = DPI desejado / 72
        bitmap = pagina.render(scale=dpi / 72)
        img = bitmap.to_pil().convert("RGB")
        buf = io.BytesIO()
        # dpi gravado no arquivo: Word/impressora sabem que é uma folha A4 e
        # não esticam nem encolhem a imagem ao inserir/imprimir
        img.save(buf, format="PNG", optimize=True, dpi=(dpi, dpi))
        return buf.getvalue()
    finally:
        doc.close()


# ══════════════════════════════════════════════
# API — QUIZZES
# ══════════════════════════════════════════════

@app.route("/api/quizzes/<int:quiz_id>/pacote_offline", methods=["GET"])
@login_required
def api_pacote_offline(quiz_id):
    """Retorna tudo que o celular precisa para corrigir sem internet:
    layout da folha, gabarito normalizado e nome do quiz."""
    db = get_db()
    q = db.execute("SELECT * FROM quizzes WHERE id=? AND user_id=?",
                   (quiz_id, session["user_id"])).fetchone()
    if not q:
        return jsonify({"erro": "Quiz não encontrado"}), 404
    gabarito = json.loads(q["gabarito"] or "{}")
    if not gabarito:
        return jsonify({"erro": "Defina o gabarito antes de baixar o pacote offline."}), 400
    layout = json.loads(q["layout"])
    folha_esperada = layout.get("folha_id") or layout.get("quiz_id")
    return jsonify({
        "quiz_id": quiz_id,
        "quiz_nome": q["nome"],
        "layout": layout,
        "gabarito": normalizar_gabarito_completo(gabarito),
        "folha_esperada": folha_esperada,
        "limiar_marcacao": LIMIAR_MARCACAO,
    })


@app.route("/api/quizzes/<int:quiz_id>/sync", methods=["POST"])
@login_required
def api_sync(quiz_id):
    """Recebe um lote de correções feitas offline (já processadas no celular)
    e salva no banco, como se tivessem vindo do /scan normal."""
    db = get_db()
    q = db.execute("SELECT id FROM quizzes WHERE id=? AND user_id=?",
                   (quiz_id, session["user_id"])).fetchone()
    if not q:
        return jsonify({"erro": "Quiz não encontrado"}), 404
    data = _json()
    resultados = data.get("resultados", [])
    if not isinstance(resultados, list):
        return jsonify({"erro": "Formato inválido."}), 400
    if len(resultados) > MAX_PROVAS_POR_ENVIO:
        return jsonify({"erro": "Provas demais num envio só."}), 400
    # Se passar do limite, recusa o envio INTEIRO com aviso: o celular então
    # mantém as provas como pendentes (nada se perde).
    if _contar_provas_da_conta(db, session["user_id"]) + len(resultados) > MAX_PROVAS_POR_CONTA:
        return jsonify({"erro": f"Esta conta chegou ao limite de {MAX_PROVAS_POR_CONTA} provas "
                                "guardadas. Apague correções antigas para liberar espaço."}), 400

    salvos = 0
    # Guardamos o id real de cada scan inserido, na MESMA ORDEM em que
    # chegou em "resultados". Sem isso, o celular que enviou não tem como
    # saber depois quais provas do servidor são as que ele mesmo mandou —
    # e acabaria baixando elas de novo como se fossem de outro aparelho,
    # duplicando na lista de correções.
    ids = []
    for bruto in resultados:
        try:
            r = _limpar_resultado(bruto)
            if r is None:
                raise ValueError("prova em formato inválido")
            cur = db.execute("""INSERT INTO scans
                (quiz_id, respostas, acertos, erros, brancos, multimarcadas, nota,
                 debug_img, nome_img, caixas_img, aluno_id, versao, img_recortada)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (quiz_id, json.dumps(r["detalhe"]),
                 r["acertos"], r["erros"], r["brancos"], r["multimarcadas"], r["nota"],
                 r["debug_img"], r["nome_img"], json.dumps(r["caixas_img"]),
                 r["aluno_id"], r["versao"], r["img_recortada"]))
            salvos += 1
            ids.append(cur.lastrowid)
        except Exception:
            ids.append(None)
            continue
    db.commit()
    return jsonify({"ok": True, "salvos": salvos, "recebidos": len(resultados), "ids": ids})


@app.route("/api/versao", methods=["GET"])
def api_versao():
    """Identifica a versão publicada do app.

    Como a tela do app é servida do cache do celular (para abrir instantâneo),
    uma atualização só apareceria na abertura seguinte. Este endereço permite
    ao app perceber que saiu versão nova e oferecer o recarregamento."""
    try:
        caminho = os.path.join(app.root_path, "templates", "corrigir.html")
        marca = str(int(os.path.getmtime(caminho)))
    except Exception:
        marca = "0"
    return jsonify({"versao": marca})


@app.route("/api/eu", methods=["GET"])
@login_required
def api_eu():
    db = get_db()
    u = db.execute("SELECT id, nome, email FROM users WHERE id=?",
                   (session["user_id"],)).fetchone()
    if not u:
        return jsonify({"erro": "Usuário não encontrado"}), 404
    return jsonify({"id": u["id"], "nome": u["nome"], "email": u["email"]})


def _apagar_usuario(db, uid):
    """Apaga a conta e TUDO que pertence a ela, na ordem que o banco exige.

    A ordem importa por causa das chaves estrangeiras do PostgreSQL: não dá
    para apagar uma folha enquanto outra linha ainda aponta para ela.
    O caso que quebrava antes: no modo antigo de compartilhar, um COLEGA tinha
    uma linha em folhas_compartilhadas apontando para a folha deste professor.
    Só apagávamos as linhas do próprio professor, então o PostgreSQL recusava
    apagar a folha e a exclusão da conta falhava com erro 500. (No SQLite local
    isso não aparecia, porque ele não confere chave estrangeira por padrão.)

    As CÓPIAS que colegas fizeram dos gabaritos deste professor NÃO são
    apagadas: são independentes e pertencem a eles. Elas continuam funcionando
    porque guardam o próprio layout e a lista de códigos aceitos."""
    db.execute("DELETE FROM scans WHERE quiz_id IN (SELECT id FROM quizzes WHERE user_id=?)", (uid,))
    db.execute("DELETE FROM quizzes WHERE user_id=?", (uid,))
    db.execute("DELETE FROM folhas_compartilhadas WHERE user_id=? "
               "OR folha_id IN (SELECT id FROM folhas WHERE user_id=?)", (uid, uid))
    db.execute("DELETE FROM folhas WHERE user_id=?", (uid,))
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.commit()


@app.route("/api/deletar_conta", methods=["POST"])
@login_required
def api_deletar_conta():
    """Exclusão pelo app (tela Sincronizar → Deletar minha conta)."""
    data = _json()
    senha = data.get("senha") if isinstance(data.get("senha"), str) else ""
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    if not u:
        return jsonify({"erro": "Senha incorreta. Digite sua senha para confirmar."}), 401
    if _bloqueado(db, _limites_senha(u["email"])):
        return _resposta_bloqueio()
    if len(senha) > 200 or not check_password_hash(u["senha_hash"], senha):
        _anotar_tentativa(db, *_chaves_senha(u["email"]))
        return jsonify({"erro": "Senha incorreta. Digite sua senha para confirmar."}), 401
    _apagar_usuario(db, u["id"])
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/excluir_conta_web", methods=["POST"])
def api_excluir_conta_web():
    """Exclusão pela página /excluir-conta, SEM precisar estar logado.

    A Play Store exige um endereço na web onde a pessoa consiga apagar a conta
    mesmo sem o app instalado. Por isso aqui a identificação é por email +
    senha, e não pela sessão."""
    data = _json()
    email = _texto(data.get("email"), 200).lower()
    senha = data.get("senha") if isinstance(data.get("senha"), str) else ""
    db = get_db()
    # mesmo limite de tentativas do login: esta página também testa a senha
    if _bloqueado(db, _limites_senha(email)):
        return _resposta_bloqueio()
    u = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    # mesma mensagem para email inexistente e senha errada: não revela
    # quais emails têm conta
    if not u or len(senha) > 200 or not check_password_hash(u["senha_hash"], senha):
        _anotar_tentativa(db, *_chaves_senha(email))
        return jsonify({"erro": "Email ou senha incorretos."}), 401
    _apagar_usuario(db, u["id"])
    if session.get("user_id") == u["id"]:
        session.clear()
    return jsonify({"ok": True})


@app.route("/api/folhas/<int:folha_id>", methods=["DELETE"])
@login_required
def api_deletar_folha(folha_id):
    """Apaga um gabarito (folha). Só o dono pode, e só se nenhuma correção
    estiver usando a folha — senão as correções ficariam órfãs."""
    db = get_db()
    uid = session["user_id"]
    f = db.execute("SELECT id FROM folhas WHERE id=? AND user_id=?",
                   (folha_id, uid)).fetchone()
    if not f:
        return jsonify({"erro": "Gabarito não encontrado (ou não é seu)."}), 404

    minhas = db.execute("SELECT COUNT(*) AS n FROM quizzes WHERE folha_id=? AND user_id=?",
                        (folha_id, uid)).fetchone()["n"]
    de_outros = db.execute("SELECT COUNT(*) AS n FROM quizzes WHERE folha_id=? AND user_id!=?",
                           (folha_id, uid)).fetchone()["n"]
    if minhas:
        return jsonify({"erro": f"Este gabarito está sendo usado por {minhas} "
                        f"correção(ões) sua(s). Apague essas correções primeiro."}), 400
    if de_outros:
        return jsonify({"erro": "Este gabarito foi compartilhado e outras pessoas "
                        "têm correções usando ele. Não é possível apagar."}), 400

    db.execute("DELETE FROM folhas_compartilhadas WHERE folha_id=?", (folha_id,))
    db.execute("DELETE FROM folhas WHERE id=?", (folha_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/quizzes/<int:quiz_id>", methods=["DELETE"])
@login_required
def api_deletar_quiz(quiz_id):
    db = get_db()
    q = db.execute("SELECT id FROM quizzes WHERE id=? AND user_id=?",
                   (quiz_id, session["user_id"])).fetchone()
    if not q:
        return jsonify({"erro": "Quiz não encontrado"}), 404
    db.execute("DELETE FROM scans WHERE quiz_id=?", (quiz_id,))
    db.execute("DELETE FROM quizzes WHERE id=?", (quiz_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/pacote_completo", methods=["GET"])
@login_required
def api_pacote_completo():
    """Retorna TODOS os quizzes do usuário com layout+gabarito, para o app
    guardar tudo localmente de uma vez e funcionar offline."""
    db = get_db()
    uid = session["user_id"]
    quizzes = db.execute("SELECT * FROM quizzes WHERE user_id=? ORDER BY id DESC", (uid,)).fetchall()
    saida = []
    for q in quizzes:
        layout = json.loads(q["layout"])
        gabarito = json.loads(q["gabarito"] or "{}")
        labels = layout.get("labels_questoes", {})
        if not labels:
            labels = {qq: "ABCDE" for qq in layout.get("questoes", {})}
        saida.append({
            "quiz_id": q["id"],
            "quiz_nome": q["nome"],
            "n_questoes": q["n_questoes"],
            "layout": layout,
            "gabarito": normalizar_gabarito_completo(gabarito) if gabarito else {},
            "gabarito_ts": q["gabarito_ts"] or 0,
            "labels": labels,
            "folha_esperada": layout.get("folha_id") or layout.get("quiz_id"),
            "tem_gabarito": bool(gabarito),
        })
    # gabaritos (folhas) disponíveis, COM layout completo, para criar
    # correções offline usando um gabarito já baixado.
    folhas_rows = db.execute("SELECT id, nome, config, layout, share_token FROM folhas WHERE user_id=? ORDER BY id DESC", (uid,)).fetchall()
    comp_rows = db.execute("""
        SELECT f.id, f.nome, f.config, f.layout, f.share_token FROM folhas_compartilhadas fc
        JOIN folhas f ON f.id = fc.folha_id WHERE fc.user_id=? ORDER BY f.id DESC
    """, (uid,)).fetchall()
    folhas = []
    for f in list(folhas_rows) + list(comp_rows):
        cfg = json.loads(f["config"])
        layout = json.loads(f["layout"])
        labels = layout.get("labels_questoes", {})
        if not labels:
            labels = {qq: "ABCDE" for qq in layout.get("questoes", {})}
        folhas.append({
            "id": f["id"], "nome": f["nome"],
            "localizador": f"{f['id']:04d}",
            "n_questoes": len(cfg.get("questoes", [])),
            "layout": layout,
            "labels": labels,
            "share_token": f["share_token"],
        })

    return jsonify({"quizzes": saida, "folhas": folhas, "limiar_marcacao": LIMIAR_MARCACAO})


@app.route("/api/quizzes", methods=["GET"])
@login_required
def api_listar_quizzes():
    db = get_db()
    rows = db.execute("""
        SELECT q.*, COUNT(s.id) as n_scans
        FROM quizzes q LEFT JOIN scans s ON s.quiz_id = q.id
        WHERE q.user_id=? GROUP BY q.id ORDER BY q.id DESC
    """, (session["user_id"],)).fetchall()
    return jsonify([{
        "id": r["id"], "nome": r["nome"], "n_questoes": r["n_questoes"],
        "n_scans": r["n_scans"], "folha_id": r["folha_id"],
        "tem_gabarito": r["gabarito"] not in ("{}", None, ""),
    } for r in rows])


@app.route("/api/quizzes", methods=["POST"])
@login_required
def api_criar_quiz():
    data = _json()
    nome = _texto(data.get("nome"), 100)
    folha_id = data.get("folha_id")
    if not nome or not isinstance(folha_id, int):
        return jsonify({"erro": "Informe o nome do quiz e escolha uma folha."}), 400
    db = get_db()
    uid = session["user_id"]
    if db.execute("SELECT COUNT(*) AS n FROM quizzes WHERE user_id=?",
                  (uid,)).fetchone()["n"] >= MAX_CORRECOES_POR_CONTA:
        return jsonify({"erro": f"Limite de {MAX_CORRECOES_POR_CONTA} correções por conta. "
                                "Apague algumas antigas."}), 400
    f = db.execute("""
        SELECT f.* FROM folhas f
        LEFT JOIN folhas_compartilhadas fc ON fc.folha_id = f.id AND fc.user_id=?
        WHERE f.id=? AND (f.user_id=? OR fc.id IS NOT NULL)
    """, (uid, folha_id, uid)).fetchone()
    if not f:
        return jsonify({"erro": "Folha não encontrada."}), 404
    cfg = json.loads(f["config"])
    n = len(cfg.get("questoes", []))
    cur = db.execute("""INSERT INTO quizzes (user_id, nome, n_questoes, layout, folha_id)
                        VALUES (?,?,?,?,?)""",
                     (uid, nome, n, f["layout"], folha_id))
    db.commit()
    return jsonify({"ok": True, "quiz_id": cur.lastrowid})


@app.route("/api/sync_correcoes_offline", methods=["POST"])
@login_required
def api_sync_correcoes_offline():
    """Recebe correções que foram criadas OFFLINE no celular (a partir de um
    gabarito já baixado) e as materializa no servidor: cria o quiz vinculado à
    folha, grava o gabarito e insere os resultados. Retorna o mapeamento de
    IDs temporários (do celular) para IDs reais (do servidor)."""
    data = _json()
    itens = data.get("correcoes_offline", [])
    if not isinstance(itens, list):
        return jsonify({"erro": "Formato inválido."}), 400
    db = get_db()
    uid = session["user_id"]
    mapa = {}  # id_temp -> id_real
    n_quizzes = db.execute("SELECT COUNT(*) AS n FROM quizzes WHERE user_id=?",
                           (uid,)).fetchone()["n"]
    n_provas = _contar_provas_da_conta(db, uid)

    for item in itens[:50]:
        if not isinstance(item, dict):
            continue
        id_temp = item.get("id_temp")
        nome = _texto(item.get("nome"), 100) or "Correção"
        folha_id = item.get("folha_id")
        gabarito = _limpar_gabarito(item.get("gabarito", {}))
        gab_ts = int(_num(item.get("gabarito_ts"), True))
        resultados = item.get("resultados", [])
        if not isinstance(resultados, list) or len(resultados) > MAX_PROVAS_POR_ENVIO:
            continue
        # Acima do limite, a correção não é criada agora: fica pendente no
        # celular (não entra no "mapa"), sem perder nada.
        if n_quizzes >= MAX_CORRECOES_POR_CONTA or n_provas + len(resultados) > MAX_PROVAS_POR_CONTA:
            continue

        f = db.execute("""
            SELECT f.* FROM folhas f
            LEFT JOIN folhas_compartilhadas fc ON fc.folha_id = f.id AND fc.user_id=?
            WHERE f.id=? AND (f.user_id=? OR fc.id IS NOT NULL)
        """, (uid, folha_id, uid)).fetchone()
        if not f:
            continue
        cfg = json.loads(f["config"])
        n = len(cfg.get("questoes", []))
        cur = db.execute("""INSERT INTO quizzes (user_id, nome, n_questoes, layout, folha_id, gabarito, gabarito_ts)
                            VALUES (?,?,?,?,?,?,?)""",
                         (uid, nome, n, f["layout"], folha_id, json.dumps(gabarito), gab_ts))
        novo_id = cur.lastrowid
        mapa[str(id_temp)] = novo_id
        n_quizzes += 1
        n_provas += len(resultados)

        for bruto in resultados:
            try:
                r = _limpar_resultado(bruto)
                if r is None:
                    continue
                db.execute("""INSERT INTO scans
                    (quiz_id, respostas, acertos, erros, brancos, multimarcadas, nota,
                     debug_img, nome_img, caixas_img, aluno_id, versao, img_recortada)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (novo_id, json.dumps(r["detalhe"]),
                     r["acertos"], r["erros"], r["brancos"], r["multimarcadas"], r["nota"],
                     r["debug_img"], r["nome_img"], json.dumps(r["caixas_img"]),
                     r["aluno_id"], r["versao"], r["img_recortada"]))
            except Exception:
                continue
    db.commit()
    return jsonify({"ok": True, "mapa": mapa})


@app.route("/api/quizzes/<int:quiz_id>", methods=["GET"])
@login_required
def api_quiz(quiz_id):
    db = get_db()
    q = db.execute("SELECT * FROM quizzes WHERE id=? AND user_id=?",
                   (quiz_id, session["user_id"])).fetchone()
    if not q:
        return jsonify({"erro": "Quiz não encontrado"}), 404
    layout = json.loads(q["layout"])
    labels = layout.get("labels_questoes", {})
    if not labels:  # compat v2
        labels = {qq: "ABCDE" for qq in layout.get("questoes", {})}
    return jsonify({
        "id": q["id"], "nome": q["nome"], "n_questoes": q["n_questoes"],
        "folha_id": q["folha_id"],
        "gabarito": normalizar_gabarito_completo(json.loads(q["gabarito"] or "{}")),
        "labels": labels,
        "versoes": sorted(layout.get("versao_bolhas", {}).keys()),
    })


@app.route("/api/quizzes/<int:quiz_id>/gabarito", methods=["PUT"])
@login_required
def api_salvar_gabarito(quiz_id):
    data = _json()
    gabarito = data.get("gabarito", {})
    if not isinstance(gabarito, dict) or _limpar_gabarito(gabarito) is not gabarito:
        return jsonify({"erro": "Gabarito inválido ou grande demais."}), 400
    ts = int(_num(data.get("ts"), True) or (datetime.now().timestamp() * 1000))
    db = get_db()
    q = db.execute("SELECT id FROM quizzes WHERE id=? AND user_id=?",
                   (quiz_id, session["user_id"])).fetchone()
    if not q:
        return jsonify({"erro": "Quiz não encontrado"}), 404
    db.execute("UPDATE quizzes SET gabarito=?, gabarito_ts=? WHERE id=?",
               (json.dumps(gabarito), ts, quiz_id))
    db.commit()
    return jsonify({"ok": True, "ts": ts})


@app.route("/api/quizzes/<int:quiz_id>/scan", methods=["POST"])
@login_required
def api_scan(quiz_id):
    db = get_db()
    q = db.execute("SELECT * FROM quizzes WHERE id=? AND user_id=?",
                   (quiz_id, session["user_id"])).fetchone()
    if not q:
        return jsonify({"erro": "Quiz não encontrado"}), 404
    gabarito = json.loads(q["gabarito"] or "{}")
    if not gabarito:
        return jsonify({"erro": "Defina o gabarito do quiz antes de escanear."}), 400
    foto = request.files.get("foto")
    if not foto:
        return jsonify({"erro": "Envie a foto."}), 400
    if _contar_provas_da_conta(db, session["user_id"]) >= MAX_PROVAS_POR_CONTA:
        return jsonify({"erro": f"Esta conta chegou ao limite de {MAX_PROVAS_POR_CONTA} provas "
                                "guardadas. Apague correções antigas para liberar espaço."}), 400
    layout = json.loads(q["layout"])
    folha_esperada = layout.get("folha_id") or layout.get("quiz_id")
    try:
        r = processar_scan(foto.read(), layout, gabarito, folha_esperada=folha_esperada)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 422

    cur = db.execute("""INSERT INTO scans
        (quiz_id, respostas, acertos, erros, brancos, multimarcadas, nota,
         debug_img, nome_img, caixas_img, aluno_id, versao)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (quiz_id, json.dumps(r["detalhe"]), r["acertos"], r["erros"], r["brancos"],
         r["multimarcadas"], r["nota"], so_base64(r["debug_img"]), so_base64(r["nome_img"]),
         json.dumps(r.get("caixas_img") or []),
         r["aluno_id"], r["versao"]))
    db.commit()
    r["scan_id"] = cur.lastrowid
    return jsonify(r)


@app.route("/api/quizzes/<int:quiz_id>/scans", methods=["GET"])
@login_required
def api_scans(quiz_id):
    db = get_db()
    q = db.execute("SELECT id FROM quizzes WHERE id=? AND user_id=?",
                   (quiz_id, session["user_id"])).fetchone()
    if not q:
        return jsonify({"erro": "Quiz não encontrado"}), 404
    rows = db.execute("""SELECT id, acertos, erros, brancos, multimarcadas, nota,
                         nome_img, aluno_id, versao, criado_em
                         FROM scans WHERE quiz_id=? ORDER BY id DESC""",
                      (quiz_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["criado_em"] = str(d["criado_em"])[:19]
        out.append(d)
    return jsonify(out)


@app.route("/api/quizzes/<int:quiz_id>/exportar.csv", methods=["GET"])
@login_required
def api_exportar_csv(quiz_id):
    """Planilha para abrir no Excel: uma linha por prova corrigida."""
    import csv
    db = get_db()
    q = db.execute("SELECT * FROM quizzes WHERE id=? AND user_id=?",
                   (quiz_id, session["user_id"])).fetchone()
    if not q:
        return jsonify({"erro": "Correção não encontrada"}), 404
    rows = db.execute("""SELECT id, acertos, erros, brancos, multimarcadas, nota,
                         aluno_id, versao, criado_em
                         FROM scans WHERE quiz_id=? ORDER BY id""", (quiz_id,)).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")  # ponto-e-vírgula: Excel em PT-BR
    w.writerow(["#", "Matricula", "Versao", "Nota", "Acertos", "Erros",
                "Brancos", "Multimarcadas", "Data"])
    def celula(v):
        # Texto começando com = + - @ o Excel executa como FÓRMULA. Um apóstrofo
        # na frente faz ele mostrar o texto como texto.
        v = "" if v is None else str(v)
        return "'" + v if v[:1] in ("=", "+", "-", "@") else v
    for i, r in enumerate(rows, 1):
        w.writerow([i, celula(r["aluno_id"]), celula(r["versao"]),
                    str(r["nota"]).replace(".", ","),  # vírgula decimal PT-BR
                    r["acertos"], r["erros"], r["brancos"], r["multimarcadas"],
                    str(r["criado_em"])[:19]])
    conteudo = "\ufeff" + buf.getvalue()  # BOM para o Excel ler acentos
    from flask import Response
    nome = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in q["nome"])[:40]
    return Response(conteudo, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{nome}.csv"'})


@app.route("/api/quizzes/<int:quiz_id>/caixas", methods=["GET"])
@login_required
def api_caixas_disponiveis(quiz_id):
    """Rótulos das caixas de cabeçalho desta correção (Nome, Turma, Nº...)."""
    db = get_db()
    q = db.execute("SELECT layout FROM quizzes WHERE id=? AND user_id=?",
                   (quiz_id, session["user_id"])).fetchone()
    if not q:
        return jsonify({"erro": "Correção não encontrada"}), 404
    layout = json.loads(q["layout"])
    caixas = [c.get("rotulo", "") for c in layout.get("caixas", [])]
    if not caixas and "campo_nome" in layout:
        caixas = ["Nome"]
    return jsonify({"caixas": caixas})


def so_base64(valor):
    """Devolve só a parte base64 da imagem.

    O app manda as fotos como 'data:image/jpeg;base64,XXXX' (formato que o
    navegador usa direto no <img>). O gerador de PDF precisa apenas do XXXX —
    com o prefixo, a decodificação falha e a imagem some do relatório sem
    nenhum aviso. Aceita os dois formatos, para funcionar também com as
    correções que já foram sincronizadas antes desta correção."""
    if not valor:
        return None
    txt = valor.strip()
    if txt.startswith("data:"):
        virgula = txt.find(",")
        if virgula == -1:
            return None
        txt = txt[virgula + 1:]
    return txt or None


def _parece_recortada(img_b64, layout):
    """A foto guardada já é o recorte das bolinhas, ou é a folha inteira?

    Compara a proporção (altura ÷ largura) da imagem com a da folha completa.
    Se bater, é a folha inteira e ainda precisa ser recortada."""
    dados = so_base64(img_b64)
    if not dados:
        return True          # sem imagem: não há o que recortar
    try:
        from PIL import Image as PILImage
        im = PILImage.open(io.BytesIO(base64.b64decode(dados)))
        w, h = im.size
        if not w or not h:
            return True
        prop_img = h / float(w)
        prop_folha = layout["page_height_pt"] / float(layout["page_width_pt"])

        # Calcula também a proporção que o RECORTE teria nesta folha e fica com
        # o formato mais parecido. Comparar só com a folha inteira não bastava:
        # numa prova de 10 questões o recorte tem proporção semelhante à dela.
        xs, ys = [], []
        for alts in layout.get("questoes", {}).values():
            for pos in alts.values():
                xs.append(pos["x"])
                ys.append(pos["y"])
        if not xs:
            return abs(prop_img - prop_folha) / prop_folha > 0.10
        larg = (min(1.0, max(xs) + 0.03) - max(0.0, min(xs) - 0.085)) * layout["page_width_pt"]
        alt = (min(1.0, max(ys) + 0.025) - max(0.0, min(ys) - 0.025)) * layout["page_height_pt"]
        if larg <= 0 or alt <= 0:
            return abs(prop_img - prop_folha) / prop_folha > 0.10
        prop_recorte = alt / larg
        return abs(prop_img - prop_recorte) < abs(prop_img - prop_folha)
    except Exception:
        return False


def recortar_area_util(img_b64, layout):
    """Recorta a foto da folha na região que interessa: cabeçalho + bolinhas.

    A folha inteira tem muito espaço vazio, e no relatório ela fica pequena e
    embaçada. Concentrando os mesmos pixels na área útil, a imagem sai bem mais
    nítida no papel — é o que o ZipGrade faz nos relatórios deles."""
    dados = so_base64(img_b64)
    if not dados:
        return None
    try:
        from PIL import Image as PILImage
        im = PILImage.open(io.BytesIO(base64.b64decode(dados)))
        W, H = im.size

        # Recorta SÓ a área das bolinhas. O cabeçalho já aparece recortado no
        # topo do relatório, então incluí-lo aqui só repetiria informação e
        # deixaria a imagem alta e estreita. Concentrando os pixels nas
        # bolinhas, o professor enxerga bem o que o aluno marcou.
        xs, ys = [], []
        for alts in layout.get("questoes", {}).values():
            for pos in alts.values():
                xs.append(pos["x"])
                ys.append(pos["y"])
        if not xs or not ys:
            return None

        # Folga maior à esquerda: o número da questão fica antes da primeira
        # bolinha e ficaria cortado com margem simétrica.
        folga_esq = 0.085
        folga_dir = 0.03
        folga_y = 0.025
        x0 = max(0.0, min(xs) - folga_esq)
        x1 = min(1.0, max(xs) + folga_dir)
        # y do layout cresce para cima; a imagem cresce para baixo
        topo = min(1.0, max(ys) + folga_y)
        base = max(0.0, min(ys) - folga_y)

        cx0, cx1 = int(x0 * W), int(x1 * W)
        cy0, cy1 = int((1 - topo) * H), int((1 - base) * H)
        if cx1 - cx0 < 40 or cy1 - cy0 < 40:
            return None
        rec = im.crop((cx0, cy0, cx1, cy1))
        buf = io.BytesIO()
        rec.convert("RGB").save(buf, format="JPEG", quality=88)
        return ImageReader(io.BytesIO(buf.getvalue()))
    except Exception:
        return None


def desenhar_relatorio_aluno(c, quiz_nome, scan, layout):
    """Desenha UMA página de relatório individual do aluno."""
    margem = 15 * mm
    y = PAGE_H - margem

    # cabeçalho
    c.setFont("Helvetica-Bold", 14)
    c.drawString(margem, y, "Relatório do Aluno")
    y -= 5.5 * mm
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(margem, y, quiz_nome)
    c.setFillColorRGB(0, 0, 0)
    y -= 8 * mm

    # recorte(s) do cabeçalho preenchido pelo aluno
    try:
        recortes = json.loads(scan["caixas_img"] or "[]")
    except Exception:
        recortes = []
    if not recortes and scan["nome_img"]:
        recortes = [{"rotulo": "Nome", "img": scan["nome_img"]}]

    x_cursor = margem
    altura_max = 0
    for rec in recortes[:3]:
        if not rec.get("img"):
            continue
        try:
            img = ImageReader(io.BytesIO(base64.b64decode(so_base64(rec["img"]))))
            iw, ih = img.getSize()
            alt = 11 * mm
            larg = min(alt * (iw / ih), 75 * mm)
            if x_cursor + larg > PAGE_W - margem:
                break
            c.setFont("Helvetica", 6.5)
            c.setFillColorRGB(0.5, 0.5, 0.5)
            c.drawString(x_cursor, y + 1 * mm, rec.get("rotulo", "").upper())
            c.setFillColorRGB(0, 0, 0)
            c.drawImage(img, x_cursor, y - alt, larg, alt)
            x_cursor += larg + 5 * mm
            altura_max = max(altura_max, alt)
        except Exception:
            pass
    y -= (altura_max + 8 * mm) if altura_max else 4 * mm

    detalhe = json.loads(scan["respostas"] or "[]")
    pontos_total = sum(float(d.get("pontos", 1)) for d in detalhe)
    pct = (scan["nota"] / pontos_total * 100) if pontos_total else 0

    # quadro-resumo à esquerda
    box_w = 78 * mm
    box_h = 34 * mm
    box_y = y - box_h
    c.setStrokeColorRGB(0.8, 0.8, 0.8)
    c.setLineWidth(0.8)
    c.rect(margem, box_y, box_w, box_h, fill=0, stroke=1)
    linhas = [
        ("Pontos obtidos", f"{scan['nota']:.1f}".replace(".", ",")),
        ("Pontos possíveis", f"{pontos_total:.1f}".replace(".", ",")),
        ("Percentual", f"{pct:.1f}%".replace(".", ",")),
        ("Acertos / Erros", f"{scan['acertos']} / {scan['erros']}"),
        ("Brancos / Multi", f"{scan['brancos']} / {scan['multimarcadas']}"),
    ]
    ly = box_y + box_h - 6 * mm
    for rot, val in linhas:
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.35, 0.35, 0.35)
        c.drawString(margem + 3 * mm, ly, rot)
        c.setFont("Helvetica-Bold", 8.5)
        c.setFillColorRGB(0, 0, 0)
        c.drawRightString(margem + box_w - 3 * mm, ly, val)
        ly -= 6 * mm

    # nota grande à direita do quadro
    c.setFont("Helvetica-Bold", 34)
    c.setFillColorRGB(0.15, 0.39, 0.92)
    c.drawRightString(PAGE_W - margem, box_y + box_h - 14 * mm,
                      f"{scan['nota']:.1f}".replace(".", ","))
    c.setFont("Helvetica", 8)
    c.setFillColorRGB(0.5, 0.5, 0.5)
    c.drawRightString(PAGE_W - margem, box_y + box_h - 20 * mm,
                      f"de {pontos_total:.1f}".replace(".", ",") + " pontos")
    c.setFillColorRGB(0, 0, 0)
    y = box_y - 8 * mm

    # foto da folha corrigida (esquerda) e tabela de questões (direita)
    col_dir_x = PAGE_W / 2 + 4 * mm
    y_tabela = y

    if scan["debug_img"]:
        try:
            # A imagem já vem recortada nas capturas novas. Em vez de confiar
            # só no aviso gravado (que falta nas provas antigas), conferimos o
            # formato da própria foto: se ela ainda tem a proporção da folha
            # inteira, recortamos; se não, já está recortada e recortar de novo
            # deixaria só um punhado de bolinhas visíveis.
            ja_recortada = False
            try:
                ja_recortada = bool(scan["img_recortada"])
            except Exception:
                pass
            if not ja_recortada:
                ja_recortada = _parece_recortada(scan["debug_img"], layout)
            img = None if ja_recortada else recortar_area_util(scan["debug_img"], layout)
            if img is None:
                img = ImageReader(io.BytesIO(base64.b64decode(so_base64(scan["debug_img"]))))
            iw, ih = img.getSize()
            larg = (PAGE_W / 2) - margem - 6 * mm
            alt = larg * (ih / iw)
            disp = y - margem
            if alt > disp:
                alt = disp
                larg = alt * (iw / ih)
            c.drawImage(img, margem, y - alt, larg, alt)
        except Exception:
            pass

    # tabela: questão | correta | marcou | pontos | status
    c.setFont("Helvetica-Bold", 7)
    c.setFillColorRGB(0.35, 0.35, 0.35)
    cols = [0, 10 * mm, 24 * mm, 38 * mm, 52 * mm]
    heads = ["#", "Certa", "Marcou", "Pts", ""]
    for i, h in enumerate(heads):
        c.drawString(col_dir_x + cols[i], y_tabela, h)
    c.setFillColorRGB(0, 0, 0)
    y_tabela -= 3.2 * mm
    c.setStrokeColorRGB(0.85, 0.85, 0.85)
    c.setLineWidth(0.4)
    c.line(col_dir_x, y_tabela + 1 * mm, PAGE_W - margem, y_tabela + 1 * mm)
    y_tabela -= 3.4 * mm

    simbolo = {"certo": "OK", "errado": "X", "branco": "-",
               "multimarcada": "!", "anulada": "AN"}
    cor = {"certo": (0.09, 0.64, 0.29), "errado": (0.86, 0.15, 0.15),
           "branco": (0.6, 0.6, 0.6), "multimarcada": (0.92, 0.35, 0.05),
           "anulada": (0.79, 0.54, 0.02)}

    for d in sorted(detalhe, key=lambda x: int(x["questao"])):
        if y_tabela < margem:
            break
        st = d.get("status", "")
        c.setFont("Helvetica", 7.2)
        c.drawString(col_dir_x + cols[0], y_tabela, str(d["questao"]))
        certa = "anul." if d.get("anulada") else ",".join(d.get("corretas", []) or ["-"])
        c.drawString(col_dir_x + cols[1], y_tabela, certa)
        marcou = ",".join(d.get("marcadas", [])) or "-"
        c.drawString(col_dir_x + cols[2], y_tabela, marcou[:7])
        ganhou = d.get("pontos", 1) if st in ("certo", "anulada") else 0
        c.drawString(col_dir_x + cols[3], y_tabela,
                     f"{float(ganhou):g}".replace(".", ","))
        c.setFillColorRGB(*cor.get(st, (0, 0, 0)))
        c.setFont("Helvetica-Bold", 7.2)
        c.drawString(col_dir_x + cols[4], y_tabela, simbolo.get(st, ""))
        c.setFillColorRGB(0, 0, 0)
        y_tabela -= 4.2 * mm

    c.setFont("Helvetica", 6.5)
    c.setFillColorRGB(0.6, 0.6, 0.6)
    c.drawString(margem, margem - 4 * mm, "Gerado pelo GabaritoApp")
    c.setFillColorRGB(0, 0, 0)


def _buscar_quiz_e_scans(quiz_id, scan_id=None):
    db = get_db()
    q = db.execute("SELECT * FROM quizzes WHERE id=? AND user_id=?",
                   (quiz_id, session["user_id"])).fetchone()
    if not q:
        return None, None
    if scan_id is not None:
        rows = db.execute("SELECT * FROM scans WHERE id=? AND quiz_id=?",
                          (scan_id, quiz_id)).fetchall()
    else:
        rows = db.execute("SELECT * FROM scans WHERE quiz_id=? ORDER BY id", (quiz_id,)).fetchall()
    return q, rows


@app.route("/api/quizzes/<int:quiz_id>/relatorios.pdf", methods=["GET"])
@login_required
def api_relatorios_pdf(quiz_id):
    """Relatório individual: uma página por aluno. ?scan=<id> gera só de um."""
    scan_id = request.args.get("scan", type=int)
    q, rows = _buscar_quiz_e_scans(quiz_id, scan_id)
    if q is None:
        return jsonify({"erro": "Correção não encontrada"}), 404
    if not rows:
        return jsonify({"erro": "Nenhuma prova corrigida ainda."}), 400

    layout = json.loads(q["layout"])
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    for i, s in enumerate(rows):
        if i > 0:
            c.showPage()
        desenhar_relatorio_aluno(c, q["nome"], s, layout)
    c.save()

    from flask import Response
    nome = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in q["nome"])[:40]
    sufixo = f"_aluno{scan_id}" if scan_id else "_todos"
    return Response(buf.getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="relatorio_{nome}{sufixo}.pdf"'})


@app.route("/api/quizzes/<int:quiz_id>/conferencia.pdf", methods=["GET"])
@login_required
def api_conferencia_pdf(quiz_id):
    """PDF de conferência: recortes escolhidos (Nome, Turma, Nº...) + nota.
    Parâmetro ?caixas=0,1,2 escolhe quais colunas mostrar (padrão: só a 1ª)."""
    db = get_db()
    q = db.execute("SELECT * FROM quizzes WHERE id=? AND user_id=?",
                   (quiz_id, session["user_id"])).fetchone()
    if not q:
        return jsonify({"erro": "Correção não encontrada"}), 404

    layout = json.loads(q["layout"])
    rotulos = [c.get("rotulo", "") for c in layout.get("caixas", [])] or ["Nome"]

    # quais caixas mostrar
    param = request.args.get("caixas", "0")
    try:
        escolhidas = [int(x) for x in param.split(",") if x.strip() != ""]
    except ValueError:
        escolhidas = [0]
    escolhidas = [i for i in escolhidas if 0 <= i < len(rotulos)] or [0]

    rows = db.execute("""SELECT id, nota, acertos, erros, brancos, multimarcadas,
                         nome_img, caixas_img, aluno_id FROM scans WHERE quiz_id=? ORDER BY id""",
                      (quiz_id,)).fetchall()

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    margem = 15 * mm
    y = PAGE_H - margem

    c.setFont("Helvetica-Bold", 13)
    c.drawString(margem, y, f"Conferência — {q['nome']}")
    y -= 6 * mm
    c.setFont("Helvetica", 8.5)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    cols = ", ".join(rotulos[i] for i in escolhidas)
    c.drawString(margem, y, f"{len(rows)} provas · colunas: {cols} · GabaritoApp")
    c.setFillColorRGB(0, 0, 0)
    y -= 8 * mm

    # largura disponível para os recortes (deixa espaço para nº e nota)
    x_ini = margem + 9 * mm
    x_fim = PAGE_W - margem - 24 * mm
    larg_total = x_fim - x_ini
    larg_col = larg_total / len(escolhidas)
    linha_h = 16 * mm

    # cabeçalho das colunas
    c.setFont("Helvetica-Bold", 7)
    c.setFillColorRGB(0.45, 0.45, 0.45)
    for j, idx in enumerate(escolhidas):
        c.drawString(x_ini + j * larg_col, y, rotulos[idx].upper())
    c.drawRightString(PAGE_W - margem, y, "NOTA")
    c.setFillColorRGB(0, 0, 0)
    y -= 4 * mm

    for i, r in enumerate(rows, 1):
        if y - linha_h < margem:
            c.showPage()
            y = PAGE_H - margem
        c.setFont("Helvetica-Bold", 10)
        c.drawString(margem, y - 8 * mm, f"{i:02d}")

        # recortes salvos (novo) ou fallback para nome_img (provas antigas)
        try:
            recortes = json.loads(r["caixas_img"] or "[]")
        except Exception:
            recortes = []
        if not recortes and r["nome_img"]:
            recortes = [{"rotulo": "Nome", "img": r["nome_img"]}]

        for j, idx in enumerate(escolhidas):
            img_b64 = recortes[idx]["img"] if idx < len(recortes) else None
            if not img_b64:
                continue
            try:
                img = ImageReader(io.BytesIO(base64.b64decode(so_base64(img_b64))))
                iw, ih = img.getSize()
                alt = 9 * mm
                larg = min(alt * (iw / ih), larg_col - 3 * mm)
                c.drawImage(img, x_ini + j * larg_col, y - 10 * mm, larg, alt)
            except Exception:
                pass

        c.setFont("Helvetica-Bold", 15)
        c.drawRightString(PAGE_W - margem, y - 8 * mm, f"{r['nota']:.1f}".replace(".", ","))
        c.setFont("Helvetica", 7)
        c.setFillColorRGB(0.5, 0.5, 0.5)
        det = f"{r['acertos']}✓ {r['erros']}✗"
        if r["brancos"]:
            det += f" {r['brancos']}br"
        if r["multimarcadas"]:
            det += f" {r['multimarcadas']}mm"
        c.drawRightString(PAGE_W - margem, y - 12 * mm, det)
        c.setFillColorRGB(0, 0, 0)
        c.setStrokeColorRGB(0.85, 0.85, 0.85)
        c.setLineWidth(0.5)
        c.line(margem, y - linha_h + 2 * mm, PAGE_W - margem, y - linha_h + 2 * mm)
        y -= linha_h

    c.save()
    from flask import Response
    nome = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in q["nome"])[:40]
    return Response(buf.getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="conferencia_{nome}.pdf"'})


@app.route("/api/scans/<int:scan_id>", methods=["GET"])
@login_required
def api_scan_detalhe(scan_id):
    db = get_db()
    s = db.execute("""SELECT s.* FROM scans s JOIN quizzes q ON q.id=s.quiz_id
                      WHERE s.id=? AND q.user_id=?""",
                   (scan_id, session["user_id"])).fetchone()
    if not s:
        return jsonify({"erro": "Scan não encontrado"}), 404
    return jsonify({
        "id": s["id"], "acertos": s["acertos"], "erros": s["erros"],
        "brancos": s["brancos"], "multimarcadas": s["multimarcadas"],
        "nota": s["nota"], "detalhe": json.loads(s["respostas"]),
        "debug_img": s["debug_img"], "nome_img": s["nome_img"],
        "aluno_id": s["aluno_id"], "versao": s["versao"],
        "criado_em": str(s["criado_em"])[:19],
    })


@app.route("/api/scans/<int:scan_id>", methods=["DELETE"])
@login_required
def api_scan_deletar(scan_id):
    db = get_db()
    s = db.execute("""SELECT s.id FROM scans s JOIN quizzes q ON q.id=s.quiz_id
                      WHERE s.id=? AND q.user_id=?""",
                   (scan_id, session["user_id"])).fetchone()
    if not s:
        return jsonify({"erro": "Scan não encontrado"}), 404
    db.execute("DELETE FROM scans WHERE id=?", (scan_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/quizzes/<int:quiz_id>/estatisticas", methods=["GET"])
@login_required
def api_estatisticas(quiz_id):
    db = get_db()
    q = db.execute("SELECT * FROM quizzes WHERE id=? AND user_id=?",
                   (quiz_id, session["user_id"])).fetchone()
    if not q:
        return jsonify({"erro": "Quiz não encontrado"}), 404
    scans = db.execute("SELECT respostas, nota FROM scans WHERE quiz_id=?", (quiz_id,)).fetchall()
    if not scans:
        return jsonify({"n_scans": 0})

    gab_raw = json.loads(q["gabarito"] or "{}")
    # Com versões, as respostas certas variam por aluno; usamos a primeira versão
    # apenas como referência de exibição (os status por questão já vêm do scan).
    gabarito, _ = gabarito_da_versao(gab_raw, None)
    layout = json.loads(q["layout"])
    labels_map = layout.get("labels_questoes", {})

    stats = {}
    notas = []
    for s in scans:
        notas.append(s["nota"])
        for d in json.loads(s["respostas"]):
            qn = d["questao"]
            if qn not in stats:
                labels = labels_map.get(qn, "ABCDE")
                stats[qn] = {"certo": 0, "errado": 0, "branco": 0,
                             "multimarcada": 0, "anulada": 0,
                             "alternativas": {L: 0 for L in labels}}
            stats[qn][d["status"]] = stats[qn].get(d["status"], 0) + 1
            for alt in d.get("marcadas", []):
                if alt in stats[qn]["alternativas"]:
                    stats[qn]["alternativas"][alt] += 1

    n = len(scans)
    resultado = []
    for qn in sorted(stats, key=int):
        st = stats[qn]
        g = gabarito.get(qn, {})
        resultado.append({
            "questao": qn,
            "corretas": g.get("corretas", []),
            "anulada": g.get("anulada", False),
            "pct_acerto": round((st["certo"] + st["anulada"]) / n * 100, 1),
            "pct_erro": round(st["errado"] / n * 100, 1),
            "pct_branco": round(st["branco"] / n * 100, 1),
            "pct_multi": round(st["multimarcada"] / n * 100, 1),
            "alternativas": st["alternativas"],
        })
    return jsonify({
        "n_scans": n,
        "media": round(sum(notas) / n, 2),
        "maior": max(notas), "menor": min(notas),
        "questoes": resultado,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
