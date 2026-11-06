"""Cópia de segurança completa (banco + ZIPs originais) e restauração numa instalação vazia.

Formato do arquivo (.zip, sem compressão — os ZIPs de fotos já são comprimidos):
  backup.json                  versão, data, contagens e SHA-256 de cada arquivo
  mapa.sqlite3                 cópia consistente do banco (API de backup do SQLite)
  envios/<sha256>.zip          os ZIPs enviados, intactos

Restauração só é aceita quando o sistema ainda não tem nenhuma foto nem envio (instalação nova):
assim ela nunca sobrescreve dados existentes.
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path

from .db import agora, conectar, inicializar

FORMATO = 1
NOME_ENVIO = re.compile(r"^envios/([0-9a-f]{64})\.zip$")


class BackupInvalido(Exception):
    pass


def _sha256(caminho):
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


def criar_backup(cfg, destino):
    """Grava o backup em `destino` e devolve o resumo."""
    cfg.preparar_pastas()
    destino = Path(destino)
    tmp_db = cfg.TMP_DIR / f"backup-{os.getpid()}-{time.time_ns()}.sqlite3"
    try:
        src = conectar(cfg.DB_PATH)
        dst = sqlite3.connect(tmp_db)
        src.backup(dst)                      # cópia consistente mesmo com o servidor em uso
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.close()
        n = {k: src.execute(f"SELECT COUNT(*) FROM {k}").fetchone()[0] for k in ("fotos", "envios", "usuarios", "eixo_blocos")}
        arquivos = [r["arquivo"] for r in src.execute("SELECT arquivo FROM envios")]
        src.close()
        resumo = {"formato": FORMATO, "criado_em": agora(), "contagens": n, "arquivos": {}}
        resumo["arquivos"]["mapa.sqlite3"] = _sha256(tmp_db)
        faltando = []
        with zipfile.ZipFile(destino, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            z.write(tmp_db, "mapa.sqlite3")
            for nome in sorted(set(arquivos)):
                p = cfg.ENVIOS_DIR / nome
                if not p.is_file():
                    faltando.append(nome)
                    continue
                resumo["arquivos"][f"envios/{nome}"] = _sha256(p)
                z.write(p, f"envios/{nome}")
            resumo["faltando"] = faltando
            z.writestr("backup.json", json.dumps(resumo, ensure_ascii=False, indent=1))
        return resumo
    finally:
        if tmp_db.exists():
            tmp_db.unlink()


def sistema_vazio(con):
    return not con.execute("SELECT 1 FROM fotos LIMIT 1").fetchone() and \
        not con.execute("SELECT 1 FROM envios LIMIT 1").fetchone()


def restaurar_backup(cfg, arquivo):
    """Restaura um backup numa instalação vazia. Confere todos os SHA-256 antes de gravar qualquer coisa."""
    cfg.preparar_pastas()
    con = conectar(cfg.DB_PATH)
    try:
        if not sistema_vazio(con):
            raise BackupInvalido("O sistema já tem fotos ou envios. A restauração só é feita numa instalação "
                                 "vazia, para nunca sobrescrever dados.")
    finally:
        con.close()
    try:
        z = zipfile.ZipFile(arquivo)
    except zipfile.BadZipFile:
        raise BackupInvalido("O arquivo não é um ZIP válido.")
    pasta = cfg.TMP_DIR / f"restaurar-{time.time_ns()}"
    pasta.mkdir()
    try:
        with z:
            nomes = set(z.namelist())
            if "backup.json" not in nomes or "mapa.sqlite3" not in nomes:
                raise BackupInvalido("Este ZIP não é um backup do Mapa de Intervenções (falta backup.json ou mapa.sqlite3).")
            resumo = json.loads(z.read("backup.json"))
            if resumo.get("formato") != FORMATO:
                raise BackupInvalido("Versão de backup desconhecida.")
            esperado = resumo.get("arquivos") or {}
            for nome in nomes - {"backup.json"}:
                if nome != "mapa.sqlite3" and not NOME_ENVIO.match(nome):
                    raise BackupInvalido(f"Arquivo inesperado no backup: {nome[:80]}")
                if nome not in esperado:
                    raise BackupInvalido(f"Arquivo sem conferência no backup: {nome[:80]}")
            # extrai para a pasta temporária (nomes já validados: sem ../ nem caminho absoluto)
            for nome in esperado:
                if nome not in nomes:
                    raise BackupInvalido(f"Arquivo listado mas ausente no backup: {nome[:80]}")
                alvo = pasta / nome.replace("/", "__")
                with z.open(nome) as src, open(alvo, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                if _sha256(alvo) != esperado[nome]:
                    raise BackupInvalido(f"Arquivo corrompido no backup: {nome[:80]}")
                m = NOME_ENVIO.match(nome)
                if m and _sha256(alvo) != m.group(1):
                    raise BackupInvalido(f"ZIP de fotos não confere com o nome: {nome[:80]}")
        # banco: confere integridade antes de usar
        db_novo = pasta / "mapa.sqlite3"
        t = sqlite3.connect(db_novo)
        ok = t.execute("PRAGMA integrity_check").fetchone()[0]
        t.close()
        if ok != "ok":
            raise BackupInvalido("O banco do backup não passou na verificação de integridade.")
        # tudo conferido: grava. ZIPs primeiro (somente leitura), banco por último, pela API de backup
        for p in pasta.glob("envios__*.zip"):
            destino = cfg.ENVIOS_DIR / p.name[len("envios__"):]
            if not destino.exists():
                shutil.move(str(p), destino)
                os.chmod(destino, 0o444)
        src = sqlite3.connect(db_novo)
        dst = conectar(cfg.DB_PATH)
        src.backup(dst)
        src.close()
        dst.close()
        inicializar(cfg.DB_PATH)          # garante tabelas/parâmetros de versões mais novas
        con = conectar(cfg.DB_PATH)
        # nenhuma sessão aberta antes da restauração continua valendo (ids de usuário mudam)
        con.execute("UPDATE usuarios SET versao_sessao = versao_sessao + 1")
        con.execute("UPDATE parametros SET valor = CAST(valor AS INTEGER) + 1000 WHERE chave='versao_dados'")
        con.execute("INSERT INTO auditoria(usuario_id, acao, detalhe, quando) VALUES(NULL, 'restaurou_backup', ?, ?)",
                    (json.dumps({"criado_em": resumo.get("criado_em"), "contagens": resumo.get("contagens")}), agora()))
        con.close()
        return resumo
    finally:
        shutil.rmtree(pasta, ignore_errors=True)
