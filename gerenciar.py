#!/usr/bin/env python3
"""Administração pela linha de comando.

  python gerenciar.py criar-admin --email voce@exemplo.com --nome "Seu nome"
      (a senha é pedida no terminal, sem aparecer na tela; use --senha-stdin para ler da entrada padrão)
  python gerenciar.py criar-usuario --email fulano@exemplo.com --papel visualizador
  python gerenciar.py redefinir-senha --email fulano@exemplo.com
  python gerenciar.py listar-usuarios
  python gerenciar.py importar-eixo "Estacas_BR-373_B4.kmz"
  python gerenciar.py backup --saida mapa-backup.zip
  python gerenciar.py restaurar mapa-backup.zip        (só numa instalação vazia)
  python gerenciar.py importar-legado dados.json --zip "22-10 - TAPA BURACO.zip" --zip "Fotos serviço 29-10.zip"

Nenhuma senha fica gravada em código ou em arquivo de configuração: só o hash (scrypt) vai para o banco.
"""
import argparse
import getpass
import hashlib
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

from mapa.config import Config
from mapa.db import agora, auditar, conectar, inicializar, nova_versao
from mapa.seguranca import SENHA_MIN, hash_senha


def _con(cfg):
    cfg.preparar_pastas()
    inicializar(cfg.DB_PATH)
    return conectar(cfg.DB_PATH)


def _ler_senha(args):
    if args.senha_stdin:
        senha = sys.stdin.readline().rstrip("\n")
    else:
        senha = getpass.getpass(f"Senha (mín. {SENHA_MIN} caracteres): ")
        if getpass.getpass("Repita a senha: ") != senha:
            sys.exit("As senhas não conferem.")
    try:
        return hash_senha(senha)
    except ValueError as e:
        sys.exit(str(e))


def criar_usuario(args, papel):
    cfg = Config()
    con = _con(cfg)
    email = args.email.strip().lower()
    if papel == "admin" and not args.adicional:
        if con.execute("SELECT 1 FROM usuarios WHERE papel='admin'").fetchone():
            sys.exit("Já existe um administrador. Use --adicional para criar outro.")
    if con.execute("SELECT 1 FROM usuarios WHERE email=?", (email,)).fetchone():
        sys.exit("Já existe um usuário com esse e-mail.")
    h = _ler_senha(args)
    con.execute("INSERT INTO usuarios(email, nome, senha_hash, papel, criado_em) VALUES(?,?,?,?,?)",
                (email, args.nome or "", h, papel, agora()))
    auditar(con, None, "criou_usuario_cli", {"email": email, "papel": papel})
    print(f"Usuário {email} criado com papel {papel}.")


def redefinir(args):
    cfg = Config()
    con = _con(cfg)
    u = con.execute("SELECT id FROM usuarios WHERE email=?", (args.email.strip().lower(),)).fetchone()
    if not u:
        sys.exit("Usuário não encontrado.")
    h = _ler_senha(args)
    con.execute("UPDATE usuarios SET senha_hash=?, ativo=1, versao_sessao=versao_sessao+1 WHERE id=?", (h, u["id"]))
    con.execute("DELETE FROM tentativas_login WHERE chave=?", ("email:" + args.email.strip().lower(),))
    print("Senha redefinida. Sessões abertas dessa conta foram encerradas.")


def listar(args):
    con = _con(Config())
    for u in con.execute("SELECT email, nome, papel, ativo, ultimo_login FROM usuarios ORDER BY papel, email"):
        print(f"{u['papel']:13} {'ativo' if u['ativo'] else 'INATIVO':8} {u['email']:35} {u['nome']}  último login: {u['ultimo_login'] or '-'}")


def importar_legado(args):
    """Importa o dados.json do mapa atual (com a classificação manual e as correções já feitas),
    o eixo dos blocos e, opcionalmente, os ZIPs originais para servir as fotos em tamanho real."""
    from mapa import processamento
    cfg = Config()
    con = _con(cfg)
    dados = json.load(open(args.dados, encoding="utf-8"))
    # 1) ZIPs originais: registrados como envios já concluídos (não são reprocessados)
    membro_por_sha = {}
    for zp in args.zip or []:
        h = hashlib.sha256()
        with open(zp, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        sha = h.hexdigest()
        env = con.execute("SELECT id FROM envios WHERE sha256=?", (sha,)).fetchone()
        if env:
            env_id = env["id"]
        else:
            destino = cfg.ENVIOS_DIR / f"{sha}.zip"
            if not destino.exists():
                shutil.copyfile(zp, destino)
                os.chmod(destino, 0o444)
            cur = con.execute(
                "INSERT INTO envios(sha256, nome_original, arquivo, tamanho, lote, origem, estado, mensagem, criado_em, concluido_em)"
                " VALUES(?,?,?,?,?,'legado','concluido','Importado do mapa anterior.',?,?)",
                (sha, os.path.basename(zp), destino.name, os.path.getsize(zp), processamento.nome_lote(zp), agora(), agora()))
            env_id = cur.lastrowid
        with zipfile.ZipFile(zp) as z:
            for info in z.infolist():
                if info.is_dir() or processamento.problema_no_nome(info) or processamento.IGNORAR_SILENCIOSO.search(info.filename):
                    continue
                if not info.filename.lower().endswith(processamento.EXT_JPG + processamento.EXT_CONVERTER):
                    continue
                try:
                    b = processamento.converter_para_jpeg(info.filename, z.read(info))
                except Exception:
                    continue
                membro_por_sha.setdefault(hashlib.sha1(b).hexdigest()[:16], (env_id, info.filename))
        print(f"ZIP {os.path.basename(zp)}: envio #{env_id}")
    # 2) eixo
    for bid, b in (dados.get("tracado") or {}).get("blocos", {}).items():
        con.execute("INSERT OR REPLACE INTO eixo_blocos(bloco, rodovia, fonte, pts, atualizado_em) VALUES(?,?,?,?,?)",
                    (bid, b.get("rodovia", ""), b.get("fonte", ""), json.dumps(b["pts"]), agora()))
    # 3) fotos
    novas = ja = sem_original = 0
    con.execute("BEGIN")
    for r in dados["registros"]:
        if con.execute("SELECT 1 FROM fotos WHERE id=?", (r["id"],)).fetchone():
            ja += 1
            continue
        env_id, membro = membro_por_sha.get(r["id"], (None, None))
        if not membro:
            sem_original += 1
        processamento.gravar_foto(con, {**r, "id_completo": None}, env_id, membro)
        novas += 1
    nova_versao(con)
    con.execute("COMMIT")
    print(f"Fotos importadas: {novas} (já existiam: {ja}; sem ZIP original para tamanho real: {sem_original}).")
    print(f"Blocos do eixo: {', '.join((dados.get('tracado') or {}).get('blocos', {}).keys()) or 'nenhum'}.")


def importar_eixo(args):
    """Carrega um KMZ/KML de estacas direto no banco (o servidor percebe sozinho)."""
    from mapa import eixo
    con = _con(Config())
    nome = os.path.basename(args.kmz)
    try:
        blocos = eixo.blocos_do_kml(eixo.ler_kml_bytes(nome, open(args.kmz, "rb").read()), nome)
    except eixo.KmzInvalido as e:
        sys.exit(f"ERRO: {e}")
    for bid, b in blocos.items():
        con.execute("INSERT OR REPLACE INTO eixo_blocos(bloco, rodovia, fonte, pts, atualizado_em) VALUES(?,?,?,?,?)",
                    (bid, b["rodovia"], b["fonte"], json.dumps(b["pts"]), agora()))
        est = [p[2] for p in b["pts"] if p[2] is not None]
        faixa = f"km {min(est) / 1000:.3f} a {max(est) / 1000:.3f}" if est else "sem estacas"
        print(f"Bloco {bid} ({b['rodovia'] or 'rodovia não informada'}): {len(b['pts'])} pontos, {faixa}")
    nova_versao(con)
    auditar(con, None, "carregou_eixo_cli", {"blocos": list(blocos)})
    print("Eixo carregado. Blocos no sistema:", ", ".join(r[0] for r in con.execute("SELECT bloco FROM eixo_blocos ORDER BY bloco")))


def fazer_backup(args):
    from mapa.backup import criar_backup
    r = criar_backup(Config(), args.saida)
    print(f"Backup gravado em {args.saida}: {r['contagens']}")
    if r["faltando"]:
        print("ATENÇÃO: ZIPs registrados mas ausentes na pasta envios:", ", ".join(r["faltando"]))


def restaurar(args):
    from mapa.backup import BackupInvalido, restaurar_backup
    cfg = Config()
    _con(cfg).close()
    try:
        r = restaurar_backup(cfg, args.arquivo)
    except BackupInvalido as e:
        sys.exit(str(e))
    print("Restaurado:", r["contagens"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for nome in ("criar-admin", "criar-usuario"):
        p = sub.add_parser(nome)
        p.add_argument("--email", required=True)
        p.add_argument("--nome", default="")
        p.add_argument("--senha-stdin", action="store_true", help="lê a senha da entrada padrão (automação)")
        if nome == "criar-admin":
            p.add_argument("--adicional", action="store_true", help="permite criar mais de um administrador")
        else:
            p.add_argument("--papel", choices=("visualizador", "admin"), default="visualizador")
            p.set_defaults(adicional=True)
    p = sub.add_parser("redefinir-senha")
    p.add_argument("--email", required=True)
    p.add_argument("--senha-stdin", action="store_true")
    sub.add_parser("listar-usuarios")
    p = sub.add_parser("importar-eixo")
    p.add_argument("kmz")
    p = sub.add_parser("importar-legado")
    p.add_argument("dados")
    p.add_argument("--zip", action="append")
    p = sub.add_parser("backup")
    p.add_argument("--saida", default="mapa-backup.zip")
    p = sub.add_parser("restaurar")
    p.add_argument("arquivo")
    a = ap.parse_args()
    if a.cmd == "criar-admin":
        criar_usuario(a, "admin")
    elif a.cmd == "criar-usuario":
        criar_usuario(a, a.papel)
    elif a.cmd == "redefinir-senha":
        redefinir(a)
    elif a.cmd == "listar-usuarios":
        listar(a)
    elif a.cmd == "importar-eixo":
        importar_eixo(a)
    elif a.cmd == "importar-legado":
        importar_legado(a)
    elif a.cmd == "backup":
        fazer_backup(a)
    elif a.cmd == "restaurar":
        restaurar(a)


if __name__ == "__main__":
    main()
