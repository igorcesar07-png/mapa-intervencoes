"""Envio de ZIP: validação, fila persistida no banco e processamento em segundo plano.

A leitura de cada foto é a do atualizar_dados.py, sem alterações
(EXIF -> carimbo por OCR -> nome do arquivo; nunca a data do arquivo).
"""
import base64
import concurrent.futures as cf
import hashlib
import importlib.util
import io
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
import traceback
import zipfile
from pathlib import Path, PurePosixPath

from PIL import Image

from . import classificacao, regras, visao
from .datas import data_valida, fmt_br
from .db import agora, conectar, nova_versao, parametros

log = logging.getLogger("mapa.processamento")
RAIZ = Path(__file__).resolve().parent.parent

try:  # fotos HEIC do iPhone (opcional)
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_OK = True
except Exception:  # pragma: no cover
    HEIC_OK = False

EXT_JPG = (".jpg", ".jpeg")
EXT_CONVERTER = (".png", ".heic", ".heif")
IGNORAR_SILENCIOSO = re.compile(r"(^|/)(__MACOSX/|\._|\.DS_Store$|Thumbs\.db$|desktop\.ini$)", re.I)

_leitura = None
_leitura_lock = threading.Lock()


def leitura():
    """Carrega o atualizar_dados.py original como módulo (lógica de leitura preservada)."""
    global _leitura
    with _leitura_lock:
        if _leitura is None:
            spec = importlib.util.spec_from_file_location("atualizar_dados", RAIZ / "atualizar_dados.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _leitura = mod
        return _leitura


class ZipInvalido(Exception):
    pass


# ------------------------------------------------------------------ nomes dentro do ZIP
def problema_no_nome(info):
    """Retorna o motivo se o caminho do membro for perigoso; None se for seguro."""
    n = info.filename
    if "\x00" in n:
        return "nome com caractere nulo"
    n2 = n.replace("\\", "/")
    if n2.startswith("/") or re.match(r"^[A-Za-z]:", n2):
        return "caminho absoluto"
    if ".." in PurePosixPath(n2).parts:
        return "caminho com '..' (tentativa de sair da pasta)"
    if stat.S_ISLNK(info.external_attr >> 16):
        return "link simbólico"
    return None


def nome_lote(nome_original):
    base = os.path.basename((nome_original or "envio.zip").replace("\\", "/"))
    base = re.sub(r"\.zip$", "", base, flags=re.I)
    base = re.sub(r"[\x00-\x1f<>\"]", "", base).strip()
    return base[:120] or "envio"


# ------------------------------------------------------------------ validação síncrona (no envio)
def validar_zip(caminho, cfg):
    """Validação estrutural rápida. Lança ZipInvalido com mensagem clara."""
    if not zipfile.is_zipfile(caminho):
        raise ZipInvalido("O arquivo não é um ZIP válido.")
    try:
        with zipfile.ZipFile(caminho) as z:
            infos = z.infolist()
            if len(infos) > cfg.MAX_MEMBROS:
                raise ZipInvalido(f"O ZIP tem {len(infos)} arquivos; o limite é {cfg.MAX_MEMBROS}.")
            total = 0
            fotos = 0
            for i in infos:
                if i.is_dir():
                    continue
                if i.flag_bits & 0x1:
                    raise ZipInvalido("O ZIP está protegido por senha. Envie um ZIP sem senha.")
                total += i.file_size
                if i.compress_size and i.file_size / max(i.compress_size, 1) > cfg.MAX_RAZAO_COMPRESSAO and i.file_size > 10 * 1024 * 1024:
                    raise ZipInvalido(f"O arquivo '{i.filename}' tem compressão suspeita (possível 'zip bomb').")
                if i.filename.lower().endswith(EXT_JPG + EXT_CONVERTER) and not IGNORAR_SILENCIOSO.search(i.filename):
                    fotos += 1
            if total > cfg.MAX_DESCOMPACTADO_BYTES:
                raise ZipInvalido("O conteúdo descompactado do ZIP passa do limite permitido.")
            if fotos == 0:
                raise ZipInvalido("O ZIP não tem nenhuma foto (.jpg, .jpeg, .png ou .heic).")
            ruim = z.testzip()
            if ruim:
                raise ZipInvalido(f"O ZIP está corrompido (arquivo '{ruim}').")
    except zipfile.BadZipFile as e:
        raise ZipInvalido(f"O ZIP está corrompido: {e}")
    except NotImplementedError:
        raise ZipInvalido("O ZIP usa um método de compressão não suportado.")


def receber_envio(con, cfg, arquivo_storage, nome_original, usuario_id, empresa=None):
    """Grava o ZIP enviado (somente leitura), valida e coloca na fila. Retorna (status_http, corpo)."""
    cfg.preparar_pastas()
    tmp = cfg.TMP_DIR / f"envio-{os.getpid()}-{threading.get_ident()}-{time.time_ns()}.zip"
    h, tam = hashlib.sha256(), 0
    try:
        with open(tmp, "wb") as out:
            while True:
                bloco = arquivo_storage.stream.read(1024 * 1024)
                if not bloco:
                    break
                tam += len(bloco)
                if tam > cfg.MAX_ZIP_BYTES:
                    raise ZipInvalido(f"O arquivo passa do limite de {cfg.MAX_ZIP_BYTES // (1024 * 1024)} MB.")
                h.update(bloco)
                out.write(bloco)
        if tam == 0:
            raise ZipInvalido("O arquivo enviado está vazio.")
        sha = h.hexdigest()
        ja = con.execute("SELECT id, nome_original, criado_em, estado FROM envios WHERE sha256=?", (sha,)).fetchone()
        if ja:
            return 409, {"erro": f"Este ZIP já foi enviado ({ja['nome_original']}, envio #{ja['id']} em "
                                 f"{ja['criado_em'][:16].replace('T', ' ')}). Nada foi processado de novo.",
                         "envio_id": ja["id"]}
        validar_zip(tmp, cfg)
        destino = cfg.ENVIOS_DIR / f"{sha}.zip"
        shutil.move(str(tmp), destino)
        os.chmod(destino, 0o444)  # o arquivo enviado não é alterado nem apagado
        cur = con.execute(
            "INSERT INTO envios(sha256, nome_original, arquivo, tamanho, lote, estado, mensagem, enviado_por, criado_em, empresa) "
            "VALUES(?,?,?,?,?, 'aguardando', 'Na fila para processamento.', ?, ?, ?)",
            (sha, os.path.basename(nome_original or "envio.zip")[:200], destino.name, tam, nome_lote(nome_original), usuario_id, agora(),
             empresa))
        return 202, {"envio_id": cur.lastrowid, "estado": "aguardando"}
    except ZipInvalido as e:
        return 400, {"erro": str(e)}
    finally:
        if tmp.exists():
            tmp.unlink()


# ------------------------------------------------------------------ conversão e leitura de membros
def converter_para_jpeg(nome, dados):
    """JPG passa direto. PNG/HEIC viram JPEG (mantendo o EXIF), como foi feito no lote de 29/10."""
    if nome.lower().endswith(EXT_JPG):
        return dados
    if nome.lower().endswith((".heic", ".heif")) and not HEIC_OK:
        raise ValueError("HEIC sem suporte instalado (pillow-heif)")
    im = Image.open(io.BytesIO(dados))
    ex = im.info.get("exif")
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=92, **({"exif": ex} if ex else {}))
    return out.getvalue()


def ler_membro(z, info, limite):
    if info.file_size > limite:
        raise ValueError(f"foto maior que o limite ({limite // (1024 * 1024)} MB)")
    with z.open(info) as f:
        dados = f.read(limite + 1)
    if len(dados) > limite:
        raise ValueError("foto maior que o limite")
    return dados


def imagem_da_foto(cfg, con, foto):
    """Bytes JPEG da foto original, lida direto do ZIP enviado (sem extrair nada em disco)."""
    if not foto["envio_id"] or not foto["membro"]:
        return None
    env = con.execute("SELECT arquivo FROM envios WHERE id=?", (foto["envio_id"],)).fetchone()
    if not env:
        return None
    caminho = cfg.ENVIOS_DIR / os.path.basename(env["arquivo"])
    if not caminho.exists():
        return None
    with zipfile.ZipFile(caminho) as z:
        info = z.getinfo(foto["membro"])
        if problema_no_nome(info):
            return None
        return converter_para_jpeg(info.filename, ler_membro(z, info, cfg.MAX_FOTO_BYTES))


class _ConteudoPreguicoso(dict):
    """Para revisar_isolados(): lê do ZIP só as fotos que precisarem ser relidas."""

    def __init__(self, z, por_sha, limite):
        super().__init__()
        self.z, self.por_sha, self.limite = z, por_sha, limite

    def __getitem__(self, sha):
        info = self.por_sha[sha]
        return converter_para_jpeg(info.filename, ler_membro(self.z, info, self.limite))


# ------------------------------------------------------------------ ocorrências
def ocorrencias_da_foto(r):
    o = []
    if r.get("lat") is None:
        o.append(("sem_coordenada", "Sem coordenada no EXIF nem no carimbo. A foto fica fora do mapa."))
    if not r.get("data"):
        o.append(("sem_data", "Sem data no EXIF, no carimbo nem no nome do arquivo."))
    elif r.get("data_fonte") == "nome_arquivo":
        o.append(("data_do_envio", "Data tirada do nome do arquivo (data de envio, não da foto)."))
    if r.get("data_suspeita"):
        o.append(("data_suspeita", "Data do carimbo posterior ao envio pelo WhatsApp. Conferir."))
    if r.get("data_revisada"):
        o.append(("data_corrigida", f"Data relida automaticamente (OCR leu {r.get('data_ocr_original')})."))
    if r.get("data_invalida"):
        o.append(("data_invalida", f"O carimbo traz uma data impossível ({fmt_br(r['data_invalida'])}). "
                  + ("Foi usada a data de envio do nome do arquivo." if r.get("data") else "A foto fica sem data até ser revista.")))
    if r.get("data_antes_envio"):
        o.append(("data_antes_envio", f"Data do carimbo ({fmt_br(r['data'])}) muito anterior ao envio pelo WhatsApp "
                  f"({fmt_br(r['data_antes_envio'])}). Pode ser erro de leitura do dia: confira a foto e corrija a data no painel."))
    if r.get("coord_revisada"):
        o.append(("coordenada_corrigida", f"Coordenada relida automaticamente (OCR leu {r.get('coord_ocr_original')})."))
    return o


def validar_data_registro(r):
    """Data impossível no carimbo/EXIF não entra no relatório. Segue a regra já existente:
    sem data válida, usa a data de envio do nome do arquivo (marcada como tal) ou fica sem data."""
    ref = r.get("importado_em") or agora()
    if not r.get("data") or data_valida(r["data"], ref):
        return r
    r = {**r, "data_invalida": r["data"], "data": None, "data_fonte": None}
    try:
        nd = leitura().data_do_nome(r.get("arquivo") or "")
    except Exception:
        nd = None
    if nd and data_valida(nd, ref):
        r["data"], r["data_fonte"] = nd, "nome_arquivo"
    return r


DIAS_ANTES_DO_ENVIO = 3


def data_antes_do_envio(r):
    """Data do carimbo/EXIF mais de 3 dias ANTES do envio pelo WhatsApp (data no nome do arquivo):
    devolve a data do envio (indício de dia lido errado pelo OCR, ex.: 31 lido como 4); senão None.
    Não altera nada: só sinaliza para o administrador conferir."""
    if not r.get("data") or r.get("data_fonte") not in ("carimbo", "exif"):
        return None
    try:
        nd = leitura().data_do_nome(r.get("arquivo") or "")
    except Exception:
        return None
    if not nd:
        return None
    from datetime import datetime, timedelta
    try:
        if datetime.fromisoformat(r["data"][:19]) < datetime.fromisoformat(nd[:19]) - timedelta(days=DIAS_ANTES_DO_ENVIO):
            return nd
    except ValueError:
        return None
    return None


def sinalizar_datas_antes_do_envio(con):
    """Para fotos já gravadas (idempotente): registra a ocorrência 'data_antes_envio' uma única vez."""
    n = 0
    for f in con.execute("SELECT id, arquivo, data, data_fonte, extras, envio_id, membro FROM fotos WHERE data IS NOT NULL").fetchall():
        extras = json.loads(f["extras"] or "{}")
        if extras.get("data_antes_envio") or extras.get("data_manual"):
            continue
        nd = data_antes_do_envio(dict(f))
        if not nd:
            continue
        extras["data_antes_envio"] = nd
        con.execute("UPDATE fotos SET extras=? WHERE id=?", (json.dumps(extras, ensure_ascii=False), f["id"]))
        for tipo, msg in ocorrencias_da_foto({**dict(f), "lat": 0, "data_antes_envio": nd}):
            if tipo == "data_antes_envio":
                con.execute("INSERT INTO ocorrencias(envio_id, foto_id, membro, tipo, mensagem, criado_em) VALUES(?,?,?,?,?,?)",
                            (f["envio_id"], f["id"], f["membro"], tipo, msg, agora()))
        n += 1
    return n


def corrigir_datas_invalidas(con):
    """Aplica a validação às fotos já gravadas (idempotente). Retorna quantas foram corrigidas."""
    n = 0
    for f in con.execute("SELECT id, arquivo, data, data_fonte, extras, importado_em, envio_id, membro FROM fotos "
                         "WHERE data IS NOT NULL").fetchall():
        if data_valida(f["data"], f["importado_em"]):
            continue
        r = validar_data_registro(dict(f))
        extras = json.loads(f["extras"] or "{}")
        extras["data_invalida"] = r["data_invalida"]
        con.execute("UPDATE fotos SET data=?, data_fonte=?, extras=? WHERE id=?",
                    (r["data"], r["data_fonte"], json.dumps(extras, ensure_ascii=False), f["id"]))
        for tipo, msg in ocorrencias_da_foto({**r, "lat": 0}):
            if tipo in ("data_invalida", "data_do_envio", "sem_data"):
                con.execute("INSERT INTO ocorrencias(envio_id, foto_id, membro, tipo, mensagem, criado_em) VALUES(?,?,?,?,?,?)",
                            (f["envio_id"], f["id"], f["membro"], tipo, msg, agora()))
        n += 1
    if n:
        nova_versao(con)
        visao.invalidar()
    return n


def gravar_foto(con, r, envio_id, membro, classe=None, motivo=None, extras_extra=None):
    r = validar_data_registro(r)
    nd = data_antes_do_envio(r)
    if nd:
        r = {**r, "data_antes_envio": nd}
    extras = {k: r[k] for k in ("texto_ocr", "data_revisada", "data_ocr_original", "data_suspeita", "data_invalida", "data_antes_envio",
                                "coord_revisada", "coord_ocr_original", "revisado_manual", "lote_carimbo", "est_carimbo", "faixa",
                                "nota", "coord_exif") if r.get(k) is not None}
    if extras_extra:
        extras.update(extras_extra)
    thumb = r.get("thumb") or ""
    mini = base64.b64decode(thumb.split(",", 1)[1]) if thumb.startswith("data:") else None
    con.execute(
        "INSERT INTO fotos(id, sha1, envio_id, membro, arquivo, lote, data, data_fonte, lat, lon, coord_fonte, km, rodovia,"
        " local, sentido, classe, motivo_exclusao, extras, miniatura, importado_em) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (r["id"], r.get("id_completo"), envio_id, membro, r["arquivo"], r.get("lote"), r.get("data"), r.get("data_fonte"),
         r.get("lat"), r.get("lon"), r.get("coord_fonte"), r.get("km"), r.get("rodovia"), r.get("local"), r.get("sentido"),
         classe if classe is not None else r.get("classe"), motivo if motivo is not None else r.get("motivo_exclusao"),
         json.dumps(extras, ensure_ascii=False), mini, r.get("importado_em") or agora()))
    for tipo, msg in ocorrencias_da_foto(r):
        con.execute("INSERT INTO ocorrencias(envio_id, foto_id, membro, tipo, mensagem, criado_em) VALUES(?,?,?,?,?,?)",
                    (envio_id, r["id"], membro, tipo, msg, agora()))


# ------------------------------------------------------------------ revisão da classificação
def registrar_revisao(con, r, blocos, params, linhas=None, novo=True):
    """Grava a classificação da foto. novo=True (foto importada agora): 'aguardando' ou 'pendente', fora do
    mapa até a confirmação. novo=False (foto que já estava no mapa): só registra 'divergente' se houver
    divergência; a foto continua no mapa. Devolve 'auto' ou 'pendente'."""
    c = classificacao.classificar(r, blocos, params, linhas)
    if novo and r.get("data") and r.get("lat") is not None and r.get("data_fonte") in ("carimbo", "exif"):
        # mesma data/hora e mesma posição de uma foto já guardada: provável reenvio da mesma intervenção
        for o in con.execute("SELECT id, arquivo, lat, lon FROM fotos WHERE data=? AND id<>? AND lat IS NOT NULL", (r["data"], r["id"])):
            if regras.dist_m((r["lat"], r["lon"]), (o["lat"], o["lon"])) < 30:
                c["divergencias"].append({"campo": "duplicata", "carimbo": None, "sistema": o["arquivo"],
                                          "msg": f"Mesma data, hora e posição da foto {o['arquivo']}, já existente: provável reenvio."})
                c["estado"], c["final"] = "pendente", None
                break
    if not novo and c["estado"] == "auto":
        return "auto"
    estado = ("aguardando" if c["estado"] == "auto" else "pendente") if novo else "divergente"
    f = c["final"] or {}
    con.execute("INSERT INTO revisao_foto(foto_id, estado, carimbo, sistema, divergencias, sugestao, rodovia, lote, est, lat, lon, fonte,"
                " criado_em) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(foto_id) DO UPDATE SET estado=excluded.estado,"
                " carimbo=excluded.carimbo, sistema=excluded.sistema, divergencias=excluded.divergencias, sugestao=excluded.sugestao,"
                " rodovia=excluded.rodovia, lote=excluded.lote, est=excluded.est, lat=excluded.lat, lon=excluded.lon, fonte=excluded.fonte",
                (r["id"], estado, json.dumps(c["carimbo"], ensure_ascii=False), json.dumps(c["sistema"], ensure_ascii=False),
                 json.dumps(c["divergencias"], ensure_ascii=False), json.dumps(c["sugestao"], ensure_ascii=False) if c["sugestao"] else None,
                 f.get("rodovia"), f.get("lote"), f.get("est"), f.get("lat"), f.get("lon"), f.get("fonte"), agora()))
    return c["estado"]


def revisar_fotos_existentes(con):
    """Compara o carimbo das fotos que já estão no mapa com a classificação do sistema e sinaliza as
    divergências (estado 'divergente'). Não muda nenhuma foto: a decisão é do administrador.
    Fotos já revisadas ('confirmada') ou em revisão de importação não são tocadas. Devolve quantas divergem."""
    blocos, params = visao.carregar_blocos(con), parametros(con)
    if not blocos:
        return 0
    linhas = regras.linhas_eixo(blocos)
    com_revisao = {r[0]: r[1] for r in con.execute("SELECT foto_id, estado FROM revisao_foto")}
    n = 0
    for row in con.execute("SELECT id, arquivo, lat, lon, rodovia, extras FROM fotos").fetchall():
        est = com_revisao.get(row["id"])
        if est in ("aguardando", "pendente", "confirmada"):
            continue
        r = dict(row)
        r["extras"] = json.loads(r["extras"] or "{}")
        if not (r["extras"].get("texto_ocr") or r["extras"].get("lote_carimbo")):
            continue   # sem carimbo lido: nada a comparar
        if registrar_revisao(con, r, blocos, params, linhas, novo=False) == "pendente":
            n += 1
        elif est == "divergente":   # a divergência sumiu (ex.: eixo corrigido)
            con.execute("DELETE FROM revisao_foto WHERE foto_id=? AND estado='divergente'", (r["id"],))
    nova_versao(con)
    visao.invalidar()
    return n


# ------------------------------------------------------------------ processamento
def _ocorrencia(con, envio_id, membro, tipo, msg):
    con.execute("INSERT INTO ocorrencias(envio_id, membro, tipo, mensagem, criado_em) VALUES(?,?,?,?,?)",
                (envio_id, membro, tipo, msg, agora()))


def processar_envio(con, cfg, envio_id):
    env = con.execute("SELECT * FROM envios WHERE id=?", (envio_id,)).fetchone()
    con.execute("UPDATE envios SET estado='em_andamento', iniciado_em=?, mensagem='Lendo o arquivo…', processadas=0,"
                " novas=0, repetidas=0, ignoradas=0 WHERE id=?", (agora(), envio_id))
    con.execute("DELETE FROM ocorrencias WHERE envio_id=? AND foto_id IS NULL", (envio_id,))  # reprocesso limpo
    L = leitura()
    caminho = cfg.ENVIOS_DIR / os.path.basename(env["arquivo"])
    try:
        if not getattr(L, "TESS", None):
            log.warning("Tesseract não encontrado: só fotos com EXIF terão posição/data.")
        with zipfile.ZipFile(caminho) as z:
            candidatos, ignoradas = [], 0
            for info in z.infolist():
                if info.is_dir() or IGNORAR_SILENCIOSO.search(info.filename):
                    continue
                motivo = problema_no_nome(info)
                if motivo:
                    _ocorrencia(con, envio_id, info.filename[:300], "caminho_suspeito",
                                f"Arquivo ignorado: {motivo}. Nada foi gravado fora do sistema.")
                    ignoradas += 1
                    continue
                nome = info.filename.lower()
                if not nome.endswith(EXT_JPG + EXT_CONVERTER):
                    _ocorrencia(con, envio_id, info.filename[:300], "arquivo_ignorado",
                                "Tipo de arquivo não suportado (só .jpg, .jpeg, .png, .heic).")
                    ignoradas += 1
                    continue
                candidatos.append(info)
            total = len(candidatos)
            con.execute("UPDATE envios SET total=?, ignoradas=?, mensagem=? WHERE id=?",
                        (total, ignoradas, f"Lendo {total} foto(s)…", envio_id))

            existentes = {r["sha1"] for r in con.execute("SELECT sha1 FROM fotos WHERE sha1 IS NOT NULL")}
            por_sha, novos, membro_de = {}, [], {}
            processadas = repetidas = 0
            pend = {}

            def colher(fut):
                nonlocal processadas
                info = pend.pop(fut)
                try:
                    r = fut.result()
                    novos.append(r)
                    membro_de[r["id"]] = info.filename
                except Exception as e:  # foto corrompida, formato inesperado…
                    _ocorrencia(con, envio_id, info.filename[:300], "foto_invalida", f"Não foi possível ler a foto: {e}")
                processadas += 1

            with cf.ThreadPoolExecutor(max_workers=cfg.WORKER_THREADS) as pool:
                for info in candidatos:
                    try:
                        dados = converter_para_jpeg(info.filename, ler_membro(z, info, cfg.MAX_FOTO_BYTES))
                    except Exception as e:
                        _ocorrencia(con, envio_id, info.filename[:300], "foto_invalida", f"Não foi possível abrir: {e}")
                        processadas += 1
                        continue
                    sha = hashlib.sha1(dados).hexdigest()
                    if sha in existentes or sha in por_sha:
                        repetidas += 1  # a mesma foto (mesmo conteúdo) não é processada duas vezes
                        processadas += 1
                    else:
                        por_sha[sha] = info
                        pend[pool.submit(L.processar, os.path.basename(info.filename.replace("\\", "/")), dados, env["lote"])] = info
                        del dados
                    while len(pend) >= cfg.WORKER_THREADS * 2:
                        feitos, _ = cf.wait(list(pend), return_when=cf.FIRST_COMPLETED)
                        for f in feitos:
                            colher(f)
                    con.execute("UPDATE envios SET processadas=?, repetidas=? WHERE id=?", (processadas, repetidas, envio_id))
                while pend:
                    feitos, _ = cf.wait(list(pend), return_when=cf.FIRST_COMPLETED)
                    for f in feitos:
                        colher(f)
                    con.execute("UPDATE envios SET processadas=? WHERE id=?", (processadas, envio_id))

            con.execute("UPDATE envios SET processadas=?, repetidas=?, mensagem=? WHERE id=?",
                        (processadas, repetidas, "Conferindo pontos isolados e gravando…", envio_id))
            antigos = [dict(r) for r in con.execute("SELECT id, lat, lon FROM fotos WHERE lat IS NOT NULL")]
            if novos and getattr(L, "TESS", None):
                L.revisar_isolados(novos, antigos, _ConteudoPreguicoso(z, por_sha, cfg.MAX_FOTO_BYTES))

        con.execute("BEGIN IMMEDIATE")
        try:
            blocos, params = visao.carregar_blocos(con), parametros(con)
            linhas = regras.linhas_eixo(blocos)
            cont = {"auto": 0, "pendente": 0}
            for r in novos:
                gravar_foto(con, r, envio_id, membro_de[r["id"]])
                # cada foto nova passa pela revisão: só entra no mapa depois da confirmação do administrador
                k = registrar_revisao(con, r, blocos, params, linhas, novo=True)
                cont[k] += 1
            nova_versao(con)
            sem = sum(1 for r in novos if r.get("lat") is None)
            msg = (f"{len(novos)} foto(s) nova(s): {cont['auto']} classificada(s) automaticamente aguardando confirmação, "
                   f"{cont['pendente']} pendente(s) de revisão. {repetidas} já existente(s) ignorada(s), "
                   f"{ignoradas} arquivo(s) ignorado(s), {sem} sem coordenada.")
            con.execute("UPDATE envios SET estado='concluido', concluido_em=?, novas=?, mensagem=? WHERE id=?",
                        (agora(), len(novos), msg, envio_id))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        visao.invalidar()
    except Exception as e:
        log.error("Falha no envio %s: %s\n%s", envio_id, e, traceback.format_exc())
        msg = f"Falha no processamento: {e}" if isinstance(e, (zipfile.BadZipFile, ValueError, OSError)) \
            else "Falha inesperada no processamento. Veja o log do servidor."
        con.execute("UPDATE envios SET estado='erro', concluido_em=?, mensagem=? WHERE id=?", (agora(), msg, envio_id))


# ------------------------------------------------------------------ worker
class Worker(threading.Thread):
    """Processa a fila em segundo plano, um ZIP por vez. A fila fica no banco:
    se o servidor reiniciar, os envios pendentes são retomados do início."""

    def __init__(self, cfg):
        super().__init__(daemon=True, name="mapa-worker")
        self.cfg = cfg
        self.acordar = threading.Event()
        self.parar = threading.Event()

    def run(self):
        con = conectar(self.cfg.DB_PATH)
        while not self.parar.is_set():
            row = con.execute("SELECT id FROM envios WHERE estado='aguardando' ORDER BY id LIMIT 1").fetchone()
            if not row:
                self.acordar.wait(5)
                self.acordar.clear()
                continue
            processar_envio(con, self.cfg, row["id"])
        con.close()


def retomar_pendentes(con):
    con.execute("UPDATE envios SET estado='aguardando', mensagem='Retomado após reinício do servidor.' "
                "WHERE estado='em_andamento'")
