#!/usr/bin/env python3
"""
atualizar_dados.py — alimenta o mapa de calor de intervenções com novos ZIPs de fotos.

Uso:
    python atualizar_dados.py "22-10 - TAPA BURACO.zip" "29-10 - TAPA BURACO.zip"
    python atualizar_dados.py pasta_com_fotos/

O que faz, para cada JPG/JPEG dentro dos ZIPs (ou pastas):
  1. Lê a DATA DA INTERVENÇÃO e as coordenadas, nesta ordem de prioridade:
       a) EXIF da câmera (DateTimeOriginal + GPS) — quando a foto ainda tem EXIF;
       b) carimbo impresso na foto (apps tipo "GPS Map Camera"/"Timestamp Camera"),
          lido por OCR com Tesseract — é o caso das fotos enviadas pelo WhatsApp,
          que perdem todo o EXIF.
     A data de modificação do arquivo NUNCA é usada. O nome "WhatsApp Image <data>"
     só entra como último recurso e fica marcado como data_fonte="nome_arquivo"
     (é a data do envio, não da intervenção).
  2. Ignora fotos já importadas (mesmo conteúdo = mesmo hash SHA-1).
  3. Acrescenta os registros novos em dados.json e regrava dados.js
     (o index.html lê o dados.js, que funciona mesmo abrindo a página por duplo-clique).

Requisitos: Python 3.9+, Pillow, numpy e o Tesseract OCR instalado.
    pip install pillow numpy
    Windows: instalar Tesseract (https://github.com/UB-Mannheim/tesseract/wiki)
             — o script procura em C:\\Program Files\\Tesseract-OCR\\tesseract.exe
    Linux:   sudo apt install tesseract-ocr     macOS: brew install tesseract
"""
import argparse, base64, datetime as dt, hashlib, io, json, os, re, shutil, subprocess, sys, tempfile, zipfile
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image, ImageOps

AQUI = os.path.dirname(os.path.abspath(__file__))
MESES = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
         "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}

# ------------------------------------------------------------------ Tesseract
def achar_tesseract():
    for c in [shutil.which("tesseract"),
              r"C:\Program Files\Tesseract-OCR\tesseract.exe",
              r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
              "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"]:
        if c and os.path.exists(c):
            return c
    return None

TESS = achar_tesseract()

# ------------------------------------------------------------------ EXIF
def _rat(v):
    try:
        return float(v)
    except Exception:
        return v[0] / v[1]

def ler_exif(im):
    """Retorna (data_iso|None, lat|None, lon|None) a partir do EXIF."""
    try:
        ex = im.getexif()
    except Exception:
        return None, None, None
    if not ex:
        return None, None, None
    data = None
    sub = ex.get_ifd(0x8769)
    bruto = sub.get(0x9003) or sub.get(0x9004) or ex.get(0x0132)  # DateTimeOriginal > Digitized > DateTime
    if bruto:
        m = re.match(r"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})", str(bruto))
        if m:
            data = "{}-{}-{}T{}:{}:{}".format(*m.groups())
    lat = lon = None
    gps = ex.get_ifd(0x8825)
    if gps and 2 in gps and 4 in gps:
        try:
            d = gps[2]; lat = _rat(d[0]) + _rat(d[1]) / 60 + _rat(d[2]) / 3600
            d = gps[4]; lon = _rat(d[0]) + _rat(d[1]) / 60 + _rat(d[2]) / 3600
            if str(gps.get(1, "N")).upper().startswith("S"): lat = -lat
            if str(gps.get(3, "E")).upper().startswith("W"): lon = -lon
            if lat == 0 and lon == 0: lat = lon = None
        except Exception:
            lat = lon = None
    return data, lat, lon

# ------------------------------------------------------------------ OCR do carimbo
def preparar(im, limiar):
    """Recorta a metade de baixo, mantém só pixels brancos (texto do carimbo) e
    devolve texto preto em fundo branco, que é o que o Tesseract lê melhor.
    limiar=None: só tons de cinza invertidos (texto branco sobre fundo claro)."""
    w, h = im.size
    c = im.crop((0, int(h * 0.45), w, h))
    if w != 1600:
        f = 1600 / w
        c = c.resize((int(c.width * f), int(c.height * f)), Image.LANCZOS)
    if limiar is None:
        return ImageOps.autocontrast(ImageOps.invert(c.convert("L")), cutoff=1)
    a = np.asarray(c, dtype=np.int16)
    mn, mx = a.min(axis=2), a.max(axis=2)
    branco = (mn > limiar) & ((mx - mn) < 45)
    return Image.fromarray(np.where(branco, 0, 255).astype(np.uint8))

def ocr(img_l, psm):
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as t:
        img_l.save(t.name)
        nome = t.name
    try:
        r = subprocess.run([TESS, nome, "-", "--psm", str(psm), "-l", "eng"],
                           capture_output=True, text=True, timeout=60)
        return r.stdout
    finally:
        os.unlink(nome)

NUM = r"\d{1,3}(?:[.,]\d+)?"

def _n(s):
    return float(s.replace(",", "."))

def parse_carimbo(txt):
    """Extrai data, lat, lon, km, rodovia e local do texto do carimbo."""
    t = txt.replace("'\"", '"').replace("º", "°").replace("’", "'").replace("‘", "'").replace("”", '"').replace("“", '"').replace("''", '"')
    out = {"data": None, "lat": None, "lon": None, "km": None, "rodovia": None, "local": None}

    # Data: "22 de out. de 2026 14:54:56"  |  "22/10/2026 14:54"  |  "2026-10-22 14:54"
    m = re.search(r"(\d{1,2})\s*de\s*([a-zç]{3})[a-zç]*\.?\s*de\s*(\d{4})\D{0,3}(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?", t, re.I)
    if m and m.group(2).lower()[:3] in MESES:
        d, mes, a, hh, mm, ss = m.groups()
        out["data"] = "%s-%02d-%02dT%02d:%s:%s" % (a, MESES[mes.lower()[:3]], int(d), int(hh), mm, ss or "00")
    else:
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})\D{1,3}(\d{1,2}):(\d{2})(?::(\d{2}))?", t)
        if m:
            d, mo, a, hh, mm, ss = m.groups()
            out["data"] = "%s-%02d-%02dT%02d:%s:%s" % (a, int(mo), int(d), int(hh), mm, ss or "00")
        else:
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})\D{1,3}(\d{1,2}):(\d{2})(?::(\d{2}))?", t)
            if m:
                a, mo, d, hh, mm, ss = m.groups()
                out["data"] = "%s-%s-%sT%02d:%s:%s" % (a, mo, d, int(hh), mm, ss or "00")

    # Coordenadas em graus/min/seg: 25°22'28,652"S 50°53'17,851"W
    m = re.search(r"(\d{1,2})\s*°\s*(\d{1,2})\s*'\s*(\d{1,2}(?:[.,]\d+)?)\s*\"?\s*([NS])\D{0,6}?"
                  r"(\d{1,3})\s*°\s*(\d{1,2})\s*'\s*(\d{1,2}(?:[.,]\d+)?)\s*\"?\s*([EWOL])", t, re.I)
    if m:
        g1, m1, s1, h1, g2, m2, s2, h2 = m.groups()
        lat = int(g1) + int(m1) / 60 + _n(s1) / 3600
        lon = int(g2) + int(m2) / 60 + _n(s2) / 3600
        out["lat"] = -lat if h1.upper() == "S" else lat
        out["lon"] = -lon if h2.upper() in "WO" else lon
    else:
        # Rotulado: "Latitude: -25.433760" / "Longitude: -50.057283"
        mla = re.search(r"Latitude\W{0,4}([-~—–]?)\s*(\d{1,2})[.,:](\d{4,})", t, re.I)
        mlo = re.search(r"Longitude\W{0,4}([-~—–]?)\s*(\d{1,3})[.,:](\d{4,})", t, re.I)
        # Decimal com hemisfério: 25.472820000000002S 49.89628833333333W
        # (tolera ruído de OCR: ':' no lugar de '.', espaço ou '/' no meio dos dígitos)
        m = re.search(r"(\d{1,2})[.,:]([\d /]{4,}?)\s*([NS])\W{0,4}(\d{1,3})[.,:]([\d /]{4,}?)\s*([EWOL])\b", t, re.I)
        if mla and mlo:
            out["lat"] = -_n(mla.group(2) + "." + mla.group(3))  # Brasil: sul/oeste
            out["lon"] = -_n(mlo.group(2) + "." + mlo.group(3))
            if mla.group(1) == "" and "N" in t[mla.end():mla.end() + 3].upper(): out["lat"] *= -1
        elif m:
            g1, f1, h1, g2, f2, h2 = m.groups()
            la = _n(g1 + "." + re.sub(r"[ /]", "", f1))
            lo = _n(g2 + "." + re.sub(r"[ /]", "", f2))
            out["lat"] = -la if h1.upper() == "S" else la
            out["lon"] = -lo if h2.upper() in "WO" else lo
        else:
            m = re.search(r"(-?\d{1,2}[.,]\d{4,})\s*[,;/ ]\s*(-?\d{1,3}[.,]\d{4,})", t)
            if m:
                out["lat"], out["lon"] = _n(m.group(1)), _n(m.group(2))

    # km: "km158"  |  estaca/localização "182+691" (= km 182,691)
    m = re.search(r"Locali\w*\W{0,4}(\d{1,4})\s*\+\s*(\d{1,3})", t, re.I) or re.search(r"\b(\d{2,4})\s*\+\s*(\d{3})\b", t)
    if m:
        out["km"] = int(m.group(1)) + int(m.group(2)) / 1000
    else:
        m = re.search(r"\bkm\s*(\d{1,4}(?:[.,]\d+)?)", t, re.I)
        if m:
            out["km"] = _n(m.group(1))
    m = re.search(r"(Crescente|Decrescente)", t, re.I)
    if m:
        out["sentido"] = m.group(1).capitalize()
    m = re.search(r"\b(BR|PR|SP|SC|RS|MG)\s*-?\s*(\d{3})", t)
    if m:
        out["rodovia"] = "%s-%s" % (m.group(1), m.group(2))

    # Local: primeira linha só com letras depois da linha da rodovia
    linhas = [l.strip() for l in t.splitlines() if l.strip()]
    idx = next((i for i, l in enumerate(linhas) if re.search(r"\b(BR|PR)\s*-?\s*\d{3}", l)), None)
    if idx is not None:
        for l in linhas[idx + 1:idx + 3]:
            if re.fullmatch(r"[A-Za-zÀ-ÿ' .-]{3,40}", l) and not re.match(r"(Altitude|Velocidade)", l, re.I):
                out["local"] = l
                break
    return out

def coord_ok(lat, lon):
    # caixa ampla do Brasil — descarta leituras absurdas do OCR
    return lat is not None and lon is not None and -34.5 < lat < 6 and -74.5 < lon < -32

PASSES = [(200, 6), (175, 6), (None, 6), (215, 11), (160, 11), (None, 11)]

def candidatos_coord(im):
    """Todas as leituras de coordenada possíveis da foto (uma por passada de OCR)."""
    out = []
    for limiar, psm in PASSES:
        p = parse_carimbo(ocr(preparar(im, limiar), psm))
        if coord_ok(p["lat"], p["lon"]):
            out.append((p["lat"], p["lon"]))
    return out

def ler_carimbo(im, todas=False):
    if not TESS:
        return None, "", []
    melhor, texto, datas = None, "", []
    for limiar, psm in PASSES:
        txt = ocr(preparar(im, limiar), psm)
        p = parse_carimbo(txt)
        if p["data"]:
            datas.append(p["data"])
        nota = (p["data"] is not None) + 2 * coord_ok(p["lat"], p["lon"])
        if melhor is None or nota > melhor[0]:
            melhor, texto = (nota, p), txt
        if nota == 3 and not todas:
            break
    return melhor[1], texto, datas

# ------------------------------------------------------------------ utilidades
def data_do_nome(nome):
    m = re.search(r"(\d{4})-(\d{2})-(\d{2}) at (\d{2})\.(\d{2})\.(\d{2})", nome)
    if m:
        return "{}-{}-{}T{}:{}:{}".format(*m.groups())
    m = re.search(r"(?:IMG|PXL|VID)?[_-]?(20\d{2})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})", nome)
    if m:
        return "{}-{}-{}T{}:{}:{}".format(*m.groups())
    return None

def miniatura(im, lado=360):
    t = im.copy()
    t.thumbnail((lado, lado))
    b = io.BytesIO()
    t.save(b, "JPEG", quality=62, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()

def processar(nome, conteudo, lote):
    h = hashlib.sha1(conteudo).hexdigest()
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(conteudo)))
    im_rgb = im.convert("RGB")
    r = {"id": h[:16], "id_completo": h, "arquivo": os.path.basename(nome), "lote": lote,
         "data": None, "data_fonte": None, "lat": None, "lon": None, "coord_fonte": None,
         "km": None, "rodovia": None, "local": None,
         "importado_em": dt.datetime.now().isoformat(timespec="seconds")}
    d, la, lo = ler_exif(im)
    if d:
        r["data"], r["data_fonte"] = d, "exif"
    if coord_ok(la, lo):
        r["lat"], r["lon"], r["coord_fonte"] = round(la, 7), round(lo, 7), "exif"
    if r["data"] is None or r["lat"] is None:
        p, txt, _ = ler_carimbo(im_rgb)
        if p:
            if r["data"] is None and p["data"]:
                r["data"], r["data_fonte"] = p["data"], "carimbo"
            if r["lat"] is None and coord_ok(p["lat"], p["lon"]):
                r["lat"], r["lon"], r["coord_fonte"] = round(p["lat"], 7), round(p["lon"], 7), "carimbo"
            r["km"], r["rodovia"], r["local"] = p["km"], p["rodovia"], p["local"]
            if p.get("sentido"): r["sentido"] = p["sentido"]
            r["texto_ocr"] = " | ".join(l.strip() for l in txt.splitlines() if l.strip())[-300:]
    # trava: a foto não pode ser posterior ao envio pelo WhatsApp (data no nome do arquivo)
    envio = data_do_nome(r["arquivo"])
    if r["data_fonte"] == "carimbo" and envio and r["data"] > envio and \
            (dt.datetime.fromisoformat(r["data"]) - dt.datetime.fromisoformat(envio)).total_seconds() > 600:
        _, _, datas = ler_carimbo(im_rgb, todas=True)
        ok = [d for d in datas if (dt.datetime.fromisoformat(d) - dt.datetime.fromisoformat(envio)).total_seconds() <= 600]
        if ok:
            r["data_ocr_original"] = r["data"]
            r["data"] = max(set(ok), key=ok.count)
            r["data_revisada"] = True
        else:
            r["data_suspeita"] = True
    if r["data"] is None:
        nd = data_do_nome(r["arquivo"])
        if nd:
            r["data"], r["data_fonte"] = nd, "nome_arquivo"
    r["thumb"] = miniatura(im_rgb)
    return r

def coletar(entradas):
    """Gera (nome, bytes, lote) para cada JPG em ZIPs ou pastas."""
    for e in entradas:
        if os.path.isdir(e):
            for raiz, _, arqs in os.walk(e):
                for a in sorted(arqs):
                    if a.lower().endswith((".jpg", ".jpeg")):
                        with open(os.path.join(raiz, a), "rb") as f:
                            yield a, f.read(), os.path.basename(os.path.normpath(e))
        elif e.lower().endswith(".zip"):
            lote = os.path.splitext(os.path.basename(e))[0]
            with zipfile.ZipFile(e) as z:
                for n in sorted(z.namelist()):
                    if n.lower().endswith((".jpg", ".jpeg")) and not os.path.basename(n).startswith("._"):
                        yield n, z.read(n), lote
        elif e.lower().endswith((".jpg", ".jpeg")):
            with open(e, "rb") as f:
                yield os.path.basename(e), f.read(), "avulsas"

def dist_m(a, b):
    import math
    dy = (a[0] - b[0]) * 111320
    dx = (a[1] - b[1]) * 111320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)

def revisar_isolados(novos, antigos, conteudos, limite_m=15000):
    """Ponto a mais de 15 km de qualquer outro costuma ser dígito do grau lido
    errado pelo OCR (25→29, 25→5). Relê a foto com todas as passadas e fica com
    a leitura que cair perto dos demais pontos. Se nenhuma cair, o ponto é mantido
    como está — e a página o mostra como anomalia."""
    todos = [r for r in antigos + novos if r["lat"] is not None]
    if len(todos) < 3:
        return
    def viz(c, eu):
        return min((dist_m(c, (o["lat"], o["lon"])) for o in todos if o is not eu), default=1e12)
    for r in novos:
        if r["lat"] is None or r["coord_fonte"] != "carimbo":
            continue
        if viz((r["lat"], r["lon"]), r) <= limite_m:
            continue
        im = Image.open(io.BytesIO(conteudos[r["id_completo"]])).convert("RGB")
        cands = candidatos_coord(im)
        if not cands:
            continue
        melhor = min(cands, key=lambda c: viz(c, r))
        if viz(melhor, r) <= limite_m and dist_m(melhor, (r["lat"], r["lon"])) > 1:
            r["coord_ocr_original"] = [r["lat"], r["lon"]]
            r["lat"], r["lon"] = round(melhor[0], 7), round(melhor[1], 7)
            r["coord_revisada"] = True
            print(f"  revisado: {r['arquivo']}  {r['coord_ocr_original']} -> {[r['lat'], r['lon']]}")

def marcar_duplicatas(regs):
    """Mesma foto reenviada (bytes diferentes, mesmo carimbo): mesma data/hora e
    mesma posição. Fica marcada com 'duplicata_de' e a página não conta duas vezes."""
    vistos = []
    for r in regs:
        r.pop("duplicata_de", None)
        if r["data_fonte"] not in ("carimbo", "exif") or r["data"] is None:
            continue
        seg = not r["data"].endswith(":00")
        for o in vistos:
            if o["data"] != r["data"]:
                continue
            if r["lat"] is None or o["lat"] is None:
                mesmo = r["lat"] is None and o["lat"] is None and seg
            else:
                mesmo = dist_m((r["lat"], r["lon"]), (o["lat"], o["lon"])) < (30 if seg else 2)
            if mesmo:
                r["duplicata_de"] = o["id"]
                break
        else:
            vistos.append(r)

def main():
    ap = argparse.ArgumentParser(description="Importa ZIPs/pastas de fotos para o dados.json do mapa de calor.")
    ap.add_argument("entradas", nargs="+", help="arquivos .zip, pastas ou .jpg")
    ap.add_argument("--dados", default=os.path.join(AQUI, "dados.json"), help="dados.json a atualizar (padrão: ao lado do script)")
    a = ap.parse_args()

    if not TESS:
        print("AVISO: Tesseract não encontrado — só fotos com EXIF terão coordenadas. Veja o cabeçalho do script.")

    base = {"versao": 1, "registros": []}
    if os.path.exists(a.dados):
        with open(a.dados, encoding="utf-8") as f:
            base = json.load(f)
    vistos = {r["id"] for r in base["registros"]}

    fila, pulados, conteudos = [], 0, {}
    for nome, conteudo, lote in coletar(a.entradas):
        h = hashlib.sha1(conteudo).hexdigest()[:16]
        if h in vistos:
            pulados += 1
            continue
        vistos.add(h)
        fila.append((nome, conteudo, lote))
        conteudos[hashlib.sha1(conteudo).hexdigest()] = conteudo

    print(f"{len(fila)} foto(s) nova(s), {pulados} já importada(s). Processando…")
    novos = []
    with ThreadPoolExecutor(max_workers=max(2, os.cpu_count() or 2)) as ex:
        for i, r in enumerate(ex.map(lambda x: processar(*x), fila), 1):
            novos.append(r)
            if i % 20 == 0 or i == len(fila):
                print(f"  {i}/{len(fila)}")

    revisar_isolados(novos, base["registros"], {h: c for h, c in conteudos.items()})
    base["registros"].extend(novos)
    marcar_duplicatas(base["registros"])
    for r in novos: r.pop("id_completo", None)
    base["registros"].sort(key=lambda r: (r["data"] or "9999", r["arquivo"]))
    base["atualizado_em"] = dt.datetime.now().isoformat(timespec="seconds")

    with open(a.dados, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False, indent=0)
    js = os.path.splitext(a.dados)[0] + ".js"
    with open(js, "w", encoding="utf-8") as f:
        f.write("// Gerado por atualizar_dados.py — não editar à mão\nwindow.DADOS_BASE = ")
        json.dump(base, f, ensure_ascii=False)
        f.write(";\n")

    sem_coord = [r["arquivo"] for r in novos if r["lat"] is None]
    data_fraca = [r["arquivo"] for r in novos if r["data_fonte"] in (None, "nome_arquivo")]
    print(f"\nOK: {len(novos)} registro(s) adicionados. Total no mapa: {len(base['registros'])}.")
    print(f"Gravados: {a.dados} e {js}")
    if sem_coord:
        print(f"\n{len(sem_coord)} foto(s) sem coordenada legível (não entram no mapa):")
        for n in sem_coord: print("   -", n)
    if data_fraca:
        print(f"\n{len(data_fraca)} foto(s) sem data no carimbo/EXIF (usada data do nome do arquivo ou nenhuma):")
        for n in data_fraca: print("   -", n)

if __name__ == "__main__":
    main()
