"""
GabaritoApp v2 — Sistema completo de correção de provas
- Cadastro/login de usuários (senha com hash)
- Quizzes com localizador via QR code
- Escaneamento múltiplo com detecção de multimarcação
- Recorte da foto do nome do aluno
- Estatísticas por questão e por alternativa
"""

import base64
import io
import json
import os
import sqlite3
from datetime import datetime
from functools import wraps

import cv2
import numpy as np
import qrcode
from flask import (Flask, g, jsonify, redirect, render_template, request,
                   session, url_for)
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

DB_PATH = os.environ.get("DB_PATH", "gabarito.db")

# ══════════════════════════════════════════════
# BANCO DE DADOS
# ══════════════════════════════════════════════

# Se a variável DATABASE_URL existir (Render PostgreSQL), usa Postgres.
# Senão, usa SQLite local (bom para testes).
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USA_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

if USA_POSTGRES:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    # Render fornece postgres:// mas o psycopg2 prefere postgresql://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


class CursorWrapper:
    """Faz o cursor do PostgreSQL se comportar como o do SQLite."""
    def __init__(self, cur, lastrowid=None):
        self._cur = cur
        self.lastrowid = lastrowid

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class PgConnWrapper:
    """Faz a conexão PostgreSQL aceitar a mesma sintaxe do SQLite (placeholders '?')."""
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


def init_db():
    if USA_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            nome TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            senha_hash TEXT NOT NULL,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS quizzes (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            nome TEXT NOT NULL,
            n_questoes INTEGER NOT NULL,
            gabarito TEXT DEFAULT '{}',
            layout TEXT NOT NULL,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS scans (
            id SERIAL PRIMARY KEY,
            quiz_id INTEGER NOT NULL REFERENCES quizzes(id),
            respostas TEXT NOT NULL,
            acertos INTEGER, erros INTEGER, brancos INTEGER, multimarcadas INTEGER,
            nota REAL,
            debug_img TEXT,
            nome_img TEXT,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        conn.commit()
        conn.close()
    else:
        db = sqlite3.connect(DB_PATH)
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            senha_hash TEXT NOT NULL,
            criado_em TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS quizzes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            nome TEXT NOT NULL,
            n_questoes INTEGER NOT NULL,
            gabarito TEXT DEFAULT '{}',
            layout TEXT NOT NULL,
            criado_em TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id INTEGER NOT NULL REFERENCES quizzes(id),
            respostas TEXT NOT NULL,
            acertos INTEGER, erros INTEGER, brancos INTEGER, multimarcadas INTEGER,
            nota REAL,
            debug_img TEXT,
            nome_img TEXT,
            criado_em TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        db.commit()
        db.close()


init_db()


def linha_para_dict(row):
    """Converte uma linha do banco em dict serializável (datas viram texto)."""
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.strftime("%Y-%m-%d %H:%M:%S")
    return d


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"erro": "Não autenticado"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return wrapper


# ══════════════════════════════════════════════
# GERADOR DE PDF (com QR localizador e campo de nome)
# ══════════════════════════════════════════════

PAGE_W, PAGE_H = A4
MARKER_SIZE = 12 * mm
MARGIN = 15 * mm
ALTERNATIVAS = ["A", "B", "C", "D", "E"]
BUBBLE_RADIUS = 3.2 * mm
COL_QUESTAO_W = 10 * mm
BUBBLE_SPACING = 9 * mm


def gerar_pdf_gabarito(n_questoes: int, quiz_id: int, quiz_nome: str) -> tuple[bytes, dict]:
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    layout = {"questoes": {}, "quiz_id": quiz_id}

    # marcadores nos 4 cantos
    positions = [
        (MARGIN, PAGE_H - MARGIN - MARKER_SIZE),
        (PAGE_W - MARGIN - MARKER_SIZE, PAGE_H - MARGIN - MARKER_SIZE),
        (MARGIN, MARGIN),
        (PAGE_W - MARGIN - MARKER_SIZE, MARGIN),
    ]
    c.setFillColorRGB(0, 0, 0)
    for x, y in positions:
        c.rect(x, y, MARKER_SIZE, MARKER_SIZE, fill=1, stroke=0)

    # QR code com o ID do quiz (localizador)
    qr_img = qrcode.make(f"GABARITO:{quiz_id}", box_size=4, border=1)
    qr_buf = io.BytesIO()
    qr_img.save(qr_buf, format="PNG")
    qr_buf.seek(0)
    qr_size = 18 * mm
    qr_x = PAGE_W / 2 - qr_size / 2
    qr_y = PAGE_H - MARGIN - qr_size
    c.drawImage(ImageReader(qr_buf), qr_x, qr_y, qr_size, qr_size)
    layout["qr"] = {
        "x": qr_x / PAGE_W, "y": qr_y / PAGE_H,
        "w": qr_size / PAGE_W, "h": qr_size / PAGE_H,
    }

    # cabeçalho
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(PAGE_W / 2, qr_y - 5 * mm, f"{quiz_nome}  (Quiz #{quiz_id})")

    # campo do nome (com caixa desenhada — será recortado na correção)
    nome_y = qr_y - 16 * mm
    nome_x = MARGIN + MARKER_SIZE + 3 * mm
    nome_w = PAGE_W - 2 * (MARGIN + MARKER_SIZE + 3 * mm)
    nome_h = 9 * mm
    c.setFont("Helvetica", 8)
    c.drawString(nome_x, nome_y + nome_h + 1.5 * mm, "NOME DO ALUNO (letra de forma):")
    c.setLineWidth(1.2)
    c.rect(nome_x, nome_y, nome_w, nome_h, fill=0, stroke=1)
    layout["campo_nome"] = {
        "x": nome_x / PAGE_W, "y": nome_y / PAGE_H,
        "w": nome_w / PAGE_W, "h": nome_h / PAGE_H,
    }

    # linha de turma/data
    c.setFont("Helvetica", 9)
    c.drawString(nome_x, nome_y - 6 * mm, "Turma: ____________   Nº: _______   Data: ___/___/______")

    # grade de bolinhas
    top_y = nome_y - 16 * mm
    n_colunas = 2 if n_questoes > 10 else 1
    questoes_por_coluna = (n_questoes + n_colunas - 1) // n_colunas
    col_width = (PAGE_W - 2 * MARGIN) / n_colunas
    row_height = 9 * mm

    for col in range(n_colunas):
        x_base = MARGIN + col * col_width + 8 * mm
        c.setFont("Helvetica-Bold", 8)
        for i, alt in enumerate(ALTERNATIVAS):
            cx = x_base + COL_QUESTAO_W + i * BUBBLE_SPACING
            c.drawCentredString(cx, top_y, alt)
        for row in range(questoes_por_coluna):
            q_num = col * questoes_por_coluna + row + 1
            if q_num > n_questoes:
                break
            y = top_y - 6 * mm - row * row_height
            c.setFont("Helvetica", 9)
            c.drawString(x_base - 6 * mm, y - 1.5 * mm, f"{q_num:02d}")
            layout["questoes"][str(q_num)] = {}
            for i, alt in enumerate(ALTERNATIVAS):
                cx = x_base + COL_QUESTAO_W + i * BUBBLE_SPACING
                c.circle(cx, y, BUBBLE_RADIUS, fill=0, stroke=1)
                layout["questoes"][str(q_num)][alt] = {"x": cx / PAGE_W, "y": y / PAGE_H}

    c.save()
    layout.update({
        "page_width_pt": PAGE_W,
        "page_height_pt": PAGE_H,
        "marker_size_pt": MARKER_SIZE,
        "bubble_radius_pt": BUBBLE_RADIUS,
        "markers": [{"x": x / PAGE_W, "y": y / PAGE_H} for x, y in positions],
    })
    return buf.getvalue(), layout


# ══════════════════════════════════════════════
# ALGORITMO OMR
# ══════════════════════════════════════════════

def encontrar_marcadores(cinza):
    _, binaria = cv2.threshold(cinza, 100, 255, cv2.THRESH_BINARY_INV)
    contornos, _ = cv2.findContours(binaria, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h_img, w_img = cinza.shape
    area_min = (w_img * 0.02) * (h_img * 0.02) * 0.3
    candidatos = []
    for c in contornos:
        x, y, w, h = cv2.boundingRect(c)
        if w * h > area_min and 0.7 < w / float(h) < 1.3:
            candidatos.append((x, y, w, h))
    if len(candidatos) < 4:
        raise ValueError(f"Só encontrei {len(candidatos)} marcadores de canto. Melhore a iluminação/enquadramento.")
    cantos = [(0, 0), (w_img, 0), (0, h_img), (w_img, h_img)]
    marcadores, restantes = [], candidatos.copy()
    for canto in cantos:
        melhor = min(restantes, key=lambda c: (c[0] - canto[0]) ** 2 + (c[1] - canto[1]) ** 2)
        marcadores.append((melhor[0] + melhor[2] / 2, melhor[1] + melhor[3] / 2))
        restantes.remove(melhor)
    return marcadores


def corrigir_perspectiva(cinza, marcadores, W, H, layout):
    ms, pw, ph = layout["marker_size_pt"], layout["page_width_pt"], layout["page_height_pt"]
    origem = np.array(marcadores, dtype="float32")
    destino = np.array([
        [(m["x"] + ms / (2 * pw)) * W, (1 - m["y"] - ms / (2 * ph)) * H]
        for m in layout["markers"]
    ], dtype="float32")
    matriz = cv2.getPerspectiveTransform(origem, destino)
    return cv2.warpPerspective(cinza, matriz, (W, H))


def ler_qr(img):
    """Tenta ler o QR code do quiz na imagem. Retorna quiz_id ou None."""
    detector = cv2.QRCodeDetector()
    data, _, _ = detector.detectAndDecode(img)
    if data and data.startswith("GABARITO:"):
        try:
            return int(data.split(":")[1])
        except (ValueError, IndexError):
            return None
    return None


LIMIAR_MARCACAO = 0.35


def ler_bolinhas(corrigida, layout, W, H):
    """Retorna {q: {"marcadas": [...], "resposta": 'A'|None, "multi": bool}}"""
    raio_px = int(layout["bubble_radius_pt"] / layout["page_width_pt"] * W * 1.3)
    resultados = {}
    for q_num, alternativas in layout["questoes"].items():
        intensidades = {}
        for alt, pos in alternativas.items():
            cx, cy = int(pos["x"] * W), int((1 - pos["y"]) * H)
            mask = np.zeros(corrigida.shape, dtype="uint8")
            cv2.circle(mask, (cx, cy), raio_px, 255, -1)
            media = cv2.mean(corrigida, mask=mask)[0]
            intensidades[alt] = 1 - (media / 255)
        marcadas = [a for a, v in intensidades.items() if v >= LIMIAR_MARCACAO]
        resultados[q_num] = {
            "marcadas": marcadas,
            "resposta": marcadas[0] if len(marcadas) == 1 else None,
            "multi": len(marcadas) > 1,
        }
    return resultados


def recortar_nome(corrigida, layout, W, H):
    """Recorta a região do campo de nome e retorna como JPEG base64."""
    cn = layout.get("campo_nome")
    if not cn:
        return None
    x1 = int(cn["x"] * W)
    y1 = int((1 - cn["y"] - cn["h"]) * H)
    x2 = int((cn["x"] + cn["w"]) * W)
    y2 = int((1 - cn["y"]) * H)
    pad = 4
    crop = corrigida[max(0, y1 - pad):y2 + pad, max(0, x1 - pad):x2 + pad]
    _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return base64.b64encode(buf).decode()


def processar_scan(foto_bytes, layout, gabarito, verificar_quiz_id=None):
    arr = np.frombuffer(foto_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Não consegui abrir a imagem enviada.")
    cinza = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # localizador: verifica se a folha pertence ao quiz
    qr_quiz_id = ler_qr(img)
    if verificar_quiz_id is not None and qr_quiz_id is not None and qr_quiz_id != verificar_quiz_id:
        raise ValueError(f"Esta folha pertence ao Quiz #{qr_quiz_id}, não ao Quiz #{verificar_quiz_id}. Verifique se pegou a folha certa.")

    marcadores = encontrar_marcadores(cinza)
    W, H = 1000, 1414
    corrigida = corrigir_perspectiva(cinza, marcadores, W, H, layout)
    leitura = ler_bolinhas(corrigida, layout, W, H)
    nome_img = recortar_nome(corrigida, layout, W, H)

    # imagem de debug
    debug = cv2.cvtColor(corrigida, cv2.COLOR_GRAY2BGR)
    raio = int(layout["bubble_radius_pt"] / layout["page_width_pt"] * W * 1.3)
    acertos = erros = brancos = multi = 0
    detalhe = []

    for q in sorted(leitura, key=lambda x: int(x)):
        info = leitura[q]
        certa = gabarito.get(q)
        if info["multi"]:
            status = "multimarcada"
            multi += 1
        elif info["resposta"] is None:
            status = "branco"
            brancos += 1
        elif certa and info["resposta"] == certa:
            status = "certo"
            acertos += 1
        else:
            status = "errado"
            erros += 1
        detalhe.append({
            "questao": q,
            "marcadas": info["marcadas"],
            "resposta": info["resposta"],
            "correta": certa,
            "status": status,
        })
        # desenha no debug
        for alt, pos in layout["questoes"][q].items():
            if alt in info["marcadas"]:
                cx, cy = int(pos["x"] * W), int((1 - pos["y"]) * H)
                if status == "certo":
                    cor = (0, 200, 0)
                elif status == "multimarcada":
                    cor = (0, 165, 255)
                else:
                    cor = (0, 0, 220)
                cv2.circle(debug, (cx, cy), raio, cor, 3)

    total = len(gabarito) if gabarito else len(leitura)
    nota = round(acertos / total * 10, 2) if total else 0
    _, dbuf = cv2.imencode(".jpg", debug, [cv2.IMWRITE_JPEG_QUALITY, 82])

    return {
        "acertos": acertos, "erros": erros, "brancos": brancos,
        "multimarcadas": multi, "total": total, "nota": nota,
        "detalhe": detalhe,
        "debug_img": base64.b64encode(dbuf).decode(),
        "nome_img": nome_img,
        "qr_detectado": qr_quiz_id,
    }


# ══════════════════════════════════════════════
# ROTAS — PÁGINAS
# ══════════════════════════════════════════════

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login_page"))


@app.route("/login")
def login_page():
    return render_template("login.html")


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", user_nome=session.get("user_nome", ""))


@app.route("/quiz/<int:quiz_id>")
@login_required
def quiz_page(quiz_id):
    db = get_db()
    quiz = db.execute("SELECT * FROM quizzes WHERE id=? AND user_id=?",
                      (quiz_id, session["user_id"])).fetchone()
    if not quiz:
        return redirect(url_for("dashboard"))
    return render_template("quiz.html", quiz_id=quiz_id, quiz_nome=quiz["nome"],
                           n_questoes=quiz["n_questoes"])


# ══════════════════════════════════════════════
# ROTAS — API AUTH
# ══════════════════════════════════════════════

@app.route("/api/registrar", methods=["POST"])
def api_registrar():
    data = request.get_json()
    nome = (data.get("nome") or "").strip()
    email = (data.get("email") or "").strip().lower()
    senha = data.get("senha") or ""
    if not nome or not email or len(senha) < 6:
        return jsonify({"erro": "Preencha nome, email e senha (mín. 6 caracteres)."}), 400
    db = get_db()
    try:
        cur = db.execute("INSERT INTO users (nome, email, senha_hash) VALUES (?,?,?)",
                         (nome, email, generate_password_hash(senha)))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"erro": "Este email já está cadastrado."}), 409
    session["user_id"] = cur.lastrowid
    session["user_nome"] = nome
    return jsonify({"ok": True})


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json()
    email = (data.get("email") or "").strip().lower()
    senha = data.get("senha") or ""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not check_password_hash(user["senha_hash"], senha):
        return jsonify({"erro": "Email ou senha incorretos."}), 401
    session["user_id"] = user["id"]
    session["user_nome"] = user["nome"]
    return jsonify({"ok": True})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


# ══════════════════════════════════════════════
# ROTAS — API QUIZZES
# ══════════════════════════════════════════════

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
        "n_scans": r["n_scans"],
        "criado_em": str(r["criado_em"])[:19],
        "tem_gabarito": r["gabarito"] != "{}",
    } for r in rows])


@app.route("/api/quizzes", methods=["POST"])
@login_required
def api_criar_quiz():
    data = request.get_json()
    nome = (data.get("nome") or "").strip()
    n = int(data.get("n_questoes", 20))
    if not nome or not 5 <= n <= 100:
        return jsonify({"erro": "Informe o nome e entre 5 e 100 questões."}), 400
    db = get_db()
    cur = db.execute("INSERT INTO quizzes (user_id, nome, n_questoes, layout) VALUES (?,?,?,?)",
                     (session["user_id"], nome, n, "{}"))
    quiz_id = cur.lastrowid
    _, layout = gerar_pdf_gabarito(n, quiz_id, nome)
    db.execute("UPDATE quizzes SET layout=? WHERE id=?", (json.dumps(layout), quiz_id))
    db.commit()
    return jsonify({"ok": True, "quiz_id": quiz_id})


@app.route("/api/quizzes/<int:quiz_id>", methods=["GET"])
@login_required
def api_quiz(quiz_id):
    db = get_db()
    q = db.execute("SELECT * FROM quizzes WHERE id=? AND user_id=?",
                   (quiz_id, session["user_id"])).fetchone()
    if not q:
        return jsonify({"erro": "Quiz não encontrado"}), 404
    return jsonify({
        "id": q["id"], "nome": q["nome"], "n_questoes": q["n_questoes"],
        "gabarito": json.loads(q["gabarito"]),
    })


@app.route("/api/quizzes/<int:quiz_id>/gabarito", methods=["PUT"])
@login_required
def api_salvar_gabarito(quiz_id):
    data = request.get_json()
    gabarito = data.get("gabarito", {})
    db = get_db()
    q = db.execute("SELECT id FROM quizzes WHERE id=? AND user_id=?",
                   (quiz_id, session["user_id"])).fetchone()
    if not q:
        return jsonify({"erro": "Quiz não encontrado"}), 404
    db.execute("UPDATE quizzes SET gabarito=? WHERE id=?", (json.dumps(gabarito), quiz_id))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/quizzes/<int:quiz_id>/pdf", methods=["GET"])
@login_required
def api_pdf(quiz_id):
    db = get_db()
    q = db.execute("SELECT * FROM quizzes WHERE id=? AND user_id=?",
                   (quiz_id, session["user_id"])).fetchone()
    if not q:
        return jsonify({"erro": "Quiz não encontrado"}), 404
    pdf_bytes, _ = gerar_pdf_gabarito(q["n_questoes"], q["id"], q["nome"])
    return jsonify({"pdf": base64.b64encode(pdf_bytes).decode()})


@app.route("/api/quizzes/<int:quiz_id>/scan", methods=["POST"])
@login_required
def api_scan(quiz_id):
    db = get_db()
    q = db.execute("SELECT * FROM quizzes WHERE id=? AND user_id=?",
                   (quiz_id, session["user_id"])).fetchone()
    if not q:
        return jsonify({"erro": "Quiz não encontrado"}), 404
    gabarito = json.loads(q["gabarito"])
    if not gabarito:
        return jsonify({"erro": "Defina o gabarito do quiz antes de escanear."}), 400
    foto = request.files.get("foto")
    if not foto:
        return jsonify({"erro": "Envie a foto."}), 400
    try:
        r = processar_scan(foto.read(), json.loads(q["layout"]), gabarito,
                           verificar_quiz_id=quiz_id)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 422

    cur = db.execute("""INSERT INTO scans
        (quiz_id, respostas, acertos, erros, brancos, multimarcadas, nota, debug_img, nome_img)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (quiz_id, json.dumps(r["detalhe"]), r["acertos"], r["erros"], r["brancos"],
         r["multimarcadas"], r["nota"], r["debug_img"], r["nome_img"]))
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
                         nome_img, criado_em FROM scans WHERE quiz_id=? ORDER BY id DESC""",
                      (quiz_id,)).fetchall()
    return jsonify([linha_para_dict(r) for r in rows])


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

    gabarito = json.loads(q["gabarito"])
    stats = {str(i): {"certo": 0, "errado": 0, "branco": 0, "multimarcada": 0,
                      "alternativas": {a: 0 for a in ALTERNATIVAS}}
             for i in range(1, q["n_questoes"] + 1)}
    notas = []
    for s in scans:
        notas.append(s["nota"])
        for d in json.loads(s["respostas"]):
            qn = d["questao"]
            if qn in stats:
                stats[qn][d["status"]] += 1
                for alt in d.get("marcadas", []):
                    stats[qn]["alternativas"][alt] += 1

    n = len(scans)
    resultado = []
    for qn in sorted(stats, key=int):
        st = stats[qn]
        resultado.append({
            "questao": qn,
            "correta": gabarito.get(qn),
            "pct_acerto": round(st["certo"] / n * 100, 1),
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
