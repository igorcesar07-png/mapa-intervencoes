"""KMZ/KML do eixo: mesmas regras do index.html (blocosDoKml).

- pontos de estaca: nome "164+700" e campo "Bloco" -> eixo com km real;
- linhas (LineString) -> eixo sem estaca.
"""
import io
import re
import zipfile

from defusedxml import ElementTree as ET

RODOVIA_BLOCO = {"2": "BR-277", "3": "BR-277", "4": "BR-373"}
MAX_KML = 50 * 1024 * 1024


class KmzInvalido(Exception):
    pass


ESTACA = re.compile(r"^\s*(?:est(?:aca)?\.?\s*)?(\d{1,4})\s*\+\s*(\d{1,3}(?:[.,]\d+)?)\s*$", re.I)


def bloco_do_nome(*textos):
    """Sem o campo "Bloco": tenta "Bloco 4", "Bloco_04", "B4" no nome do arquivo ou do documento."""
    for t in textos:
        m = re.search(r"bloco\s*[_ -]?\s*0*(\d{1,2})\b", t or "", re.I) or re.search(r"(?:^|[_\s-])B0*(\d{1,2})(?=$|[_\s.-])", t or "")
        if m:
            return m.group(1)
    return None


def rodovia_do_nome(*textos):
    for t in textos:
        m = re.search(r"\b(BR|PR|SC|SP|RS)\s*[-_ ]?\s*(\d{3})\b", t or "", re.I)
        if m:
            return f"{m.group(1).upper()}-{m.group(2)}"
    return None


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _filhos(el, nome):
    return [e for e in el.iter() if _local(e.tag) == nome]


def _num(s):
    return float(s.replace(",", "."))


def ler_kml_bytes(nome, dados):
    if nome.lower().endswith(".kmz"):
        try:
            with zipfile.ZipFile(io.BytesIO(dados)) as z:
                kml = next((i for i in z.infolist() if i.filename.lower().endswith(".kml")), None)
                if not kml:
                    raise KmzInvalido("O KMZ não tem arquivo .kml dentro.")
                if kml.file_size > MAX_KML:
                    raise KmzInvalido("O .kml dentro do KMZ é grande demais.")
                return z.read(kml)
        except zipfile.BadZipFile:
            raise KmzInvalido("O KMZ está corrompido.")
    return dados


def blocos_do_kml(dados, nome_arq):
    try:
        raiz = ET.fromstring(dados)
    except Exception as e:  # inclui XML com entidades (bloqueado pelo defusedxml)
        raise KmzInvalido(f"Não consegui ler o KML: {e.__class__.__name__}")
    base = re.sub(r"\.(kmz|kml)$", "", nome_arq, flags=re.I)
    doc_nome = next((e.text or "" for e in raiz.iter() if _local(e.tag) == "name"), "")
    bloco_arquivo = bloco_do_nome(base, doc_nome)
    rodovia_arquivo = rodovia_do_nome(base, doc_nome)
    blocos = {}

    def dado(pm, nome):
        for d in _filhos(pm, "Data"):
            disp = next((x.text or "" for x in d if _local(x.tag) == "displayName"), "")
            if (d.get("name") or "").lower() == nome or disp.strip().lower() == nome:
                v = next((x for x in d if _local(x.tag) == "value"), None)
                return (v.text or "").strip() if v is not None else None
        for d in _filhos(pm, "SimpleData"):
            if (d.get("name") or "").lower() == nome:
                return (d.text or "").strip()
        desc = next((e.text or "" for e in _filhos(pm, "description")), "")
        m = re.search(nome + r"\s*:\s*([^<\n]+)", desc, re.I)
        return m.group(1).strip() if m else None

    def coords(el):
        out = []
        for c in (el.text or "").split():
            p = c.split(",")
            try:
                out.append([round(float(p[1]), 6), round(float(p[0]), 6)])
            except (IndexError, ValueError):
                pass
        return out

    for pm in _filhos(raiz, "Placemark"):
        nome_el = next((e for e in pm if _local(e.tag) == "name"), None)
        nome = (nome_el.text or "").strip() if nome_el is not None else ""
        pt = next(iter(_filhos(pm, "Point")), None)
        # aceita "164+700" e também "Estaca 164+700" / "Est. 164+700"; senão, o campo "Estaca"
        m = ESTACA.match(nome) or ESTACA.match(dado(pm, "estaca") or "")
        if pt is not None and m:
            ce = next(iter(_filhos(pt, "coordinates")), None)
            c = coords(ce) if ce is not None else []
            if not c:
                continue
            bl = dado(pm, "bloco")
            if bl:
                try:
                    f = float(bl.replace(",", "."))
                    bl = str(int(f)) if f.is_integer() else str(f)
                except ValueError:
                    pass
            else:
                bl = bloco_arquivo or base
            rod = dado(pm, "rodovia") or rodovia_arquivo
            b = blocos.setdefault(bl, {"rodovia": rod or RODOVIA_BLOCO.get(bl, ""), "fonte": nome_arq, "pts": []})
            b["pts"].append([c[0][0], c[0][1], round(int(m.group(1)) * 1000 + _num(m.group(2)))])
            continue
        for i, ls in enumerate(_filhos(pm, "LineString")):
            ce = next(iter(_filhos(ls, "coordinates")), None)
            l = coords(ce) if ce is not None else []
            if len(l) < 2:
                continue
            bid = f"{nome or base}{' ' + str(i + 1) if i else ''}"
            blocos.setdefault(bid, {"rodovia": "", "fonte": nome_arq, "pts": []})["pts"].extend([p[0], p[1], None] for p in l)
    for b in blocos.values():
        if b["pts"] and b["pts"][0][2] is not None:
            b["pts"].sort(key=lambda p: p[2])
            b["pts"] = [p for i, p in enumerate(b["pts"]) if i == 0 or p[2] != b["pts"][i - 1][2]]
    if not blocos:
        raise KmzInvalido("Não achei estacas (pontos “km+m”) nem linhas no KMZ.")
    return blocos
