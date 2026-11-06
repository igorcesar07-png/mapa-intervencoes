"""Autenticação, autorização por papel, CSRF, limite de tentativas e cabeçalhos de segurança."""
import datetime as dt
import functools
import hmac
import secrets

from flask import abort, current_app, g, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import agora

PAPEIS = ("admin", "visualizador")
SENHA_MIN = 10
# Hash de uma senha qualquer, usado para gastar o mesmo tempo quando o e-mail não existe
_HASH_FALSO = generate_password_hash(secrets.token_hex(16))


def hash_senha(senha):
    if not senha or len(senha) < SENHA_MIN:
        raise ValueError(f"A senha precisa ter pelo menos {SENHA_MIN} caracteres.")
    return generate_password_hash(senha)


# ------------------------------------------------------------------ usuário da requisição
def carregar_usuario():
    g.usuario = None
    uid, sv = session.get("uid"), session.get("sv")
    if not uid:
        return
    u = g.db.execute("SELECT * FROM usuarios WHERE id=?", (uid,)).fetchone()
    # conta desativada ou senha trocada derrubam sessões antigas
    if not u or not u["ativo"] or u["versao_sessao"] != sv:
        session.clear()
        return
    g.usuario = u


def eh_admin():
    return bool(g.get("usuario")) and g.usuario["papel"] == "admin"


def _negar_nao_autenticado():
    if request.path.startswith("/api/"):
        return jsonify(erro="Faça login para continuar."), 401
    return redirect(url_for("rotas.login", proximo=request.path))


def requer_login(f):
    @functools.wraps(f)
    def w(*a, **k):
        if not g.get("usuario"):
            return _negar_nao_autenticado()
        return f(*a, **k)
    return w


def requer_admin(f):
    @functools.wraps(f)
    def w(*a, **k):
        if not g.get("usuario"):
            return _negar_nao_autenticado()
        if g.usuario["papel"] != "admin":
            if request.path.startswith("/api/"):
                return jsonify(erro="Acesso restrito ao administrador."), 403
            abort(403)
        return f(*a, **k)
    return w


# ------------------------------------------------------------------ CSRF
def token_csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def verificar_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    enviado = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    esperado = session.get("csrf") or ""
    if not esperado or not hmac.compare_digest(enviado, esperado):
        if request.path.startswith("/api/"):
            return jsonify(erro="Sessão expirada ou requisição inválida. Recarregue a página."), 403
        abort(403)


# ------------------------------------------------------------------ login com limite de tentativas
def _bloqueado(con, chave):
    r = con.execute("SELECT bloqueado_ate FROM tentativas_login WHERE chave=?", (chave,)).fetchone()
    return bool(r and r["bloqueado_ate"] and r["bloqueado_ate"] > agora())


def _falha(con, chave, cfg):
    r = con.execute("SELECT falhas, primeira FROM tentativas_login WHERE chave=?", (chave,)).fetchone()
    janela = (dt.datetime.now() - dt.timedelta(minutes=cfg.LOGIN_BLOQUEIO_MIN)).isoformat(timespec="seconds")
    if not r or r["primeira"] < janela:
        con.execute("INSERT OR REPLACE INTO tentativas_login(chave, falhas, primeira, bloqueado_ate) VALUES(?,1,?,NULL)",
                    (chave, agora()))
        return
    falhas = r["falhas"] + 1
    ate = None
    if falhas >= cfg.LOGIN_MAX_FALHAS:
        ate = (dt.datetime.now() + dt.timedelta(minutes=cfg.LOGIN_BLOQUEIO_MIN)).isoformat(timespec="seconds")
    con.execute("UPDATE tentativas_login SET falhas=?, bloqueado_ate=? WHERE chave=?", (falhas, ate, chave))


def autenticar(email, senha):
    """Retorna (usuario|None, mensagem_erro|None)."""
    con, cfg = g.db, current_app.config["MAPA"]
    email = (email or "").strip().lower()
    chaves = [f"ip:{request.remote_addr}", f"email:{email}"]
    if any(_bloqueado(con, c) for c in chaves):
        return None, f"Muitas tentativas. Aguarde {cfg.LOGIN_BLOQUEIO_MIN} minutos e tente de novo."
    u = con.execute("SELECT * FROM usuarios WHERE email=?", (email,)).fetchone()
    ok = check_password_hash(u["senha_hash"] if u else _HASH_FALSO, senha or "")
    if not u or not ok or not u["ativo"]:
        for c in chaves:
            _falha(con, c, cfg)
        return None, "E-mail ou senha incorretos."
    for c in chaves:
        con.execute("DELETE FROM tentativas_login WHERE chave=?", (c,))
    con.execute("UPDATE usuarios SET ultimo_login=? WHERE id=?", (agora(), u["id"]))
    return u, None


def iniciar_sessao(u):
    session.clear()
    session.permanent = True
    session["uid"] = u["id"]
    session["sv"] = u["versao_sessao"]
    token_csrf()


# ------------------------------------------------------------------ cabeçalhos
CSP = ("default-src 'self'; "
       "script-src 'self'; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; "
       "img-src 'self' data: blob: https://tile.openstreetmap.org "
       "https://server.arcgisonline.com; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


def cabecalhos(resp):
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    # padrão do navegador: para outros sites (tiles do mapa) vai só a origem, nunca o caminho da página;
    # o OpenStreetMap exige esse Referer pela política de uso dos tiles
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.is_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    if request.path.startswith("/api/") or resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp
