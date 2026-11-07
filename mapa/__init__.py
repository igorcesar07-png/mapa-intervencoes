"""Mapa de Intervenções — versão web com login e papéis (administrador / visualizador)."""
import datetime as dt
import logging

from flask import Flask, g, request

from .config import Config
from .db import conectar, inicializar


def _admin_inicial(con):
    """Primeira conta de administrador a partir de variáveis secretas da hospedagem.

    Só age quando o banco ainda não tem nenhum usuário; a senha nunca fica no código nem no repositório.
    Depois do primeiro acesso, troque a senha em Conta e apague MAPA_ADMIN_SENHA_INICIAL da hospedagem.
    """
    import os
    from .db import agora, auditar
    from .seguranca import hash_senha
    email = (os.environ.get("MAPA_ADMIN_EMAIL") or "").strip().lower()
    senha = os.environ.get("MAPA_ADMIN_SENHA_INICIAL") or ""
    if not email or not senha or con.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone():
        return
    try:
        h = hash_senha(senha)
    except ValueError as e:
        logging.getLogger("mapa").error("MAPA_ADMIN_SENHA_INICIAL inválida: %s", e)
        return
    con.execute("INSERT INTO usuarios(email, nome, senha_hash, papel, criado_em) VALUES(?,?,?,?,?)",
                (email, "Administrador", h, "admin", agora()))
    auditar(con, None, "criou_admin_inicial_ambiente", {"email": email})
    logging.getLogger("mapa").warning("Conta inicial de administrador criada para %s (variável de ambiente).", email)


def create_app(cfg=None, iniciar_worker=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = cfg or Config()
    cfg.preparar_pastas()
    inicializar(cfg.DB_PATH)

    app = Flask(__name__, static_folder=None)  # nada estático público: tudo passa pelo login
    app.config.update(
        MAPA=cfg,
        SECRET_KEY=cfg.chave_secreta(),
        SESSION_COOKIE_NAME="mapa_sessao",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.HTTPS,
        PERMANENT_SESSION_LIFETIME=dt.timedelta(hours=cfg.SESSAO_HORAS),
        MAX_CONTENT_LENGTH=cfg.MAX_ZIP_BYTES + 10 * 1024 * 1024,
    )

    from flask.sessions import SecureCookieSessionInterface

    class _Sessao(SecureCookieSessionInterface):
        # cookie "Secure" em todo acesso HTTPS (pelo link público); no próprio computador (http) continua funcionando
        def get_cookie_secure(self, app):
            return bool(app.config["SESSION_COOKIE_SECURE"] or request.is_secure)

    app.session_interface = _Sessao()

    from . import seguranca
    from .rotas import bp

    @app.before_request
    def _antes():
        g.db = conectar(cfg.DB_PATH)
        seguranca.carregar_usuario()
        return seguranca.verificar_csrf()

    @app.teardown_request
    def _depois(exc):
        con = g.pop("db", None)
        if con is not None:
            con.close()

    app.after_request(seguranca.cabecalhos)

    @app.errorhandler(413)
    def _grande(e):
        from flask import jsonify
        return jsonify(erro=f"Arquivo grande demais (limite {cfg.MAX_ZIP_BYTES // (1024 * 1024)} MB)."), 413

    @app.errorhandler(500)
    def _erro(e):
        logging.getLogger("mapa").error("Erro em %s %s: %s", request.method, request.path, getattr(e, "original_exception", e))
        if request.path.startswith("/api/"):
            from flask import jsonify
            return jsonify(erro="Erro interno. O administrador pode ver o detalhe nos logs."), 500
        return "Erro interno. Tente de novo em instantes.", 500

    @app.errorhandler(403)
    def _proibido(e):
        if request.path.startswith("/api/"):
            from flask import jsonify
            return jsonify(erro="Acesso negado."), 403
        return "Acesso negado.", 403

    app.register_blueprint(bp)

    from . import processamento
    con = conectar(cfg.DB_PATH)
    _admin_inicial(con)
    processamento.retomar_pendentes(con)
    corrigidas = processamento.corrigir_datas_invalidas(con)
    if processamento.sinalizar_datas_antes_do_envio(con):
        logging.getLogger("mapa").info("Fotos com data muito anterior ao envio pelo WhatsApp sinalizadas para conferência.")
    con.close()
    if iniciar_worker if iniciar_worker is not None else cfg.WORKER:
        w = processamento.Worker(cfg)
        w.start()
        app.extensions["mapa_worker"] = w
    if corrigidas:
        logging.getLogger("mapa").info("%s foto(s) com data impossível no carimbo foram corrigidas/sinalizadas.", corrigidas)

    @app.context_processor
    def _versao_recursos():
        # força o navegador a baixar JS/CSS novos depois de uma atualização
        from pathlib import Path
        pasta = Path(__file__).resolve().parent / "static"
        v = max((int(p.stat().st_mtime) for p in pasta.rglob("*") if p.is_file()), default=0)
        return {"v_recursos": v}
    return app
