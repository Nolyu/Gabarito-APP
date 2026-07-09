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
import os
import secrets
import sqlite3
import string
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
    CREATE TABLE IF NOT EXISTS scans (
        id {serial},
        quiz_id INTEGER NOT NULL REFERENCES quizzes(id),
        respostas TEXT NOT NULL,
        acertos INTEGER, erros INTEGER, brancos INTEGER, multimarcadas INTEGER,
        nota REAL,
        debug_img TEXT,
        nome_img TEXT,
        aluno_id TEXT,
        versao TEXT,
        criado_em {ts} DEFAULT CURRENT_TIMESTAMP
    );
    """
    if USA_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(tabelas)
        conn.commit()
        # migrações de versões antigas
        for alter in [
            "ALTER TABLE users ADD COLUMN codigo_recuperacao TEXT",
            "ALTER TABLE quizzes ADD COLUMN folha_id INTEGER",
            "ALTER TABLE quizzes ADD COLUMN gabarito_ts BIGINT DEFAULT 0",
            "ALTER TABLE scans ADD COLUMN aluno_id TEXT",
            "ALTER TABLE scans ADD COLUMN versao TEXT",
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
        ]:
            _exec_ignore(db.execute, alter)
        db.commit()
        db.close()


init_db()

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


# ══════════════════════════════════════════════
# CONSTRUÇÃO DE FOLHAS (layout + PDF)
# ══════════════════════════════════════════════

PAGE_W, PAGE_H = A4
MARGIN = 12 * mm
MARKER_SIZE = 10 * mm
CONTENT_X = 20 * mm
CONTENT_W = PAGE_W - 40 * mm
BUBBLE_R = 3.0 * mm
BUBBLE_SP = 8.5 * mm
ROW_H = 8.5 * mm
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
    bc_w = 8 * mm
    bc_h = 4 * mm
    bc_gap = 2.5 * mm
    bc_total = BC_N * bc_h + (BC_N - 1) * bc_gap
    bc_x = MARGIN
    bc_y_topo = PAGE_H / 2 + bc_total / 2
    bits = [(folha_id >> (11 - i)) & 1 for i in range(12)]
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
    caixa_h = 8 * mm
    label_h = 3.5 * mm
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
    n_col = 2 if (max_labels <= 5 and len(questoes) > 10) else 1
    col_w = CONTENT_W / n_col
    por_col = (len(questoes) + n_col - 1) // n_col

    header_y = y
    y_min_permitido = MARGIN + MARKER_SIZE + 4 * mm
    altura_necessaria = 6 * mm + por_col * ROW_H
    if header_y - altura_necessaria < y_min_permitido:
        max_rows = int((header_y - y_min_permitido - 6 * mm) / ROW_H)
        return None, (f"Muitas questões para uma página: cabem no máximo "
                      f"{max_rows * n_col} com essa configuração.")

    for col in range(n_col):
        x_base = CONTENT_X + col * col_w + 7 * mm
        for row in range(por_col):
            idx = col * por_col + row
            if idx >= len(questoes):
                break
            q = questoes[idx]
            num = str(q["numero"])
            labels = q["labels"]
            qy = header_y - 6 * mm - row * ROW_H
            layout["questoes"][num] = {}
            layout["labels_questoes"][num] = labels
            for i, L in enumerate(labels):
                cx = x_base + 8 * mm + i * BUBBLE_SP
                layout["questoes"][num][L] = fr(cx, qy)

    layout["questoes_header_y"] = header_y / PAGE_H
    layout["n_colunas"] = n_col
    layout["por_coluna"] = por_col
    return layout, None


def desenhar_folha(c, config, layout, folha_id, cinza=0.62):
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
    c.setFont("Helvetica-Bold", 11)
    loc = f"{folha_id:04d}"
    c.drawCentredString(PAGE_W / 2, layout["titulo_y"] * PAGE_H,
                        f"{config['nome']}  ·  Localizador {loc}")

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

    # caixas de cabeçalho
    c.setLineWidth(1.1)
    for cx in layout.get("caixas", []):
        x, y = cx["x"] * PAGE_W, cx["y"] * PAGE_H
        w, h = cx["w"] * PAGE_W, cx["h"] * PAGE_H
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica", 7.5)
        c.drawString(x, y + h + 1.2 * mm, cx["rotulo"].upper())
        c.setStrokeColorRGB(*tom)
        c.rect(x, y, w, h, fill=0, stroke=1)

    # matrícula
    if "id_digitos" in layout:
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 8)
        top = layout["id_top_y"] * PAGE_H
        c.drawString(CONTENT_X, top + 1 * mm, layout.get("id_rotulo", "Matrícula").upper())
        c.setFont("Helvetica", 6)
        c.setStrokeColorRGB(*tom)
        for d, nums in layout["id_digitos"].items():
            for n, pos in nums.items():
                cxp, cyp = pos["x"] * PAGE_W, pos["y"] * PAGE_H
                c.circle(cxp, cyp, DIGIT_R, fill=0, stroke=1)
                c.setFillColorRGB(*tom)
                c.drawCentredString(cxp, cyp - 1.6, n)
                c.setFillColorRGB(0, 0, 0)

    # versão
    if "versao_bolhas" in layout:
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 8)
        vy = layout["versao_y"] * PAGE_H
        primeiro_x = min(p["x"] for p in layout["versao_bolhas"].values()) * PAGE_W
        c.drawString(primeiro_x - 2 * mm, vy + 5 * mm, layout.get("versao_rotulo", "Versão").upper())
        c.setStrokeColorRGB(*tom)
        for L, pos in layout["versao_bolhas"].items():
            cxp, cyp = pos["x"] * PAGE_W, pos["y"] * PAGE_H
            c.circle(cxp, cyp, BUBBLE_R, fill=0, stroke=1)
            c.setFillColorRGB(*tom)
            c.setFont("Helvetica", 7)
            c.drawCentredString(cxp, cyp - 2, L)
            c.setFillColorRGB(0, 0, 0)

    # questões
    header_y = layout["questoes_header_y"] * PAGE_H
    c.setStrokeColorRGB(*tom)
    for num, alts in layout["questoes"].items():
        primeiro = min(alts.values(), key=lambda p: p["x"])
        qx = primeiro["x"] * PAGE_W - 8 * mm - 6 * mm
        qy = primeiro["y"] * PAGE_H
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica", 8.5)
        c.drawString(qx, qy - 1.4 * mm, f"{int(num):02d}")
        for L, pos in alts.items():
            cxp, cyp = pos["x"] * PAGE_W, pos["y"] * PAGE_H
            c.circle(cxp, cyp, BUBBLE_R, fill=0, stroke=1)
            c.setFillColorRGB(*tom)
            c.setFont("Helvetica", 7)
            c.drawCentredString(cxp, cyp - 2, L)
            c.setFillColorRGB(0, 0, 0)


def gerar_pdf_folha(config, layout, folha_id, copias=1, escuro=0):
    """copias: 1 ou 2 por página. escuro: 0 (padrão) a 3 (mais escuro)."""
    tons = [0.62, 0.45, 0.28, 0.0]
    cinza = tons[max(0, min(3, escuro))]
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    if copias == 1:
        desenhar_folha(c, config, layout, folha_id, cinza)
    else:
        escala = (PAGE_H / 2) / PAGE_H  # 0.5
        for i in range(2):
            c.saveState()
            offset_y = PAGE_H / 2 if i == 0 else 0
            offset_x = (PAGE_W - PAGE_W * escala) / 2
            c.translate(offset_x, offset_y)
            c.scale(escala, escala)
            desenhar_folha(c, config, layout, folha_id, cinza)
            c.restoreState()
        # linha de corte
        c.setDash(3, 3)
        c.setStrokeColorRGB(0.6, 0.6, 0.6)
        c.line(0, PAGE_H / 2, PAGE_W, PAGE_H / 2)
    c.save()
    return buf.getvalue()


# ══════════════════════════════════════════════
# ALGORITMO OMR
# ══════════════════════════════════════════════

LIMIAR_MARCACAO = 0.35


def encontrar_marcadores(cinza):
    _, binaria = cv2.threshold(cinza, 100, 255, cv2.THRESH_BINARY_INV)
    contornos, _ = cv2.findContours(binaria, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h_img, w_img = cinza.shape
    area_min = (w_img * 0.015) * (h_img * 0.015) * 0.3
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
    o papel ao redor. Imune a sombra suave (papel e bolinha escurecem juntos)."""
    h, w = img.shape
    if cx - raio*2 < 0 or cy - raio*2 < 0 or cx + raio*2 >= w or cy + raio*2 >= h:
        return intensidade_circulo(img, cx, cy, raio), 0.0
    mask_in = np.zeros(img.shape, dtype="uint8")
    cv2.circle(mask_in, (cx, cy), raio, 255, -1)
    brilho_miolo = cv2.mean(img, mask=mask_in)[0]
    mask_out = np.zeros(img.shape, dtype="uint8")
    cv2.circle(mask_out, (cx, cy), int(raio*2.0), 255, -1)
    cv2.circle(mask_out, (cx, cy), int(raio*1.4), 0, -1)
    brilho_papel = cv2.mean(img, mask=mask_out)[0]
    fill = 1 - (brilho_miolo / 255)
    contraste = max(0.0, (brilho_papel - brilho_miolo) / 255)
    return fill, contraste


def medir_vizinhanca(img, cx, cy, raio):
    passo = max(2, int(raio * 0.35))
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


def decidir_marcadas(medidas):
    """medidas: {alt: (fill, contraste)}. Usa contraste local (imune a sombra)."""
    contrastes = {a: m[1] for a, m in medidas.items()}
    if not contrastes:
        return []
    max_c = max(contrastes.values())
    CONTRASTE_MIN = 0.16
    if max_c < CONTRASTE_MIN:
        return []
    return [a for a, c in contrastes.items() if c >= CONTRASTE_MIN and c >= max_c * 0.55]


def ler_questoes(corrigida, layout, W, H):
    raio = int(layout["bubble_radius_pt"] / layout["page_width_pt"] * W * 0.95)
    resultados = {}
    for q_num, alts in layout["questoes"].items():
        medidas = {}
        for alt, pos in alts.items():
            cx, cy = int(pos["x"] * W), int((1 - pos["y"]) * H)
            medidas[alt] = medir_vizinhanca(corrigida, cx, cy, raio)
        marcadas = decidir_marcadas(medidas)
        resultados[q_num] = {
            "marcadas": marcadas,
            "resposta": marcadas[0] if len(marcadas) == 1 else None,
            "multi": len(marcadas) > 1,
        }
    return resultados


def ler_matricula(corrigida, layout, W, H):
    if "id_digitos" not in layout:
        return None
    raio = int(layout.get("digit_radius_pt", 6) / layout["page_width_pt"] * W * 1.25)
    digitos = []
    for d in sorted(layout["id_digitos"], key=int):
        melhor, melhor_v = None, 0
        for n, pos in layout["id_digitos"][d].items():
            cx, cy = int(pos["x"] * W), int((1 - pos["y"]) * H)
            v = intensidade_circulo(corrigida, cx, cy, raio)
            if v > melhor_v:
                melhor, melhor_v = n, v
        digitos.append(melhor if melhor_v >= LIMIAR_MARCACAO else "·")
    resultado = "".join(digitos)
    return resultado if resultado.strip("·") else None


def ler_versao(corrigida, layout, W, H):
    if "versao_bolhas" not in layout:
        return None
    raio = int(layout["bubble_radius_pt"] / layout["page_width_pt"] * W * 1.25)
    melhor, melhor_v = None, 0
    for L, pos in layout["versao_bolhas"].items():
        cx, cy = int(pos["x"] * W), int((1 - pos["y"]) * H)
        v = intensidade_circulo(corrigida, cx, cy, raio)
        if v > melhor_v:
            melhor, melhor_v = L, v
    return melhor if melhor_v >= LIMIAR_MARCACAO else None


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
    data, _, _ = detector.detectAndDecode(img)
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
        if isinstance(v, str):
            norm[q] = {"corretas": [v], "anulada": False, "pontos": 1.0}
        else:
            norm[q] = {
                "corretas": v.get("corretas", []),
                "anulada": bool(v.get("anulada", False)),
                "pontos": float(v.get("pontos", 1.0)),
            }
    return norm


def ler_barcode(corrigida, layout, W, H):
    """Lê o código de barras lateral da folha corrigida. Retorna folha_id ou None."""
    bc = layout.get("barcode")
    if not bc:
        return None
    bits = []
    for cell in bc["cells"]:
        x1 = int(cell["x"] * W)
        y1 = int((1 - cell["y"] - cell["h"]) * H)
        x2 = int((cell["x"] + cell["w"]) * W)
        y2 = int((1 - cell["y"]) * H)
        if x2 <= x1 or y2 <= y1:
            return None
        regiao = corrigida[max(0, y1):y2, max(0, x1):x2]
        if regiao.size == 0:
            return None
        escuro = 1 - (float(regiao.mean()) / 255)
        bits.append(1 if escuro >= 0.5 else 0)
    if len(bits) != 14:
        return None
    dados, p1, p2 = bits[:12], bits[12], bits[13]
    if sum(dados[0:6]) % 2 != p1 or sum(dados[6:12]) % 2 != p2:
        return None
    valor = 0
    for b in dados:
        valor = (valor << 1) | b
    return valor if valor > 0 else None


def processar_scan(foto_bytes, layout, gabarito_raw, folha_esperada=None):
    arr = np.frombuffer(foto_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Não consegui abrir a imagem enviada.")
    cinza = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    tipo_qr, qr_id = ler_qr(img)
    if folha_esperada is not None and qr_id is not None and qr_id != folha_esperada:
        raise ValueError(f"Esta folha tem localizador {qr_id:04d}, mas o quiz usa a folha "
                         f"{folha_esperada:04d}. Verifique se pegou a folha certa.")

    marcadores = encontrar_marcadores(cinza)
    W = 1000
    H = int(W * layout["page_height_pt"] / layout["page_width_pt"])
    corrigida = corrigir_perspectiva(cinza, marcadores, W, H, layout)

    # verificação pelo código de barras (funciona mesmo sem QR legível)
    id_barcode = ler_barcode(corrigida, layout, W, H)
    if folha_esperada is not None and id_barcode is not None and id_barcode != folha_esperada:
        raise ValueError(f"Esta folha pertence ao gabarito {id_barcode:04d}, mas esta correção "
                         f"usa o gabarito {folha_esperada:04d}. Verifique a folha.")

    leitura = ler_questoes(corrigida, layout, W, H)
    aluno_id = ler_matricula(corrigida, layout, W, H)
    versao = ler_versao(corrigida, layout, W, H)

    # recorte da primeira caixa de cabeçalho (normalmente "Nome")
    nome_img = None
    caixas = layout.get("caixas", [])
    if caixas:
        nome_img = recortar_regiao(corrigida, caixas[0], W, H)
    elif "campo_nome" in layout:  # compat v2
        nome_img = recortar_regiao(corrigida, layout["campo_nome"], W, H)

    gabarito = normalizar_gabarito(gabarito_raw)

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
                cx, cy = int(pos["x"] * W), int((1 - pos["y"]) * H)
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
        "aluno_id": aluno_id,
        "versao": versao,
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
    return redirect(url_for("dashboard") if "user_id" in session else url_for("login_page"))


@app.route("/login")
def login_page():
    return render_template("login.html")


@app.route("/dashboard")
@login_required
def dashboard():
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
    return render_template("corrigir.html")


@app.route("/offline/<int:quiz_id>")
@login_required
def offline_page(quiz_id):
    db = get_db()
    quiz = db.execute("SELECT * FROM quizzes WHERE id=? AND user_id=?",
                      (quiz_id, session["user_id"])).fetchone()
    if not quiz:
        return redirect(url_for("dashboard"))
    return render_template("offline.html", quiz_id=quiz_id, quiz_nome=quiz["nome"])


@app.route("/f/<token>")
@login_required
def compartilhar_folha(token):
    db = get_db()
    folha = db.execute("SELECT * FROM folhas WHERE share_token=?", (token,)).fetchone()
    if folha:
        ja = db.execute("SELECT id FROM folhas_compartilhadas WHERE user_id=? AND folha_id=?",
                        (session["user_id"], folha["id"])).fetchone()
        if not ja and folha["user_id"] != session["user_id"]:
            db.execute("INSERT INTO folhas_compartilhadas (user_id, folha_id) VALUES (?,?)",
                       (session["user_id"], folha["id"]))
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
    data = request.get_json()
    nome = (data.get("nome") or "").strip()
    email = (data.get("email") or "").strip().lower()
    senha = data.get("senha") or ""
    if not nome or not email or len(senha) < 6:
        return jsonify({"erro": "Preencha nome, email e senha (mín. 6 caracteres)."}), 400
    codigo = gerar_codigo()
    db = get_db()
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
    data = request.get_json()
    email = (data.get("email") or "").strip().lower()
    senha = data.get("senha") or ""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not check_password_hash(user["senha_hash"], senha):
        return jsonify({"erro": "Email ou senha incorretos."}), 401
    session.permanent = True
    session["user_id"] = user["id"]
    session["user_nome"] = user["nome"]
    return jsonify({"ok": True})


@app.route("/api/recuperar", methods=["POST"])
def api_recuperar():
    data = request.get_json()
    email = (data.get("email") or "").strip().lower()
    codigo = (data.get("codigo") or "").strip().upper()
    nova = data.get("nova_senha") or ""
    if len(nova) < 6:
        return jsonify({"erro": "A nova senha precisa de pelo menos 6 caracteres."}), 400
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not user["codigo_recuperacao"] or \
       not check_password_hash(user["codigo_recuperacao"], codigo):
        return jsonify({"erro": "Email ou código de recuperação incorretos."}), 401
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
        return {
            "id": f["id"], "nome": f["nome"],
            "localizador": f"{f['id']:04d}",
            "n_questoes": len(cfg.get("questoes", [])),
            "share_token": f["share_token"],
            "autor": autor,
        }
    return jsonify({
        "minhas": [resumo(f) for f in minhas],
        "compartilhadas": [resumo(f, f["autor"]) for f in compartilhadas],
    })


@app.route("/api/folhas", methods=["POST"])
@login_required
def api_criar_folha():
    config = request.get_json()
    nome = (config.get("nome") or "").strip()
    if not nome:
        return jsonify({"erro": "Dê um nome à folha."}), 400
    questoes = config.get("questoes", [])
    if not questoes or len(questoes) > 150:
        return jsonify({"erro": "Adicione entre 1 e 150 questões."}), 400

    db = get_db()
    token = secrets.token_urlsafe(12)
    cur = db.execute("INSERT INTO folhas (user_id, nome, config, layout, share_token) VALUES (?,?,?,?,?)",
                     (session["user_id"], nome, json.dumps(config), "{}", token))
    folha_id = cur.lastrowid
    layout, erro = compute_layout(config, folha_id)
    if erro:
        db.execute("DELETE FROM folhas WHERE id=?", (folha_id,))
        db.commit()
        return jsonify({"erro": erro}), 400
    db.execute("UPDATE folhas SET layout=? WHERE id=?", (json.dumps(layout), folha_id))
    db.commit()
    return jsonify({"ok": True, "folha_id": folha_id, "localizador": f"{folha_id:04d}"})


@app.route("/api/folhas/<int:folha_id>/pdf", methods=["GET"])
@login_required
def api_folha_pdf(folha_id):
    copias = int(request.args.get("copias", 1))
    escuro = int(request.args.get("escuro", 0))
    db = get_db()
    uid = session["user_id"]
    f = db.execute("""
        SELECT f.* FROM folhas f
        LEFT JOIN folhas_compartilhadas fc ON fc.folha_id = f.id AND fc.user_id=?
        WHERE f.id=? AND (f.user_id=? OR fc.id IS NOT NULL)
    """, (uid, folha_id, uid)).fetchone()
    if not f:
        return jsonify({"erro": "Folha não encontrada"}), 404
    pdf = gerar_pdf_folha(json.loads(f["config"]), json.loads(f["layout"]),
                          folha_id, copias=copias, escuro=escuro)
    return jsonify({"pdf": base64.b64encode(pdf).decode()})


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
        "gabarito": normalizar_gabarito(gabarito),
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
    data = request.get_json()
    resultados = data.get("resultados", [])
    if not isinstance(resultados, list):
        return jsonify({"erro": "Formato inválido."}), 400

    salvos = 0
    for r in resultados:
        try:
            db.execute("""INSERT INTO scans
                (quiz_id, respostas, acertos, erros, brancos, multimarcadas, nota,
                 debug_img, nome_img, aluno_id, versao)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (quiz_id, json.dumps(r.get("detalhe", [])),
                 r.get("acertos", 0), r.get("erros", 0), r.get("brancos", 0),
                 r.get("multimarcadas", 0), r.get("nota", 0),
                 r.get("debug_img"), r.get("nome_img"),
                 r.get("aluno_id"), r.get("versao")))
            salvos += 1
        except Exception:
            continue
    db.commit()
    return jsonify({"ok": True, "salvos": salvos, "recebidos": len(resultados)})


@app.route("/api/eu", methods=["GET"])
@login_required
def api_eu():
    db = get_db()
    u = db.execute("SELECT id, nome, email FROM users WHERE id=?",
                   (session["user_id"],)).fetchone()
    if not u:
        return jsonify({"erro": "Usuário não encontrado"}), 404
    return jsonify({"id": u["id"], "nome": u["nome"], "email": u["email"]})


@app.route("/api/deletar_conta", methods=["POST"])
@login_required
def api_deletar_conta():
    data = request.get_json() or {}
    senha = data.get("senha") or ""
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    if not u or not check_password_hash(u["senha_hash"], senha):
        return jsonify({"erro": "Senha incorreta. Digite sua senha para confirmar."}), 401
    uid = session["user_id"]
    # apaga tudo do usuário
    quiz_ids = [r["id"] for r in db.execute("SELECT id FROM quizzes WHERE user_id=?", (uid,)).fetchall()]
    for qid in quiz_ids:
        db.execute("DELETE FROM scans WHERE quiz_id=?", (qid,))
    db.execute("DELETE FROM quizzes WHERE user_id=?", (uid,))
    db.execute("DELETE FROM folhas_compartilhadas WHERE user_id=?", (uid,))
    db.execute("DELETE FROM folhas WHERE user_id=?", (uid,))
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.commit()
    session.clear()
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
            "gabarito": normalizar_gabarito(gabarito) if gabarito else {},
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
    data = request.get_json()
    nome = (data.get("nome") or "").strip()
    folha_id = data.get("folha_id")
    if not nome or not folha_id:
        return jsonify({"erro": "Informe o nome do quiz e escolha uma folha."}), 400
    db = get_db()
    uid = session["user_id"]
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
    data = request.get_json()
    itens = data.get("correcoes_offline", [])
    db = get_db()
    uid = session["user_id"]
    mapa = {}  # id_temp -> id_real

    for item in itens:
        id_temp = item.get("id_temp")
        nome = (item.get("nome") or "Correção").strip()
        folha_id = item.get("folha_id")
        gabarito = item.get("gabarito", {})
        gab_ts = int(item.get("gabarito_ts") or 0)
        resultados = item.get("resultados", [])

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

        for r in resultados:
            try:
                db.execute("""INSERT INTO scans
                    (quiz_id, respostas, acertos, erros, brancos, multimarcadas, nota,
                     debug_img, nome_img, aluno_id, versao)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (novo_id, json.dumps(r.get("detalhe", [])),
                     r.get("acertos", 0), r.get("erros", 0), r.get("brancos", 0),
                     r.get("multimarcadas", 0), r.get("nota", 0),
                     r.get("debug_img"), r.get("nome_img"),
                     r.get("aluno_id"), r.get("versao")))
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
        "gabarito": normalizar_gabarito(json.loads(q["gabarito"] or "{}")),
        "labels": labels,
    })


@app.route("/api/quizzes/<int:quiz_id>/gabarito", methods=["PUT"])
@login_required
def api_salvar_gabarito(quiz_id):
    data = request.get_json()
    gabarito = data.get("gabarito", {})
    ts = int(data.get("ts") or (datetime.now().timestamp() * 1000))
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
    layout = json.loads(q["layout"])
    folha_esperada = layout.get("folha_id") or layout.get("quiz_id")
    try:
        r = processar_scan(foto.read(), layout, gabarito, folha_esperada=folha_esperada)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 422

    cur = db.execute("""INSERT INTO scans
        (quiz_id, respostas, acertos, erros, brancos, multimarcadas, nota,
         debug_img, nome_img, aluno_id, versao)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (quiz_id, json.dumps(r["detalhe"]), r["acertos"], r["erros"], r["brancos"],
         r["multimarcadas"], r["nota"], r["debug_img"], r["nome_img"],
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
    for i, r in enumerate(rows, 1):
        w.writerow([i, r["aluno_id"] or "", r["versao"] or "",
                    str(r["nota"]).replace(".", ","),  # vírgula decimal PT-BR
                    r["acertos"], r["erros"], r["brancos"], r["multimarcadas"],
                    str(r["criado_em"])[:19]])
    conteudo = "\ufeff" + buf.getvalue()  # BOM para o Excel ler acentos
    from flask import Response
    nome = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in q["nome"])[:40]
    return Response(conteudo, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{nome}.csv"'})


@app.route("/api/quizzes/<int:quiz_id>/conferencia.pdf", methods=["GET"])
@login_required
def api_conferencia_pdf(quiz_id):
    """PDF de conferência: recorte do nome escrito à mão + nota, para digitar no
    sistema da escola sem abrir prova por prova."""
    db = get_db()
    q = db.execute("SELECT * FROM quizzes WHERE id=? AND user_id=?",
                   (quiz_id, session["user_id"])).fetchone()
    if not q:
        return jsonify({"erro": "Correção não encontrada"}), 404
    rows = db.execute("""SELECT id, nota, acertos, erros, brancos, multimarcadas,
                         nome_img, aluno_id FROM scans WHERE quiz_id=? ORDER BY id""",
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
    c.drawString(margem, y, f"{len(rows)} provas corrigidas · gerado pelo GabaritoApp")
    c.setFillColorRGB(0, 0, 0)
    y -= 8 * mm

    linha_h = 16 * mm
    for i, r in enumerate(rows, 1):
        if y - linha_h < margem:
            c.showPage()
            y = PAGE_H - margem
        # número
        c.setFont("Helvetica-Bold", 10)
        c.drawString(margem, y - 8 * mm, f"{i:02d}")
        # recorte do nome
        if r["nome_img"]:
            try:
                img_bytes = base64.b64decode(r["nome_img"])
                img = ImageReader(io.BytesIO(img_bytes))
                iw, ih = img.getSize()
                alt = 9 * mm
                larg = min(alt * (iw / ih), 105 * mm)
                c.drawImage(img, margem + 10 * mm, y - 10 * mm, larg, alt)
            except Exception:
                pass
        # matrícula (se houver)
        if r["aluno_id"]:
            c.setFont("Helvetica", 8)
            c.drawString(margem + 120 * mm, y - 8 * mm, str(r["aluno_id"]))
        # nota grande
        c.setFont("Helvetica-Bold", 15)
        c.drawRightString(PAGE_W - margem, y - 8 * mm, f"{r['nota']:.1f}".replace(".", ","))
        # detalhes
        c.setFont("Helvetica", 7)
        c.setFillColorRGB(0.5, 0.5, 0.5)
        det = f"{r['acertos']}✓ {r['erros']}✗"
        if r["brancos"]:
            det += f" {r['brancos']}branco"
        if r["multimarcadas"]:
            det += f" {r['multimarcadas']}multi"
        c.drawRightString(PAGE_W - margem, y - 12 * mm, det)
        c.setFillColorRGB(0, 0, 0)
        # linha separadora
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

    gabarito = normalizar_gabarito(json.loads(q["gabarito"] or "{}"))
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
