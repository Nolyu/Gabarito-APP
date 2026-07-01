"""
GabaritoApp — Backend Flask
Recebe foto de folha de respostas, lê as bolinhas e retorna a correção.
"""

import json
import os
import io
import base64
import tempfile

import cv2
import numpy as np
from flask import Flask, request, jsonify, render_template, send_file
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.units import mm

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB max foto

# ──────────────────────────────────────────────
# GERADOR DE GABARITO
# ──────────────────────────────────────────────
PAGE_W, PAGE_H = A4
MARKER_SIZE = 12 * mm
MARGIN = 15 * mm
ALTERNATIVAS = ["A", "B", "C", "D", "E"]
BUBBLE_RADIUS = 3.2 * mm
COL_QUESTAO_W = 10 * mm
BUBBLE_SPACING = 9 * mm


def gerar_pdf_gabarito(n_questoes: int) -> tuple[bytes, dict]:
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    layout = {"questoes": {}}

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

    # cabeçalho
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(PAGE_W / 2, PAGE_H - MARGIN - MARKER_SIZE - 10 * mm, "FOLHA DE RESPOSTAS")
    c.setFont("Helvetica", 10)
    y_cab = PAGE_H - MARGIN - MARKER_SIZE - 18 * mm
    c.drawString(MARGIN + MARKER_SIZE + 5 * mm, y_cab, "Nome: ______________________________________________")
    c.drawString(MARGIN + MARKER_SIZE + 5 * mm, y_cab - 7 * mm, "Turma: ____________   Nº: _______   Data: ___/___/______")

    # grade de bolinhas
    top_y = y_cab - 26 * mm
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
                layout["questoes"][str(q_num)][alt] = {
                    "x": cx / PAGE_W,
                    "y": y / PAGE_H,
                }

    c.save()
    layout.update({
        "page_width_pt": PAGE_W,
        "page_height_pt": PAGE_H,
        "marker_size_pt": MARKER_SIZE,
        "bubble_radius_pt": BUBBLE_RADIUS,
        "markers": [{"x": x / PAGE_W, "y": y / PAGE_H} for x, y in positions],
    })
    return buf.getvalue(), layout


# ──────────────────────────────────────────────
# ALGORITMO OMR
# ──────────────────────────────────────────────

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
        raise ValueError(f"Só encontrei {len(candidatos)} marcadores. Tente com mais iluminação ou enquadramento melhor.")
    cantos = [(0, 0), (w_img, 0), (0, h_img), (w_img, h_img)]
    marcadores = []
    restantes = candidatos.copy()
    for canto in cantos:
        melhor = min(restantes, key=lambda c: (c[0] - canto[0]) ** 2 + (c[1] - canto[1]) ** 2)
        marcadores.append((melhor[0] + melhor[2] / 2, melhor[1] + melhor[3] / 2))
        restantes.remove(melhor)
    return marcadores


def corrigir_perspectiva(cinza, marcadores, W, H, layout):
    ms = layout["marker_size_pt"]
    pw = layout["page_width_pt"]
    ph = layout["page_height_pt"]
    origem = np.array(marcadores, dtype="float32")
    destino = np.array([
        [(m["x"] + ms / (2 * pw)) * W, (1 - m["y"] - ms / (2 * ph)) * H]
        for m in layout["markers"]
    ], dtype="float32")
    matriz = cv2.getPerspectiveTransform(origem, destino)
    return cv2.warpPerspective(cinza, matriz, (W, H))


def ler_bolinhas(corrigida, layout, W, H):
    raio_px = int(layout["bubble_radius_pt"] / layout["page_width_pt"] * W * 1.3)
    resultados = {}
    for q_num, alternativas in layout["questoes"].items():
        intensidades = {}
        for alt, pos in alternativas.items():
            cx = int(pos["x"] * W)
            cy = int((1 - pos["y"]) * H)
            mask = np.zeros(corrigida.shape, dtype="uint8")
            cv2.circle(mask, (cx, cy), raio_px, 255, -1)
            media = cv2.mean(corrigida, mask=mask)[0]
            intensidades[alt] = 1 - (media / 255)
        mais_escura = max(intensidades, key=intensidades.get)
        resultados[q_num] = mais_escura if intensidades[mais_escura] >= 0.35 else None
    return resultados


def processar_foto(foto_bytes: bytes, layout: dict, gabarito: dict) -> dict:
    arr = np.frombuffer(foto_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    cinza = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    marcadores = encontrar_marcadores(cinza)
    W, H = 1000, 1414
    corrigida = corrigir_perspectiva(cinza, marcadores, W, H, layout)
    respostas = ler_bolinhas(corrigida, layout, W, H)

    # imagem de debug colorida
    debug = cv2.cvtColor(corrigida, cv2.COLOR_GRAY2BGR)
    raio = int(layout["bubble_radius_pt"] / layout["page_width_pt"] * W * 1.3)
    for q, alts in layout["questoes"].items():
        for alt, pos in alts.items():
            cx = int(pos["x"] * W)
            cy = int((1 - pos["y"]) * H)
            marcada = respostas.get(q)
            certa = gabarito.get(q)
            if alt == marcada:
                cor = (0, 200, 0) if marcada == certa else (0, 0, 220)
                cv2.circle(debug, (cx, cy), raio, cor, 3)
    _, buf = cv2.imencode(".jpg", debug, [cv2.IMWRITE_JPEG_QUALITY, 85])
    debug_b64 = base64.b64encode(buf).decode()

    acertos = sum(1 for q, r in respostas.items() if gabarito.get(q) and r == gabarito[q])
    total = len(gabarito)
    detalhe = []
    for q in sorted(respostas, key=lambda x: int(x)):
        if q not in gabarito:
            continue
        marcada = respostas[q]
        certa = gabarito[q]
        detalhe.append({
            "questao": q,
            "marcada": marcada or "-",
            "correta": certa,
            "status": "certo" if marcada == certa else ("branco" if marcada is None else "errado"),
        })

    return {
        "acertos": acertos,
        "total": total,
        "nota": round(acertos / total * 10, 2) if total else 0,
        "detalhe": detalhe,
        "debug_img": debug_b64,
    }


# ──────────────────────────────────────────────
# ROTAS
# ──────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/gerar-gabarito", methods=["POST"])
def gerar_gabarito_route():
    data = request.get_json()
    n = int(data.get("n_questoes", 20))
    if not 5 <= n <= 100:
        return jsonify({"erro": "Número de questões deve ser entre 5 e 100"}), 400
    pdf_bytes, layout = gerar_pdf_gabarito(n)
    pdf_b64 = base64.b64encode(pdf_bytes).decode()
    return jsonify({"pdf": pdf_b64, "layout": layout})


@app.route("/corrigir", methods=["POST"])
def corrigir_route():
    try:
        foto = request.files.get("foto")
        layout = json.loads(request.form.get("layout", "{}"))
        gabarito = json.loads(request.form.get("gabarito", "{}"))
        if not foto or not layout or not gabarito:
            return jsonify({"erro": "Envie a foto, o layout e o gabarito."}), 400
        resultado = processar_foto(foto.read(), layout, gabarito)
        return jsonify(resultado)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 422
    except Exception as e:
        return jsonify({"erro": f"Erro interno: {str(e)}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
