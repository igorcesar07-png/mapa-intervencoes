"""O que cada papel pode ver. Toda a filtragem por papel acontece aqui, no servidor."""
import csv
import io
import json
import threading

from . import regras
from .datas import data_valida
from .db import parametros

_cache = {"versao": None, "calc": None, "fotos": None, "blocos": None}
_lock = threading.Lock()

# Sufixo gravado em revisao_foto.fonte quando quem informou a posição foi o administrador
# (ver rotas.api_revisar). É o que separa "o sistema leu" de "o administrador decidiu".
FONTE_ADMIN = "informada pelo administrador"

CAMPOS_FOTO = ("id", "arquivo", "lote", "data", "data_fonte", "lat", "lon", "coord_fonte",
               "km", "rodovia", "local", "sentido", "classe", "motivo_exclusao", "extras", "envio_id")
# Campos que o visualizador recebe de uma foto válida
CAMPOS_PUBLICOS = ("id", "arquivo", "lote", "data", "data_fonte", "lat", "lon", "km", "rodovia", "local", "sentido", "empresa")


def carregar_blocos(con):
    return {r["bloco"]: {"rodovia": r["rodovia"], "fonte": r["fonte"], "pts": json.loads(r["pts"])}
            for r in con.execute("SELECT * FROM eixo_blocos ORDER BY bloco")}


def calculo(con):
    """Regras aplicadas sobre todos os dados; recalcula só quando a versão dos dados muda."""
    p = parametros(con)
    versao = p["versao_dados"]
    with _lock:
        if _cache["versao"] == versao:
            return _cache
        decisoes = {r["foto_id"]: dict(r) for r in con.execute("SELECT * FROM decisoes_foto")}
        revisoes = {r["foto_id"]: dict(r) for r in con.execute("SELECT * FROM revisao_foto")}
        empresas = {r["id"]: r["empresa"] for r in con.execute("SELECT id, empresa FROM envios")}
        fotos, excluidas, em_revisao = [], [], set()
        for r in con.execute(f"SELECT {', '.join(CAMPOS_FOTO)}, importado_em FROM fotos"):
            f = dict(r)
            f["extras"] = json.loads(f["extras"] or "{}")
            if f["data"] and not data_valida(f["data"], f.pop("importado_em")):
                f["extras"]["data_invalida"], f["data"], f["data_fonte"] = f["data"], None, None
            f.pop("importado_em", None)
            f["empresa"] = empresas.get(f["envio_id"])
            rv = revisoes.get(f["id"])
            if rv and rv["estado"] in ("aguardando", "pendente"):
                em_revisao.add(f["id"])     # importada e ainda não confirmada: fora do mapa
                continue
            if rv and rv["estado"] == "confirmada" and rv["lat"] is not None:
                if (rv["lat"], rv["lon"]) != (f["lat"], f["lon"]):
                    f["coord_original"] = [f["lat"], f["lon"]]
                    f["lat"], f["lon"] = rv["lat"], rv["lon"]
                f["fixa_bloco"], f["fixa_est"] = rv["lote"], rv["est"]
                if (rv["fonte"] or "").endswith(FONTE_ADMIN):
                    # o administrador corrigiu rodovia/lote/estaca (ou a coordenada) e confirmou a inclusão
                    f["tratada"] = {"como": "revisao", "fonte": rv["fonte"], "quando": rv["quando"],
                                    "bloco": rv["lote"], "est": rv["est"]}
            if rv and rv["estado"] == "divergente":
                f["divergencias"] = json.loads(rv["divergencias"] or "[]")
            dec = decisoes.get(f["id"])
            if dec and dec["excluida"]:
                # excluída: fora de tudo (mapa, listas, relatórios, exportações); fica no banco para restaurar
                excluidas.append({"id": f["id"], "arquivo": f["arquivo"], "lote": f["lote"], "data": f["data"],
                                  "motivo": dec["motivo"], "quando": dec["quando"]})
                continue
            if dec and dec["decisao"] == "incorporar" and dec["lat"] is not None:
                # o registro original não muda: a posição escolhida vale só para o cálculo e o mapa
                f["coord_original"] = [f["lat"], f["lon"]]
                f["lat"], f["lon"] = dec["lat"], dec["lon"]
                f["decisao"], f["dec_est"], f["dec_bloco"] = "incorporar", dec["est"], dec["bloco"]
                f["tratada"] = {"como": "incorporada", "fonte": f"estaca {FONTE_ADMIN}", "quando": dec["quando"],
                                "bloco": dec["bloco"], "est": dec["est"]}
            elif dec and dec["decisao"] == "remover":
                f["decisao"] = "remover"
            fotos.append(f)
        blocos = carregar_blocos(con)
        calc = regras.calcular(fotos, blocos, p)
        calc["visiveis"] = {gp["rep"] for gp in calc["grupos"] if gp["tapado"]}
        _cache.update(versao=versao, calc=calc, fotos={f["id"]: f for f in fotos}, blocos=blocos, params=p,
                      excluidas=sorted(excluidas, key=lambda e: e["quando"] or "", reverse=True), em_revisao=em_revisao)
        return _cache


def invalidar():
    with _lock:
        _cache["versao"] = None


def pode_ver_foto(con, papel, foto_id):
    c = calculo(con)
    if foto_id not in c["fotos"]:
        # foto excluída: só o administrador vê (para conferir e restaurar)
        return papel == "admin" and (foto_id in (c.get("em_revisao") or set())
                                     or any(e["id"] == foto_id for e in c.get("excluidas") or []))
    return papel == "admin" or foto_id in c["calc"]["visiveis"]


def _deriv_publico(d):
    return {k: d.get(k) for k in ("bloco", "est", "dist", "grupo") if d.get(k) is not None}


def relatorio(con, papel):
    c = calculo(con)
    calc, fotos, p = c["calc"], c["fotos"], c["params"]
    eixo = {b: {"rodovia": v["rodovia"], "pts": v["pts"]} for b, v in c["blocos"].items()}
    base = {"papel": papel, "versao": c["versao"], "eixo": eixo, "tem_estacas": calc["tem_estacas"]}
    if papel == "admin":
        regs = []
        for f in fotos.values():
            r = {k: f[k] for k in CAMPOS_FOTO if k != "extras"}
            r.update({k: v for k, v in f["extras"].items() if k != "thumb"})
            for k in ("decisao", "coord_original", "empresa", "divergencias", "tratada"):
                if f.get(k) is not None:
                    r[k] = f[k]
            r["d"] = calc["deriv"][f["id"]]
            regs.append(r)
        n_rev = {k: v for k, v in con.execute("SELECT estado, COUNT(*) FROM revisao_foto WHERE estado != 'confirmada' GROUP BY estado")}
        base.update(registros=regs, grupos=calc["grupos"], excluidas=c["excluidas"], revisao=n_rev,
                    parametros={k: p[k] for k in ("limite_anomalia_m", "gps_travado_ativo", "gps_travado_min",
                                                  "isolado_m", "agrupa_m", "nao_classificadas_contam")})
        return base
    # visualizador: 1 foto (a do buraco tapado) por buraco válido; nada de inconsistências (anomalias),
    # fotos excluídas, buracos sem tapado, fotos extras do mesmo local, duplicatas ou sem coordenada
    regs, grupos = [], []
    for gp in calc["grupos"]:
        if not gp["tapado"]:
            continue
        f = fotos[gp["rep"]]
        r = {k: f[k] for k in CAMPOS_PUBLICOS}
        r["d"] = _deriv_publico(calc["deriv"][f["id"]])
        regs.append(r)
        grupos.append({"id": gp["id"], "lat": gp["lat"], "lon": gp["lon"], "dia": gp["dia"], "tapado": True,
                       "rep": gp["rep"], "fotos": [gp["rep"]]})
    base.update(registros=regs, grupos=grupos,
                parametros={"limite_anomalia_m": p["limite_anomalia_m"]})
    return base


# ------------------------------------------------------------------ filtros e CSV
def passa_filtro(r, d, f):
    if f.get("lote") and r["lote"] != f["lote"]:
        return False
    blocos = f.get("blocos") or ([f["bloco"]] if f.get("bloco") else [])
    if blocos and d.get("bloco") not in blocos:
        return False
    dia = (r["data"] or "")[:10]
    if any(f.get(k) for k in ("de", "ate", "mes", "dia")) and not dia:
        return False
    if f.get("de") and dia < f["de"]:
        return False
    if f.get("ate") and dia > f["ate"]:
        return False
    if f.get("mes") and dia[:7] != f["mes"]:
        return False
    if f.get("dia") and dia != f["dia"]:
        return False
    return True


def _num(v, casas=None):
    if v is None:
        return ""
    if casas is not None:
        v = round(v, casas)
    return str(v).replace(".", ",")


def exportar_csv(con, papel, filtros):
    c = calculo(con)
    calc, fotos = c["calc"], c["fotos"]
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\r\n")
    base_cab = ["arquivo", "data", "fonte_data", "latitude", "longitude", "bloco", "estaca", "km", "rodovia",
                "local", "sentido", "lote", "dist_eixo_m", "empresa"]
    rodovia_bloco = {b: v["rodovia"] for b, v in c["blocos"].items()}

    def linha(f, d):
        return [f["arquivo"], (f["data"] or "").replace("T", " "), f["data_fonte"] or "", _num(f["lat"]), _num(f["lon"]),
                d.get("bloco") or "", regras.fmt_est(d["est"]) if d.get("est") is not None else "",
                _num(d["est"] / 1000, 3) if d.get("est") is not None else "",
                rodovia_bloco.get(d.get("bloco")) or f["rodovia"] or "", f["local"] or "", f["sentido"] or "",
                f["lote"] or "", round(d["dist"]) if d.get("dist") is not None else "", f.get("empresa") or ""]

    if papel == "admin":
        grupo_tapado = {gp["id"]: gp["tapado"] for gp in calc["grupos"]}
        w.writerow(base_cab + ["classe", "situacao", "inconsistencia", "motivo", "duplicata", "buraco", "decisao_admin",
                               "tratada_pelo_admin"])
        for f in sorted(fotos.values(), key=lambda f: (f["data"] or "9999", f["arquivo"])):
            d = calc["deriv"][f["id"]]
            if not passa_filtro(f, d, filtros):
                continue
            if f["lat"] is None:
                sit = "sem coordenada"
            elif d["dup"]:
                sit = "duplicata"
            elif f["classe"] == "outro" and not d.get("incorporada"):
                sit = "não relacionada"
            elif d.get("removida"):
                sit = "removida do mapa de calor"
            elif d["anomalia"]:
                sit = "inconsistência"
            elif d.get("grupo") and not grupo_tapado.get(d["grupo"]):
                sit = "buraco sem foto de tapado"
            elif d.get("rep"):
                sit = "válida"
            else:
                sit = "foto extra do mesmo buraco"
            tr = f.get("tratada") or {}
            w.writerow(linha(f, d) + [f["classe"] or "", sit, "sim" if d["anomalia"] else "não", d["motivo"] or "",
                                      "sim" if d["dup"] else "", d.get("grupo") or "",
                                      "incorporada na estaca" if d.get("incorporada") else "removida do mapa de calor" if d.get("removida") else "",
                                      f"{tr['fonte']} em {(tr['quando'] or '').replace('T', ' ')}" if tr else ""])
    else:
        w.writerow(base_cab)
        for gp in calc["grupos"]:
            if not gp["tapado"]:
                continue
            f, d = fotos[gp["rep"]], calc["deriv"][gp["rep"]]
            if passa_filtro(f, d, filtros):
                w.writerow(linha(f, d))
    return "﻿" + out.getvalue()
