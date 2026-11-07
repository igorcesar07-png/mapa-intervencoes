"""Classificação de cada foto num segmento elegível do mapa, com as divergências para revisão.

Ordem de prioridade (o que está escrito na foto vem primeiro):
  1. rodovia, lote e estaca/km escritos no carimbo;
  2. coordenada escrita no carimbo;
  3. demais metadados (EXIF, nome do arquivo, data e hora).

Só três segmentos podem entrar no mapa: BR-277 lotes 2 e 3, e BR-373 lote 4.
Uma foto só é classificada automaticamente quando os dados são suficientes e compatíveis com
um desses segmentos (a estaca existe no eixo do lote e/ou a coordenada cai nesse lote). Em
qualquer conflito a foto fica pendente: nada é trocado em silêncio e nenhuma foto é associada
a outro trecho por conta própria.
"""
import re

from . import regras

ELEGIVEIS = {("BR-277", "2"), ("BR-277", "3"), ("BR-373", "4")}
RODOVIA_DO_LOTE = {"2": "BR-277", "3": "BR-277", "4": "BR-373"}
TOL_ESTACA_M = 300        # estaca do carimbo x estaca da coordenada
TOL_EXIF_M = 50           # coordenada do carimbo x coordenada do EXIF


def _m(v):
    """Metros com separador de milhar brasileiro: 1234.5 -> '1.235'."""
    return f"{v:,.0f}".replace(",", ".")


def fmt_seg(rod, lote):
    return f"{rod or 'rodovia ?'} · lote {lote or '?'}"


def norm_lote(v):
    if v is None or v == "":
        return None
    m = re.search(r"\d+", str(v))
    return str(int(m.group(0))) if m else None


def norm_rodovia(v):
    if not v:
        return None
    m = re.search(r"([A-Z]{2})\s*-?\s*(\d{3})", str(v).upper())
    return f"{m.group(1)}-{m.group(2)}" if m else None


def dados_do_carimbo(r):
    """Campos escritos na foto. Para fotos antigas (sem os campos novos), relê o texto do OCR guardado."""
    ex = r.get("extras") or {}
    c = {"rodovia": norm_rodovia(r.get("rodovia")), "lote": norm_lote(ex.get("lote_carimbo") or r.get("lote_carimbo")),
         "est": ex.get("est_carimbo", r.get("est_carimbo")), "faixa": ex.get("faixa") or r.get("faixa"),
         "nota": ex.get("nota") or r.get("nota")}
    txt = ex.get("texto_ocr") or r.get("texto_ocr")
    if txt and (c["lote"] is None or c["est"] is None or c["rodovia"] is None):
        from .processamento import leitura
        try:
            p = leitura().parse_carimbo(txt.replace(" | ", "\n"))
        except Exception:
            p = {}
        c["rodovia"] = c["rodovia"] or norm_rodovia(p.get("rodovia"))
        c["lote"] = c["lote"] or norm_lote(p.get("lote"))
        if c["est"] is None:
            c["est"] = p.get("est")
        c["faixa"] = c["faixa"] or p.get("faixa")
        c["nota"] = c["nota"] or p.get("nota")
    return c


def _rodovia_bloco(blocos, b):
    return norm_rodovia((blocos.get(b) or {}).get("rodovia")) or RODOVIA_DO_LOTE.get(b)


def classificar(r, blocos, params, linhas=None):
    """r: foto (lat, lon, rodovia, extras/campos do carimbo). blocos: eixo por bloco/lote.
    Devolve {estado: 'auto'|'pendente', carimbo, sistema, divergencias, sugestao, final}."""
    lim = float(params.get("limite_anomalia_m", 150))
    linhas = linhas if linhas is not None else regras.linhas_eixo(blocos)
    c = dados_do_carimbo(r)
    div = []

    # segmento escrito na foto (lote 2/3 -> BR-277, lote 4 -> BR-373 quando a rodovia não foi lida)
    rod_c, lote_c = c["rodovia"], c["lote"]
    if lote_c and not rod_c:
        rod_c = RODOVIA_DO_LOTE.get(lote_c)
    seg_c = (rod_c, lote_c) if rod_c and lote_c else None

    # o que a coordenada indica (projeção no eixo)
    sistema = {}
    if r.get("lat") is not None and linhas:
        pj = regras.projetar(r, linhas)
        if pj.get("bloco") is not None:
            b = str(pj["bloco"])
            sistema = {"lote": b, "rodovia": _rodovia_bloco(blocos, b), "est": pj.get("est"), "dist": pj.get("dist")}
    ex = r.get("extras") or {}
    if ex.get("coord_exif") or r.get("coord_exif"):
        ce = ex.get("coord_exif") or r.get("coord_exif")
        div.append({"campo": "coordenada", "carimbo": [r.get("lat"), r.get("lon")], "sistema": ce,
                    "msg": f"A coordenada do carimbo difere da do EXIF da câmera ({_m(regras.dist_m((r['lat'], r['lon']), ce))} m)."})

    if seg_c and seg_c not in ELEGIVEIS:
        div.append({"campo": "segmento", "carimbo": fmt_seg(*seg_c), "sistema": None,
                    "msg": f"O carimbo indica {fmt_seg(*seg_c)}, que não está entre os segmentos do mapa "
                           "(BR-277 lotes 02 e 03, BR-373 lote 04)."})
    if seg_c and seg_c in ELEGIVEIS and seg_c[1] not in blocos:
        div.append({"campo": "eixo", "carimbo": fmt_seg(*seg_c), "sistema": None,
                    "msg": f"O eixo do lote {seg_c[1]} não está carregado: não dá para conferir a estaca."})
    if c["rodovia"] and lote_c and RODOVIA_DO_LOTE.get(lote_c) and RODOVIA_DO_LOTE[lote_c] != c["rodovia"]:
        div.append({"campo": "rodovia", "carimbo": c["rodovia"], "sistema": RODOVIA_DO_LOTE[lote_c],
                    "msg": f"O carimbo diz {c['rodovia']}, mas o lote {lote_c} é da {RODOVIA_DO_LOTE[lote_c]}."})

    # estaca do carimbo dentro do lote do carimbo
    ponto_est = None
    if c["est"] is not None and seg_c and seg_c[1] in blocos:
        ponto_est = regras.estaca_para_ponto(blocos, seg_c[1], c["est"])
        if not ponto_est:
            div.append({"campo": "estaca", "carimbo": regras.fmt_est(c["est"]), "sistema": None,
                        "msg": f"A estaca {regras.fmt_est(c['est'])} do carimbo não existe no eixo do lote {seg_c[1]}."})

    # coordenada x carimbo
    if sistema:
        if sistema["dist"] is not None and sistema["dist"] > lim:
            div.append({"campo": "coordenada", "carimbo": [r["lat"], r["lon"]], "sistema": round(sistema["dist"]),
                        "msg": f"A coordenada está a {_m(sistema['dist'])} m do eixo (limite {lim:.0f} m)."})
        elif seg_c:
            if sistema["lote"] != seg_c[1]:
                div.append({"campo": "lote", "carimbo": seg_c[1], "sistema": sistema["lote"],
                            "msg": f"O carimbo diz lote {seg_c[1]}, mas a coordenada cai no lote {sistema['lote']}."})
            elif c["est"] is not None and sistema.get("est") is not None and abs(sistema["est"] - c["est"]) > TOL_ESTACA_M:
                div.append({"campo": "estaca", "carimbo": regras.fmt_est(c["est"]), "sistema": regras.fmt_est(sistema["est"]),
                            "msg": f"Estaca do carimbo {regras.fmt_est(c['est'])}, mas a coordenada aponta "
                                   f"{regras.fmt_est(sistema['est'])} ({_m(abs(sistema['est'] - c['est']))} m de diferença)."})
        if sistema["dist"] is not None and sistema["dist"] <= lim and (sistema["rodovia"], sistema["lote"]) not in ELEGIVEIS:
            div.append({"campo": "segmento", "carimbo": None, "sistema": fmt_seg(sistema["rodovia"], sistema["lote"]),
                        "msg": f"A coordenada cai em {fmt_seg(sistema['rodovia'], sistema['lote'])}, fora dos segmentos do mapa."})

    # sugestão (só proposta; quem decide é o administrador) e classificação final
    sug, final = None, None
    if seg_c and seg_c in ELEGIVEIS and ponto_est:
        sug = {"rodovia": seg_c[0], "lote": seg_c[1], "est": float(c["est"]), "fonte": "estaca do carimbo"}
    elif sistema and sistema.get("est") is not None and (sistema["rodovia"], sistema["lote"]) in ELEGIVEIS \
            and sistema.get("dist") is not None and sistema["dist"] <= lim and (not seg_c or seg_c[1] == sistema["lote"]):
        # coordenada longe do eixo, ou num lote diferente do escrito na foto, não vira sugestão
        sug = {"rodovia": sistema["rodovia"], "lote": sistema["lote"], "est": round(sistema["est"]), "fonte": "coordenada da foto"}

    if not div:
        if seg_c and ponto_est:
            usa_coord = bool(sistema) and sistema.get("lote") == seg_c[1]
            lat, lon = (r["lat"], r["lon"]) if usa_coord else ponto_est
            final = {"rodovia": seg_c[0], "lote": seg_c[1], "est": float(c["est"]), "lat": lat, "lon": lon,
                     "fonte": "estaca e coordenada do carimbo" if usa_coord else "estaca do carimbo (sem coordenada)"}
        elif sistema and sistema["dist"] is not None and sistema["dist"] <= lim and \
                (sistema["rodovia"], sistema["lote"]) in ELEGIVEIS and (not seg_c or seg_c[1] == sistema["lote"]):
            final = {"rodovia": sistema["rodovia"], "lote": sistema["lote"], "est": sistema.get("est"),
                     "lat": r["lat"], "lon": r["lon"], "fonte": "coordenada"}
        else:
            falta = []
            if not seg_c:
                falta.append("rodovia e lote")
            if c["est"] is None:
                falta.append("estaca")
            if r.get("lat") is None:
                falta.append("coordenada")
            div.append({"campo": "dados", "carimbo": None, "sistema": None,
                        "msg": "Dados insuficientes para confirmar o segmento"
                               + (f" (não identificado: {', '.join(falta)})." if falta else ".")})
    return {"estado": "auto" if final and not div else "pendente", "carimbo": c, "sistema": sistema,
            "divergencias": div, "sugestao": sug, "final": final}
