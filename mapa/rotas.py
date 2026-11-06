"""Páginas e API. Toda rota exige login (exceto /login e /saude, que não mostra dados); rotas de administrador checam o papel no servidor."""
import json
import re
import secrets
from pathlib import Path

from flask import (Blueprint, Response, abort, current_app, g, jsonify, redirect, render_template, request,
                   send_file, send_from_directory, session, url_for)

from . import eixo as eixo_mod
from . import processamento, visao
from .db import agora, auditar, nova_versao, parametros, set_parametro
from .seguranca import (PAPEIS, SENHA_MIN, autenticar, eh_admin, hash_senha, iniciar_sessao, requer_admin,
                        requer_login, token_csrf)

bp = Blueprint("rotas", __name__)
RECURSOS = Path(__file__).resolve().parent / "static"
ID_FOTO = re.compile(r"^[0-9a-f]{16}$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def cfg():
    return current_app.config["MAPA"]


def _json():
    return request.get_json(silent=True) or {}


# ------------------------------------------------------------------ login / sessão
@bp.route("/login", methods=["GET", "POST"])
def login():
    erro = None
    proximo = request.values.get("proximo") or "/"
    if not proximo.startswith("/") or proximo.startswith("//"):
        proximo = "/"
    if request.method == "POST":
        u, erro = autenticar(request.form.get("email"), request.form.get("senha"))
        if u:
            iniciar_sessao(u)
            auditar(g.db, u["id"], "login")
            return redirect(proximo)
    elif g.get("usuario"):
        return redirect(proximo)
    return render_template("login.html", erro=erro, proximo=proximo, csrf=token_csrf()), (401 if erro else 200)


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("rotas.login"))


@bp.get("/recursos/<path:nome>")
@requer_login
def recursos(nome):
    resp = send_from_directory(RECURSOS, nome, max_age=300)
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


# ------------------------------------------------------------------ páginas
@bp.get("/")
@requer_login
def relatorio():
    return render_template("relatorio.html", usuario=g.usuario, admin=eh_admin(), csrf=token_csrf())


@bp.get("/admin")
@requer_admin
def admin():
    return render_template("admin.html", usuario=g.usuario, csrf=token_csrf())


@bp.get("/conta")
@requer_login
def conta():
    return render_template("conta.html", usuario=g.usuario, admin=eh_admin(), csrf=token_csrf(), senha_min=SENHA_MIN)


# ------------------------------------------------------------------ dados do relatório
@bp.get("/api/relatorio")
@requer_login
def api_relatorio():
    return jsonify(visao.relatorio(g.db, g.usuario["papel"]))


@bp.get("/api/versao")
@requer_login
def api_versao():
    v = g.db.execute("SELECT valor FROM parametros WHERE chave='versao_dados'").fetchone()["valor"]
    out = {"versao": v}
    if eh_admin():
        out["processando"] = g.db.execute(
            "SELECT COUNT(*) FROM envios WHERE estado IN ('aguardando','em_andamento')").fetchone()[0]
    return jsonify(out)


def _foto_permitida(foto_id):
    if not ID_FOTO.match(foto_id) or not visao.pode_ver_foto(g.db, g.usuario["papel"], foto_id):
        abort(404)  # 404 também para foto restrita: não revela que ela existe


@bp.get("/api/fotos/<foto_id>/miniatura")
@requer_login
def api_miniatura(foto_id):
    _foto_permitida(foto_id)
    r = g.db.execute("SELECT miniatura FROM fotos WHERE id=?", (foto_id,)).fetchone()
    if not r or not r["miniatura"]:
        abort(404)
    return Response(r["miniatura"], mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@bp.get("/api/fotos/<foto_id>/imagem")
@requer_login
def api_imagem(foto_id):
    _foto_permitida(foto_id)
    f = g.db.execute("SELECT envio_id, membro, miniatura FROM fotos WHERE id=?", (foto_id,)).fetchone()
    try:
        dados = processamento.imagem_da_foto(cfg(), g.db, f)
    except Exception:
        dados = None
    dados = dados or f["miniatura"]
    if not dados:
        abort(404)
    return Response(dados, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@bp.get("/api/exportar.csv")
@requer_login
def api_csv():
    filtros = {k: (request.args.get(k) or "").strip()[:40] for k in ("de", "ate", "mes", "lote", "dia")}
    # vários blocos: "blocos=2,3" (o antigo "bloco=2" continua aceito)
    filtros["blocos"] = [b.strip() for b in ((request.args.get("blocos") or request.args.get("bloco") or "")[:200]).split(",") if b.strip()]
    txt = visao.exportar_csv(g.db, g.usuario["papel"], filtros)
    return Response(txt, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="intervencoes.csv"'})


@bp.post("/api/conta/senha")
@requer_login
def api_senha():
    from werkzeug.security import check_password_hash
    d = _json()
    if not check_password_hash(g.usuario["senha_hash"], d.get("atual") or ""):
        return jsonify(erro="A senha atual não confere."), 400
    try:
        h = hash_senha(d.get("nova"))
    except ValueError as e:
        return jsonify(erro=str(e)), 400
    g.db.execute("UPDATE usuarios SET senha_hash=?, versao_sessao=versao_sessao+1 WHERE id=?", (h, g.usuario["id"]))
    auditar(g.db, g.usuario["id"], "alterou_senha")
    u = g.db.execute("SELECT * FROM usuarios WHERE id=?", (g.usuario["id"],)).fetchone()
    iniciar_sessao(u)  # mantém esta sessão; as outras caem
    return jsonify(ok=True, csrf=token_csrf())


# ------------------------------------------------------------------ administrador: envios
def _envio_json(r):
    d = dict(r)
    d.pop("arquivo", None)
    d.pop("sha256", None)
    return d


@bp.post("/api/admin/envios")
@requer_admin
def api_enviar():
    arq = request.files.get("arquivo")
    if not arq or not arq.filename:
        return jsonify(erro="Escolha um arquivo .zip para enviar."), 400
    if not arq.filename.lower().endswith(".zip"):
        return jsonify(erro="Envie um arquivo .zip."), 400
    status, corpo = processamento.receber_envio(g.db, cfg(), arq, arq.filename, g.usuario["id"])
    if status == 202:
        auditar(g.db, g.usuario["id"], "enviou_zip", {"envio_id": corpo["envio_id"], "nome": arq.filename})
        w = current_app.extensions.get("mapa_worker")
        if w:
            w.acordar.set()
    return jsonify(corpo), status


@bp.get("/api/admin/envios")
@requer_admin
def api_envios():
    rows = g.db.execute("SELECT e.*, u.email AS enviado_por_email FROM envios e LEFT JOIN usuarios u ON u.id=e.enviado_por "
                        "ORDER BY e.id DESC LIMIT 100").fetchall()
    return jsonify(envios=[_envio_json(r) for r in rows])


@bp.get("/api/admin/envios/<int:envio_id>")
@requer_admin
def api_envio(envio_id):
    r = g.db.execute("SELECT * FROM envios WHERE id=?", (envio_id,)).fetchone()
    if not r:
        abort(404)
    return jsonify(_envio_json(r))


# ------------------------------------------------------------------ administrador: ocorrências e classificação
@bp.get("/api/admin/ocorrencias")
@requer_admin
def api_ocorrencias():
    resolvidas = request.args.get("resolvidas") == "1"
    rows = g.db.execute(
        "SELECT o.*, f.arquivo, f.data, e.lote FROM ocorrencias o LEFT JOIN fotos f ON f.id=o.foto_id "
        "LEFT JOIN envios e ON e.id=o.envio_id WHERE o.resolvida=? ORDER BY o.id DESC LIMIT 500",
        (1 if resolvidas else 0,)).fetchall()
    total = g.db.execute("SELECT COUNT(*) FROM ocorrencias WHERE resolvida=0").fetchone()[0]
    return jsonify(ocorrencias=[dict(r) for r in rows], abertas=total)


@bp.post("/api/admin/ocorrencias/<int:oid>/resolver")
@requer_admin
def api_resolver(oid):
    cur = g.db.execute("UPDATE ocorrencias SET resolvida=1, resolvida_por=?, resolvida_em=? WHERE id=? AND resolvida=0",
                       (g.usuario["id"], agora(), oid))
    if not cur.rowcount:
        abort(404)
    return jsonify(ok=True)


@bp.get("/api/admin/fotos")
@requer_admin
def api_fotos_admin():
    classe = request.args.get("classe", "nao_classificada")
    lote = request.args.get("lote") or ""
    try:
        inicio = max(0, int(request.args.get("inicio", 0) or 0))
    except ValueError:
        inicio = 0
    cond, args = [], []
    if classe == "nao_classificada":
        cond.append("classe IS NULL")
    elif classe in ("antes", "depois", "outro"):
        cond.append("classe=?")
        args.append(classe)
    if lote:
        cond.append("lote=?")
        args.append(lote)
    where = ("WHERE " + " AND ".join(cond)) if cond else ""
    total = g.db.execute(f"SELECT COUNT(*) FROM fotos {where}", args).fetchone()[0]
    rows = g.db.execute(f"SELECT id, arquivo, lote, data, lat, lon, classe, motivo_exclusao, classificado_em FROM fotos {where} "
                        "ORDER BY data, arquivo, id LIMIT 60 OFFSET ?", args + [inicio]).fetchall()
    lotes = [r[0] for r in g.db.execute("SELECT DISTINCT lote FROM fotos ORDER BY lote")]
    return jsonify(fotos=[dict(r) for r in rows], total=total, inicio=inicio, lotes=lotes)


@bp.post("/api/admin/fotos/<foto_id>/classe")
@requer_admin
def api_classificar(foto_id):
    if not ID_FOTO.match(foto_id):
        abort(404)
    d = _json()
    classe = d.get("classe")
    if classe not in ("antes", "depois", "outro", None):
        return jsonify(erro="Classe inválida."), 400
    motivo = (d.get("motivo") or "").strip()[:200] or None
    if classe == "outro" and not motivo:
        motivo = "não relacionada ao tapa-buraco"
    cur = g.db.execute("UPDATE fotos SET classe=?, motivo_exclusao=?, classificado_por=?, classificado_em=? WHERE id=?",
                       (classe, motivo if classe == "outro" else None, g.usuario["id"], agora(), foto_id))
    if not cur.rowcount:
        abort(404)
    nova_versao(g.db)
    visao.invalidar()
    auditar(g.db, g.usuario["id"], "classificou_foto", {"foto": foto_id, "classe": classe})
    # devolve o que ficou gravado, para a tela só confirmar depois de ler do banco
    salvo = g.db.execute("SELECT classe, motivo_exclusao, classificado_em FROM fotos WHERE id=?", (foto_id,)).fetchone()
    return jsonify(ok=True, **dict(salvo))


# ------------------------------------------------------------------ administrador: parâmetros e eixo
EDITAVEIS = {"limite_anomalia_m": (30, 5000), "gps_travado_ativo": (0, 1), "nao_classificadas_contam": (0, 1)}


@bp.route("/api/admin/parametros", methods=["GET", "POST"])
@requer_admin
def api_parametros():
    if request.method == "POST":
        d = _json()
        for k, (lo, hi) in EDITAVEIS.items():
            if k in d:
                try:
                    v = int(d[k])
                except (TypeError, ValueError):
                    return jsonify(erro=f"Valor inválido para {k}."), 400
                if not lo <= v <= hi:
                    return jsonify(erro=f"{k} deve ficar entre {lo} e {hi}."), 400
                set_parametro(g.db, k, v)
        nova_versao(g.db)
        visao.invalidar()
        auditar(g.db, g.usuario["id"], "alterou_parametros", d)
    p = parametros(g.db)
    return jsonify({k: p[k] for k in EDITAVEIS})


@bp.get("/api/admin/eixo")
@requer_admin
def api_eixo():
    rows = g.db.execute("SELECT bloco, rodovia, fonte, pts, atualizado_em FROM eixo_blocos ORDER BY bloco").fetchall()
    out = []
    for r in rows:
        pts = json.loads(r["pts"])
        est = [p[2] for p in pts if p[2] is not None]
        out.append({"bloco": r["bloco"], "rodovia": r["rodovia"], "fonte": r["fonte"], "pontos": len(pts),
                    "inicio": min(est) if est else None, "fim": max(est) if est else None, "atualizado_em": r["atualizado_em"]})
    return jsonify(blocos=out)


@bp.post("/api/admin/eixo")
@requer_admin
def api_eixo_enviar():
    arq = request.files.get("arquivo")
    if not arq or not arq.filename.lower().endswith((".kmz", ".kml")):
        return jsonify(erro="Envie um arquivo .kmz ou .kml."), 400
    dados = arq.stream.read(eixo_mod.MAX_KML + 1)
    if len(dados) > eixo_mod.MAX_KML:
        return jsonify(erro="Arquivo grande demais."), 400
    try:
        blocos = eixo_mod.blocos_do_kml(eixo_mod.ler_kml_bytes(arq.filename, dados), arq.filename[:120])
    except eixo_mod.KmzInvalido as e:
        return jsonify(erro=str(e)), 400
    for bid, b in blocos.items():
        g.db.execute("INSERT OR REPLACE INTO eixo_blocos(bloco, rodovia, fonte, pts, atualizado_em) VALUES(?,?,?,?,?)",
                     (bid, b["rodovia"], b["fonte"], json.dumps(b["pts"]), agora()))
    nova_versao(g.db)
    visao.invalidar()
    auditar(g.db, g.usuario["id"], "carregou_eixo", {"blocos": list(blocos)})
    return jsonify(blocos=[{"bloco": k, "pontos": len(v["pts"])} for k, v in blocos.items()])


@bp.delete("/api/admin/eixo/<bloco>")
@requer_admin
def api_eixo_remover(bloco):
    cur = g.db.execute("DELETE FROM eixo_blocos WHERE bloco=?", (bloco,))
    if not cur.rowcount:
        abort(404)
    nova_versao(g.db)
    visao.invalidar()
    auditar(g.db, g.usuario["id"], "removeu_bloco", {"bloco": bloco})
    return jsonify(ok=True)


# ------------------------------------------------------------------ administrador: usuários
def _usuario_json(r):
    return {k: r[k] for k in ("id", "email", "nome", "papel", "ativo", "criado_em", "ultimo_login")}


@bp.route("/api/admin/usuarios", methods=["GET", "POST"])
@requer_admin
def api_usuarios():
    if request.method == "POST":
        d = _json()
        email = (d.get("email") or "").strip().lower()
        papel = d.get("papel") or "visualizador"
        if not EMAIL.match(email):
            return jsonify(erro="E-mail inválido."), 400
        if papel not in PAPEIS:
            return jsonify(erro="Papel inválido."), 400
        if g.db.execute("SELECT 1 FROM usuarios WHERE email=?", (email,)).fetchone():
            return jsonify(erro="Já existe um usuário com esse e-mail."), 409
        senha = secrets.token_urlsafe(12)
        g.db.execute("INSERT INTO usuarios(email, nome, senha_hash, papel, criado_em) VALUES(?,?,?,?,?)",
                     (email, (d.get("nome") or "").strip()[:100], hash_senha(senha), papel, agora()))
        auditar(g.db, g.usuario["id"], "criou_usuario", {"email": email, "papel": papel})
        return jsonify(ok=True, senha_temporaria=senha), 201
    rows = g.db.execute("SELECT * FROM usuarios ORDER BY papel, email").fetchall()
    return jsonify(usuarios=[_usuario_json(r) for r in rows], eu=g.usuario["id"])


@bp.post("/api/admin/usuarios/<int:uid>")
@requer_admin
def api_usuario(uid):
    u = g.db.execute("SELECT * FROM usuarios WHERE id=?", (uid,)).fetchone()
    if not u:
        abort(404)
    d = _json()
    resp = {"ok": True}
    if uid == g.usuario["id"] and (("ativo" in d and not d["ativo"]) or ("papel" in d and d["papel"] != "admin")):
        return jsonify(erro="Você não pode desativar nem rebaixar a sua própria conta."), 400
    if "papel" in d:
        if d["papel"] not in PAPEIS:
            return jsonify(erro="Papel inválido."), 400
        g.db.execute("UPDATE usuarios SET papel=?, versao_sessao=versao_sessao+1 WHERE id=?", (d["papel"], uid))
    if "ativo" in d:
        g.db.execute("UPDATE usuarios SET ativo=?, versao_sessao=versao_sessao+1 WHERE id=?", (1 if d["ativo"] else 0, uid))
    if d.get("redefinir_senha"):
        senha = secrets.token_urlsafe(12)
        g.db.execute("UPDATE usuarios SET senha_hash=?, versao_sessao=versao_sessao+1 WHERE id=?", (hash_senha(senha), uid))
        resp["senha_temporaria"] = senha
    auditar(g.db, g.usuario["id"], "alterou_usuario", {"id": uid, **{k: v for k, v in d.items() if k != "senha"}})
    return jsonify(resp)


# ------------------------------------------------------------------ saúde, backup e restauração
@bp.get("/saude")
def saude():
    """Verificação da plataforma de hospedagem: sem login e sem nenhum dado do sistema."""
    try:
        g.db.execute("SELECT 1").fetchone()
        return jsonify(ok=True)
    except Exception:  # noqa: BLE001
        current_app.logger.exception("verificação de saúde falhou")
        return jsonify(ok=False), 503


@bp.get("/api/admin/backup")
@requer_admin
def api_backup():
    from . import backup
    c = cfg()
    destino = c.TMP_DIR / f"backup-download-{secrets.token_hex(6)}.zip"
    for antigo in c.TMP_DIR.glob("backup-download-*.zip"):   # limpa downloads anteriores
        try:
            antigo.unlink()
        except OSError:
            pass
    resumo = backup.criar_backup(c, destino)
    auditar(g.db, g.usuario["id"], "baixou_backup", resumo["contagens"])
    nome = f"mapa-backup-{agora()[:16].replace(':', '').replace('T', '-')}.zip"
    return send_file(destino, as_attachment=True, download_name=nome, mimetype="application/zip", max_age=0)


@bp.get("/api/admin/restaurar")
@requer_admin
def api_restaurar_estado():
    from . import backup
    return jsonify(permitido=backup.sistema_vazio(g.db))


@bp.post("/api/admin/restaurar")
@requer_admin
def api_restaurar():
    from . import backup
    arq = request.files.get("arquivo")
    if not arq or not arq.filename.lower().endswith(".zip"):
        return jsonify(erro="Escolha o arquivo .zip de backup."), 400
    c = cfg()
    tmp = c.TMP_DIR / f"restaurar-upload-{secrets.token_hex(6)}.zip"
    try:
        arq.save(tmp)
        resumo = backup.restaurar_backup(c, tmp)
    except backup.BackupInvalido as e:
        return jsonify(erro=str(e)), 400
    finally:
        if tmp.exists():
            tmp.unlink()
    visao.invalidar()
    processamento.retomar_pendentes(g.db)
    session.clear()
    current_app.logger.info("Backup restaurado: %s", resumo.get("contagens"))
    return jsonify(ok=True, contagens=resumo.get("contagens"),
                   mensagem="Backup restaurado. Entre de novo com a sua conta (a mesma senha do computador).")
