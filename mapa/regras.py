"""Regras do relatório, portadas do index.html sem mudar critérios.

- duplicata: mesma foto reenviada (mesmo carimbo de data/hora e mesma posição);
- eixo/estaca: projeção da foto na estaca mais próxima do KMZ do bloco;
- anomalia: distância ao eixo > limite (padrão 150 m), ou GPS repetido
  (coordenada idêntica em fotos com >= 5 min de diferença); sem eixo,
  foto a mais de 5 km de qualquer outra;
- buraco (grupo): fotos válidas do mesmo dia a <= 25 m do centro do grupo;
- buraco tapado: o grupo tem foto classificada "depois" (tapado) ou foto
  sem classificação (regra atual para lotes não classificados).
"""
import math

import numpy as np

K = 111320.0


def dist_m(a, b):
    dy = (a[0] - b[0]) * K
    dx = (a[1] - b[1]) * K * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


# ------------------------------------------------------------------ duplicatas (marcarDuplicatas)
def marcar_duplicatas(regs):
    dup, vistos = {}, []
    for r in regs:
        if not r["data"] or r["data_fonte"] not in ("carimbo", "exif"):
            continue
        seg = not r["data"].endswith(":00")
        achou = None
        for o in vistos:
            if o["data"] != r["data"]:
                continue
            if r["lat"] is None or o["lat"] is None:
                mesmo = r["lat"] is None and o["lat"] is None and seg
            else:
                mesmo = dist_m((r["lat"], r["lon"]), (o["lat"], o["lon"])) < (30 if seg else 2)
            if mesmo:
                achou = o
                break
        if achou:
            dup[r["id"]] = achou["id"]
        else:
            vistos.append(r)
    return dup


# ------------------------------------------------------------------ eixo
def linhas_eixo(blocos):
    """Cada bloco vira uma ou mais polilinhas (quebra onde há buraco > 250 m entre pontos)."""
    out = []
    for bid, b in blocos.items():
        cur = []
        for p in b["pts"]:
            if cur and dist_m(cur[-1], p) > 250:
                if len(cur) > 1:
                    out.append((bid, cur))
                cur = []
            cur.append(p)
        if len(cur) > 1:
            out.append((bid, cur))
    linhas = []
    for bid, pts in out:
        a = np.array([[p[0], p[1], np.nan if p[2] is None else p[2]] for p in pts], dtype=float)
        linhas.append({"bloco": bid, "la": a[:, 0], "lo": a[:, 1], "est": a[:, 2],
                       "bb": (a[:, 0].min() - .05, a[:, 0].max() + .05, a[:, 1].min() - .05, a[:, 1].max() + .05)})
    return linhas


def _proj(lat, lon, ln):
    cl = math.cos(math.radians(lat))
    ax = (ln["lo"][:-1] - lon) * K * cl
    ay = (ln["la"][:-1] - lat) * K
    bx = (ln["lo"][1:] - lon) * K * cl
    by = (ln["la"][1:] - lat) * K
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    with np.errstate(invalid="ignore", divide="ignore"):
        t = np.where(l2 > 0, -(ax * dx + ay * dy) / l2, 0.0)
    t = np.clip(t, 0, 1)
    d = np.hypot(ax + t * dx, ay + t * dy)
    i = int(np.argmin(d))
    return float(d[i]), i, float(t[i])


def projetar(r, linhas):
    best = None
    for ln in linhas:
        bb = ln["bb"]
        if best and not (bb[0] <= r["lat"] <= bb[1] and bb[2] <= r["lon"] <= bb[3]):
            continue
        d, i, t = _proj(r["lat"], r["lon"], ln)
        if best is None or d < best[0]:
            best = (d, i, t, ln)
    if not best:
        return {"dist": None}
    d, i, t, ln = best
    out = {"dist": d, "bloco": ln["bloco"]}
    e0, e1 = ln["est"][i], ln["est"][min(i + 1, len(ln["est"]) - 1)]
    if not math.isnan(e0) and not math.isnan(e1):
        out["est"] = float(e0 + t * (e1 - e0))
    return out


def estaca_para_ponto(blocos, bloco, est):
    """Ponto (lat, lon) do eixo do bloco na estaca (em metros), interpolando entre as estacas do KMZ.
    None se o bloco não existe, não tem estacas ou a estaca está fora do trecho (mais de 50 m além das pontas)."""
    b = blocos.get(str(bloco))
    if not b:
        return None
    pts = sorted((p for p in b["pts"] if p[2] is not None), key=lambda p: p[2])
    if not pts or est < pts[0][2] - 50 or est > pts[-1][2] + 50:
        return None
    for a, c in zip(pts, pts[1:]):
        if a[2] <= est <= c[2]:
            t = 0.0 if c[2] == a[2] else (est - a[2]) / (c[2] - a[2])
            return (a[0] + t * (c[0] - a[0]), a[1] + t * (c[1] - a[1]))
    q = pts[0] if est < pts[0][2] else pts[-1]
    return (q[0], q[1])


# ------------------------------------------------------------------ cálculo completo
def calcular(fotos, blocos, params):
    """fotos: lista de dicts (linhas da tabela). Retorna derivados por foto e grupos."""
    lim = float(params.get("limite_anomalia_m", 150))
    travado_on = params.get("gps_travado_ativo", "1") == "1"
    travado_min = float(params.get("gps_travado_min", 5))
    isolado = float(params.get("isolado_m", 5000))
    agrupa = float(params.get("agrupa_m", 25))
    nc_contam = params.get("nao_classificadas_contam", "1") == "1"

    regs = sorted(fotos, key=lambda r: ((r["data"] or "9999"), r["arquivo"]))
    dup = marcar_duplicatas(regs)
    linhas = linhas_eixo(blocos)
    tem_eixo = bool(linhas)
    der = {}
    com = [r for r in regs if r["lat"] is not None]
    unicos = [r for r in com if r["id"] not in dup]

    for r in regs:
        d = {"dup": dup.get(r["id"]), "anomalia": False, "motivo": None, "travado": False}
        if r["lat"] is not None and tem_eixo:
            d.update(projetar(r, linhas))
        if r.get("fixa_bloco") is not None and r.get("fixa_est") is not None:
            # classificação confirmada pelo administrador na revisão da importação
            d.update(bloco=str(r["fixa_bloco"]), est=float(r["fixa_est"]))
        dec = r.get("decisao")
        if dec == "incorporar":
            # posição escolhida pelo administrador: ponto normal, na estaca e no bloco que ele definiu
            d.update(est=r["dec_est"], bloco=r["dec_bloco"], incorporada=True)
            if d.get("dist") is not None:
                d["dist"] = min(d["dist"], 1.0)
        elif dec == "remover":
            d["removida"] = True
        der[r["id"]] = d

    # GPS travado: mesma coordenada exata em fotos com minutos de diferença
    por_coord = {}
    for r in unicos:
        por_coord.setdefault(f"{r['lat']:.6f},{r['lon']:.6f}", []).append(r)
    travados = set()
    import datetime as dt
    for grp in por_coord.values():
        ts = []
        for r in grp:
            try:
                ts.append(dt.datetime.fromisoformat(r["data"]).timestamp())
            except (TypeError, ValueError):
                pass
        if len(ts) >= 2 and max(ts) - min(ts) >= travado_min * 60:
            travados.update(r["id"] for r in grp)

    for r in com:
        d = der[r["id"]]
        if d.get("incorporada") or d.get("removida"):
            continue   # o administrador já decidiu: não é mais inconsistência
        motivos = []
        if tem_eixo:
            if d.get("dist") is not None and d["dist"] > lim:
                motivos.append(f"{d['dist']:,.0f} m do eixo".replace(",", "."))
        else:
            nn = min((dist_m((r["lat"], r["lon"]), (o["lat"], o["lon"])) for o in unicos
                      if o is not r and o["id"] != d["dup"]), default=float("inf"))
            if nn > isolado:
                motivos.append(f"{nn / 1000:.1f} km da foto mais próxima".replace(".", ","))
        d["travado"] = r["id"] in travados or (d["dup"] in travados)
        if d["travado"] and travado_on:
            motivos.append("GPS repetido em horários diferentes")
        d["anomalia"] = bool(motivos)
        d["motivo"] = " · ".join(motivos) or None

    # buracos: fotos válidas do mesmo dia a <= 25 m do centro do grupo
    validas = [r for r in com if not der[r["id"]]["dup"] and r["classe"] != "outro" and not der[r["id"]]["anomalia"]
               and not der[r["id"]].get("removida")]
    grupos = []
    for r in sorted(validas, key=lambda r: r["data"] or ""):
        dia = (r["data"] or "")[:10]
        alvo = next((gp for gp in grupos if gp["dia"] == dia and dist_m((gp["lat"], gp["lon"]), (r["lat"], r["lon"])) <= agrupa), None)
        if alvo:
            alvo["fotos"].append(r)
            alvo["lat"] = sum(f["lat"] for f in alvo["fotos"]) / len(alvo["fotos"])
            alvo["lon"] = sum(f["lon"] for f in alvo["fotos"]) / len(alvo["fotos"])
        else:
            grupos.append({"dia": dia, "lat": r["lat"], "lon": r["lon"], "fotos": [r]})
    saida = []
    for gp in grupos:
        tapado = any(f["classe"] == "depois" or (f["classe"] is None and nc_contam) for f in gp["fotos"])
        rep = next((f for f in gp["fotos"] if f["classe"] == "depois"), gp["fotos"][0])
        gid = "g" + rep["id"]
        for f in gp["fotos"]:
            der[f["id"]]["grupo"] = gid
            der[f["id"]]["rep"] = f is rep
        saida.append({"id": gid, "lat": gp["lat"], "lon": gp["lon"], "dia": gp["dia"], "tapado": tapado,
                      "rep": rep["id"], "fotos": [f["id"] for f in gp["fotos"]]})
    return {"deriv": der, "grupos": saida, "tem_eixo": tem_eixo,
            "tem_estacas": tem_eixo and all(not math.isnan(ln["est"][0]) for ln in linhas)}


def fmt_est(m):
    m = int(round(m / 10.0)) * 10
    return f"{m // 1000}+{m % 1000:03d}"
