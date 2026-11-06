"""Testes dos critérios de conclusão.

Rodar:  python -m pytest testes -q
"""
import hashlib
import io
import json
import re
import time
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from mapa import create_app, processamento, visao
from mapa.config import Config
from mapa.db import agora, conectar
from mapa.seguranca import hash_senha

AMOSTRAS = Path(__file__).parent / "amostras"
FOTOS = sorted(AMOSTRAS.glob("*.jpeg"))
SENHA_ADM = "senha-do-admin-123"
SENHA_VIS = "senha-do-visual-456"


# ------------------------------------------------------------------ ambiente
@pytest.fixture
def app(tmp_path):
    visao.invalidar()
    cfg = Config(dados_dir=tmp_path / "inst", WORKER_THREADS=2)
    app = create_app(cfg, iniciar_worker=True)
    app.config["TESTING"] = True
    con = conectar(cfg.DB_PATH)
    for email, papel, senha in (("adm@teste.com", "admin", SENHA_ADM), ("vis@teste.com", "visualizador", SENHA_VIS)):
        con.execute("INSERT INTO usuarios(email, nome, senha_hash, papel, criado_em) VALUES(?,?,?,?,?)",
                    (email, papel, hash_senha(senha), papel, agora()))
    con.close()
    yield app
    w = app.extensions.get("mapa_worker")
    if w:
        w.parar.set()
        w.acordar.set()
        w.join(timeout=30)
    visao.invalidar()


def entrar(app, email, senha):
    c = app.test_client()
    html = c.get("/login").get_data(as_text=True)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    r = c.post("/login", data={"email": email, "senha": senha, "csrf_token": csrf})
    assert r.status_code == 302, r.get_data(as_text=True)
    pagina = c.get("/").get_data(as_text=True)
    c.csrf = re.search(r'<meta name="csrf" content="([^"]+)"', pagina).group(1)
    return c


def zip_bytes(arquivos):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for nome, dados in arquivos:
            if isinstance(nome, zipfile.ZipInfo):
                z.writestr(nome, dados)
            else:
                z.writestr(nome, dados)
    return b.getvalue()


def enviar(c, dados, nome="lote-teste.zip"):
    return c.post("/api/admin/envios", data={"arquivo": (io.BytesIO(dados), nome)},
                  headers={"X-CSRF-Token": c.csrf}, content_type="multipart/form-data")


def esperar(c, envio_id, limite=300):
    t0 = time.time()
    estados = []
    while time.time() - t0 < limite:
        e = c.get(f"/api/admin/envios/{envio_id}").get_json()
        if not estados or estados[-1] != e["estado"]:
            estados.append(e["estado"])
        if e["estado"] in ("concluido", "erro"):
            return e, estados
        time.sleep(0.5)
    raise AssertionError("processamento não terminou")


# ------------------------------------------------------------------ 1. nada público sem login
@pytest.mark.parametrize("url", ["/", "/admin", "/conta", "/api/relatorio", "/api/versao", "/api/exportar.csv",
                                 "/api/fotos/0123456789abcdef/miniatura", "/api/fotos/0123456789abcdef/imagem",
                                 "/recursos/relatorio.js", "/recursos/vendor/leaflet/leaflet.js",
                                 "/api/admin/envios", "/api/admin/ocorrencias", "/api/admin/usuarios", "/static/relatorio.js"])
def test_nada_publico_sem_login(app, url):
    r = app.test_client().get(url)
    assert r.status_code in (302, 401, 404)
    if r.status_code == 302:
        assert "/login" in r.headers["Location"]
    assert b"registros" not in r.data


def test_login_errado_e_bloqueio(app):
    c = app.test_client()
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
    for _ in range(5):
        r = c.post("/login", data={"email": "adm@teste.com", "senha": "errada", "csrf_token": csrf})
        assert r.status_code == 401
    r = c.post("/login", data={"email": "adm@teste.com", "senha": SENHA_ADM, "csrf_token": csrf})
    assert r.status_code == 401 and "Muitas tentativas" in r.get_data(as_text=True)


def test_csrf_obrigatorio(app):
    c = entrar(app, "adm@teste.com", SENHA_ADM)
    r = c.post("/api/admin/envios", data={"arquivo": (io.BytesIO(b"x"), "a.zip")}, content_type="multipart/form-data")
    assert r.status_code == 403


# ------------------------------------------------------------------ 6. permissões checadas no servidor
def test_visualizador_bloqueado_no_servidor(app):
    c = entrar(app, "vis@teste.com", SENHA_VIS)
    h = {"X-CSRF-Token": c.csrf}
    assert c.get("/admin").status_code == 403
    assert enviar(c, zip_bytes([("a.jpg", FOTOS[0].read_bytes())])).status_code == 403
    for metodo, url, corpo in [("get", "/api/admin/envios", None), ("get", "/api/admin/ocorrencias", None),
                               ("get", "/api/admin/fotos", None), ("post", "/api/admin/fotos/0123456789abcdef/classe", {"classe": "depois"}),
                               ("post", "/api/admin/parametros", {"limite_anomalia_m": 5000}), ("get", "/api/admin/usuarios", None),
                               ("post", "/api/admin/usuarios", {"email": "x@y.com"}), ("delete", "/api/admin/eixo/2", None),
                               ("post", "/api/admin/ocorrencias/1/resolver", None)]:
        r = getattr(c, metodo)(url, json=corpo, headers=h)
        assert r.status_code == 403, (url, r.status_code)
    # nenhum ZIP gravado
    assert not list((app.config["MAPA"].ENVIOS_DIR).glob("*.zip"))


# ------------------------------------------------------------------ 1/2/3. envio e processamento em segundo plano
def test_fluxo_admin_envio_zip(app):
    c = entrar(app, "adm@teste.com", SENHA_ADM)
    kmz = (AMOSTRAS / "B2B3_KMZ_Estaca.kmz").read_bytes()
    r = c.post("/api/admin/eixo", data={"arquivo": (io.BytesIO(kmz), "B2B3_KMZ_Estaca.kmz")},
               headers={"X-CSRF-Token": c.csrf}, content_type="multipart/form-data")
    assert r.status_code == 200 and {b["bloco"] for b in r.get_json()["blocos"]} == {"2", "3"}

    dados = zip_bytes([(f"pasta/{p.name}", p.read_bytes()) for p in FOTOS] + [("pasta/video.mov", b"nao-e-foto")])
    t0 = time.time()
    r = enviar(c, dados, "22-10 - TAPA BURACO.zip")
    assert r.status_code == 202
    assert time.time() - t0 < 5, "o envio não pode esperar o processamento"
    # a interface continua respondendo enquanto processa
    assert c.get("/api/relatorio").status_code == 200
    env, estados = esperar(c, r.get_json()["envio_id"])
    assert env["estado"] == "concluido", env
    assert estados[-1] == "concluido"
    assert env["novas"] == len(FOTOS) and env["ignoradas"] == 1

    # o ZIP enviado é guardado intacto e somente leitura
    sha = hashlib.sha256(dados).hexdigest()
    guardado = app.config["MAPA"].ENVIOS_DIR / f"{sha}.zip"
    assert guardado.read_bytes() == dados and not (guardado.stat().st_mode & 0o222)

    rel = c.get("/api/relatorio").get_json()
    regs = {r["arquivo"]: r for r in rel["registros"]}
    a = regs["WhatsApp Image 2026-10-22 at 15.05.37.jpeg"]       # carimbo decimal
    assert a["data"] == "2026-10-22T14:54:56" and a["data_fonte"] == "carimbo"
    assert abs(a["lat"] + 25.47282) < 1e-4 and abs(a["lon"] + 49.89629) < 1e-4
    b = regs["WhatsApp Image 2026-10-22 at 16.33.36.jpeg"]       # app da concessionária: estaca 182+691
    assert b["d"]["bloco"] == "2" and abs(b["d"]["est"] - 182691) < 60
    sem = regs["WhatsApp Image 2026-10-22 at 16.24.37.jpeg"]     # GPS indisponível -> ocorrência, sem inventar
    assert sem["lat"] is None and sem["data"] is not None
    oc = c.get("/api/admin/ocorrencias").get_json()["ocorrencias"]
    assert any(o["tipo"] == "sem_coordenada" and o["foto_id"] == sem["id"] for o in oc)
    assert any(o["tipo"] == "arquivo_ignorado" and o["membro"] == "pasta/video.mov" for o in oc)
    # a foto original abre em tamanho real
    img = c.get(f"/api/fotos/{a['id']}/imagem")
    assert img.status_code == 200 and img.data == (AMOSTRAS / a["arquivo"]).read_bytes()

    # mesmo ZIP de novo: recusado; mesmas fotos em outro ZIP: não reprocessadas
    assert enviar(c, dados, "outro-nome.zip").status_code == 409
    r2 = enviar(c, zip_bytes([("x/" + FOTOS[0].name, FOTOS[0].read_bytes()), ("y/copia.jpg", FOTOS[0].read_bytes())]), "repetido.zip")
    env2, _ = esperar(c, r2.get_json()["envio_id"])
    assert env2["novas"] == 0 and env2["repetidas"] == 2
    assert len(c.get("/api/relatorio").get_json()["registros"]) == len(FOTOS)


# ------------------------------------------------------------------ 4. ZIPs inválidos ou maliciosos
def test_zip_invalido_e_malicioso(app, tmp_path):
    c = entrar(app, "adm@teste.com", SENHA_ADM)
    assert enviar(c, b"isto nao e zip").status_code == 400
    assert enviar(c, zip_bytes([("leia.txt", b"oi")])).status_code == 400   # sem fotos
    assert c.post("/api/admin/envios", data={"arquivo": (io.BytesIO(b"x"), "fotos.rar")}, headers={"X-CSRF-Token": c.csrf},
                  content_type="multipart/form-data").status_code == 400
    foto = FOTOS[0].read_bytes()
    link = zipfile.ZipInfo("link.jpg")
    link.external_attr = (0o120777 << 16)
    dados = zip_bytes([("../../fora.jpg", foto), ("/etc/abs.jpg", foto), ("C:/win/x.jpg", foto), (link, b"/etc/passwd"),
                       ("ok/boa.jpg", foto), ("ok/programa.exe", b"MZ")])
    r = enviar(c, dados, "malicioso.zip")
    assert r.status_code == 202
    env, _ = esperar(c, r.get_json()["envio_id"])
    assert env["estado"] == "concluido" and env["novas"] == 1 and env["ignoradas"] == 5
    tipos = [o["tipo"] for o in c.get("/api/admin/ocorrencias").get_json()["ocorrencias"]]
    assert tipos.count("caminho_suspeito") == 4 and tipos.count("arquivo_ignorado") == 1
    # nada foi escrito fora da pasta de envios
    inst = app.config["MAPA"].DADOS_DIR
    assert not (inst.parent / "fora.jpg").exists() and not Path("/etc/abs.jpg").exists()
    assert {p.name for p in inst.iterdir()} <= {"mapa.sqlite3", "mapa.sqlite3-wal", "mapa.sqlite3-shm", "envios", "tmp", "chave_secreta"}

    # foto corrompida vira ocorrência, sem derrubar o envio
    r = enviar(c, zip_bytes([("ruim.jpg", b"\xff\xd8\xff nao e jpeg"), ("ok2.jpg", FOTOS[1].read_bytes())]), "corrompida.zip")
    env, _ = esperar(c, r.get_json()["envio_id"])
    assert env["estado"] == "concluido" and env["novas"] == 1
    assert any(o["tipo"] == "foto_invalida" for o in c.get("/api/admin/ocorrencias").get_json()["ocorrencias"])


# ------------------------------------------------------------------ 5. visualizador nunca recebe dados restritos
def _jpeg():
    b = io.BytesIO()
    Image.new("RGB", (8, 8), "gray").save(b, "JPEG")
    return "data:image/jpeg;base64," + __import__("base64").b64encode(b.getvalue()).decode()


def _semear(app):
    """Eixo reto com estacas e um caso de cada categoria."""
    con = conectar(app.config["MAPA"].DB_PATH)
    pts = [[-25.0, -50.0 + i * 0.0002, 100000 + i * 20] for i in range(500)]   # ~10 km
    con.execute("INSERT INTO eixo_blocos(bloco, rodovia, fonte, pts, atualizado_em) VALUES('2','BR-277','teste',?,?)",
                (json.dumps(pts), agora()))
    T = _jpeg()

    def f(i, arquivo, data, lat, lon, classe=None, fonte="carimbo"):
        r = {"id": f"{i:016x}", "arquivo": arquivo, "lote": "L1", "data": data, "data_fonte": fonte, "lat": lat, "lon": lon,
             "coord_fonte": "carimbo" if lat else None, "classe": classe, "thumb": T}
        processamento.gravar_foto(con, r, None, None)
    f(1, "VALIDO_TAPADO.jpg", "2026-10-29T10:00:05", -25.0, -49.99, "depois")
    f(2, "EXTRA_ANTES_MESMO_BURACO.jpg", "2026-10-29T09:58:10", -25.00005, -49.99005, "antes")
    f(3, "SO_ANTES_SEM_TAPADO.jpg", "2026-10-29T11:00:00", -25.0, -49.97, "antes")
    f(4, "NAO_RELACIONADA_CHUVA.jpg", "2026-10-29T12:00:00", -25.0, -49.96, "outro")
    f(5, "ANOMALIA_LONGE.jpg", "2026-10-29T13:00:00", -25.05, -49.95, "depois")
    f(6, "SEM_COORDENADA.jpg", "2026-10-29T14:00:00", None, None, "depois")
    f(7, "VALIDO_TAPADO_REENVIO.jpg", "2026-10-29T10:00:05", -25.0, -49.99, "depois")
    f(8, "GPS_TRAVADO_A.jpg", "2026-10-29T15:00:00", -25.0, -49.93, "depois")
    f(9, "GPS_TRAVADO_B.jpg", "2026-10-29T15:20:00", -25.0, -49.93, "depois")
    f(10, "OUTRO_DIA_VALIDO.jpg", "2026-10-30T08:00:00", -25.0, -49.92, None)   # não classificada (regra atual: conta)
    con.execute("UPDATE parametros SET valor=CAST(valor AS INTEGER)+1 WHERE chave='versao_dados'")
    con.close()
    visao.invalidar()


VALIDOS = {"VALIDO_TAPADO.jpg", "OUTRO_DIA_VALIDO.jpg"}
RESTRITOS = {"EXTRA_ANTES_MESMO_BURACO.jpg", "SO_ANTES_SEM_TAPADO.jpg", "NAO_RELACIONADA_CHUVA.jpg", "ANOMALIA_LONGE.jpg",
             "SEM_COORDENADA.jpg", "VALIDO_TAPADO_REENVIO.jpg", "GPS_TRAVADO_A.jpg", "GPS_TRAVADO_B.jpg"}


def test_visualizador_nao_ve_restritos(app):
    _semear(app)
    v = entrar(app, "vis@teste.com", SENHA_VIS)
    rel = v.get("/api/relatorio")
    texto = rel.get_data(as_text=True)
    j = rel.get_json()
    assert {r["arquivo"] for r in j["registros"]} == VALIDOS
    assert len(j["grupos"]) == 2 and all(len(g["fotos"]) == 1 for g in j["grupos"])
    for nome in RESTRITOS:
        assert nome not in texto
    for i in range(2, 10):
        assert f"{i:016x}" not in texto
    for campo in ("anomalia", "motivo", "classe", "texto_ocr", "dup", "travado"):
        assert f'"{campo}"' not in texto, campo
    # CSV com e sem filtros
    for q in ("", "?lote=L1", "?dia=2026-10-29", "?bloco=2", "?mes=2026-10"):
        csvtxt = v.get("/api/exportar.csv" + q).get_data(as_text=True)
        assert not any(n in csvtxt for n in RESTRITOS), q
        assert "anomalia" not in csvtxt.split("\r\n")[0]
    assert "VALIDO_TAPADO.jpg" in v.get("/api/exportar.csv").get_data(as_text=True)
    # fotos restritas: 404 (nem revela que existem)
    for i in range(2, 10):
        assert v.get(f"/api/fotos/{i:016x}/miniatura").status_code == 404
        assert v.get(f"/api/fotos/{i:016x}/imagem").status_code == 404
    assert v.get(f"/api/fotos/{1:016x}/miniatura").status_code == 200
    # a página do visualizador não traz as seções do administrador
    html = v.get("/").get_data(as_text=True)
    assert "anomList" not in html and "semGrid" not in html and 'href="/admin"' not in html

    # administrador vê tudo, com o motivo de cada restrição
    a = entrar(app, "adm@teste.com", SENHA_ADM)
    ja = a.get("/api/relatorio").get_json()
    assert {r["arquivo"] for r in ja["registros"]} == VALIDOS | RESTRITOS
    d = {r["arquivo"]: r["d"] for r in ja["registros"]}
    assert d["ANOMALIA_LONGE.jpg"]["anomalia"] and "do eixo" in d["ANOMALIA_LONGE.jpg"]["motivo"]
    assert d["GPS_TRAVADO_A.jpg"]["anomalia"] and "GPS repetido" in d["GPS_TRAVADO_A.jpg"]["motivo"]
    assert d["VALIDO_TAPADO_REENVIO.jpg"]["dup"] == f"{1:016x}"
    assert d["EXTRA_ANTES_MESMO_BURACO.jpg"]["grupo"] == d["VALIDO_TAPADO.jpg"]["grupo"]
    g = {x["id"]: x for x in ja["grupos"]}
    assert g[d["SO_ANTES_SEM_TAPADO.jpg"]["grupo"]]["tapado"] is False
    csv_adm = a.get("/api/exportar.csv").get_data(as_text=True)
    assert all(n in csv_adm for n in RESTRITOS)

    # regra "não classificadas contam" desligada pelo admin: a foto sem classe sai do visualizador
    assert a.post("/api/admin/parametros", json={"nao_classificadas_contam": 0}, headers={"X-CSRF-Token": a.csrf}).status_code == 200
    assert {r["arquivo"] for r in v.get("/api/relatorio").get_json()["registros"]} == {"VALIDO_TAPADO.jpg"}
    # classificação feita pelo admin vale para o visualizador
    a.post(f"/api/admin/fotos/{10:016x}/classe", json={"classe": "depois"}, headers={"X-CSRF-Token": a.csrf})
    assert {r["arquivo"] for r in v.get("/api/relatorio").get_json()["registros"]} == VALIDOS


def test_usuario_desativado_perde_acesso(app):
    v = entrar(app, "vis@teste.com", SENHA_VIS)
    a = entrar(app, "adm@teste.com", SENHA_ADM)
    uid = next(u["id"] for u in a.get("/api/admin/usuarios").get_json()["usuarios"] if u["email"] == "vis@teste.com")
    assert a.post(f"/api/admin/usuarios/{uid}", json={"ativo": False}, headers={"X-CSRF-Token": a.csrf}).status_code == 200
    assert v.get("/api/relatorio").status_code == 401


def test_admin_nao_se_rebaixa(app):
    a = entrar(app, "adm@teste.com", SENHA_ADM)
    eu = a.get("/api/admin/usuarios").get_json()["eu"]
    assert a.post(f"/api/admin/usuarios/{eu}", json={"papel": "visualizador"}, headers={"X-CSRF-Token": a.csrf}).status_code == 400


def test_kmz_com_prefixo_estaca_e_bloco_no_nome():
    """KMZ do Bloco 4: nomes "Estaca 183+400", sem campo Bloco (vem do nome do arquivo)."""
    from mapa import eixo
    kml = ('<?xml version="1.0" encoding="utf-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
           '<name>Estacas BR-373 B4</name>' + "".join(
               f'<Placemark><name>Estaca 183+{i * 20:03d}</name><ExtendedData><Data name="coluna_2"><displayName>Estaca</displayName>'
               f'<value>183+{i * 20:03d}</value></Data></ExtendedData><Point><coordinates>{-50.28 + i * 0.0002},-25.04,0</coordinates></Point></Placemark>'
               for i in range(10)) + '</Document></kml>').encode()
    b = eixo.blocos_do_kml(kml, "Estacas_BR-373_B4.kmz")
    assert list(b) == ["4"] and b["4"]["rodovia"] == "BR-373" and len(b["4"]["pts"]) == 10
    assert b["4"]["pts"][0][2] == 183000 and b["4"]["pts"][-1][2] == 183180


# ------------------------------------------------------------------ correções de 05/11
def test_datas_impossiveis_nao_chegam_ao_relatorio(app):
    """33/10, anos 2028/2086/2626 lidos errado pelo OCR: viram ocorrência e não aparecem no filtro."""
    con = conectar(app.config["MAPA"].DB_PATH)
    T = _jpeg()
    casos = [("a" * 16, "B4_INTERNO_2026-10-31_14h37_03.jpg", "2026-10-33T14:16:00"),
             ("b" * 16, "WhatsApp Image 2026-10-31 at 17.44.39.jpeg", "2086-10-31T05:17:02"),
             ("c" * 16, "ok.jpg", "2026-10-31T09:00:00")]
    for i, arq, data in casos:
        con.execute("INSERT INTO fotos(id, arquivo, lote, data, data_fonte, lat, lon, classe, extras, miniatura, importado_em)"
                    " VALUES(?,?,?,?, 'carimbo', -25.0, -49.99, 'depois', '{}', NULL, '2026-11-05T20:00:00')", (i, arq, "L", data))
    con.close()
    # a correção roda ao iniciar o sistema (e também ao gravar fotos novas)
    from mapa import processamento as P
    con = conectar(app.config["MAPA"].DB_PATH)
    assert P.corrigir_datas_invalidas(con) == 2
    assert P.corrigir_datas_invalidas(con) == 0          # idempotente
    d = {r["id"]: (r["data"], r["data_fonte"], json.loads(r["extras"])) for r in con.execute("SELECT * FROM fotos")}
    assert d["a" * 16][0] is None and d["a" * 16][2]["data_invalida"] == "2026-10-33T14:16:00"
    assert d["b" * 16][:2] == ("2026-10-31T17:44:39", "nome_arquivo")   # regra existente: data de envio, marcada
    assert d["c" * 16][0] == "2026-10-31T09:00:00"
    tipos = [r["tipo"] for r in con.execute("SELECT tipo FROM ocorrencias")]
    assert tipos.count("data_invalida") == 2
    con.close()
    # foto nova com data impossível também é barrada na gravação
    con = conectar(app.config["MAPA"].DB_PATH)
    P.gravar_foto(con, {"id": "d" * 16, "arquivo": "x.jpg", "data": "2626-10-31T10:48:12", "data_fonte": "carimbo",
                        "lat": -25.0, "lon": -49.98, "thumb": T}, None, None)
    r = con.execute("SELECT data, extras FROM fotos WHERE id=?", ("d" * 16,)).fetchone()
    assert r["data"] is None and "2626" in r["extras"]
    con.close()


def test_classificacao_salva_confirmada_e_paginacao(app):
    _semear(app)
    a = entrar(app, "adm@teste.com", SENHA_ADM)
    h = {"X-CSRF-Token": a.csrf}
    fid = f"{10:016x}"
    r = a.post(f"/api/admin/fotos/{fid}/classe", json={"classe": "depois"}, headers=h).get_json()
    assert r["ok"] and r["classe"] == "depois" and r["classificado_em"]
    r = a.post(f"/api/admin/fotos/{fid}/classe", json={"classe": "outro", "motivo": "chuva"}, headers=h).get_json()
    assert r["classe"] == "outro" and r["motivo_exclusao"] == "chuva"
    # recarregar a tela traz o que ficou gravado
    todas = a.get("/api/admin/fotos?classe=todas").get_json()["fotos"]
    assert next(f for f in todas if f["id"] == fid)["classe"] == "outro"
    assert a.get("/api/admin/fotos?classe=outro").get_json()["total"] == 2
    # paginação por posição (o painel desconta as fotos que saíram do filtro)
    p0 = a.get("/api/admin/fotos?classe=todas&inicio=0").get_json()
    p3 = a.get("/api/admin/fotos?classe=todas&inicio=3").get_json()
    assert p3["fotos"][0]["id"] == p0["fotos"][3]["id"]


def test_csv_com_varios_blocos(app):
    _semear(app)
    con = conectar(app.config["MAPA"].DB_PATH)
    pts = [[-24.0, -51.0 + i * 0.0002, 200000 + i * 20] for i in range(200)]
    con.execute("INSERT INTO eixo_blocos(bloco, rodovia, fonte, pts, atualizado_em) VALUES('4','BR-373','teste',?,?)", (json.dumps(pts), agora()))
    T = _jpeg()
    processamento.gravar_foto(con, {"id": f"{20:016x}", "arquivo": "BLOCO4.jpg", "lote": "L1", "data": "2026-10-29T10:00:00",
                                    "data_fonte": "carimbo", "lat": -24.0, "lon": -50.99, "classe": "depois", "thumb": T}, None, None)
    con.execute("UPDATE parametros SET valor=CAST(valor AS INTEGER)+1 WHERE chave='versao_dados'")
    con.close()
    visao.invalidar()
    v = entrar(app, "vis@teste.com", SENHA_VIS)
    so2 = v.get("/api/exportar.csv?blocos=2").get_data(as_text=True)
    ambos = v.get("/api/exportar.csv?blocos=2,4").get_data(as_text=True)
    so4 = v.get("/api/exportar.csv?bloco=4").get_data(as_text=True)      # forma antiga
    assert "BLOCO4.jpg" not in so2 and "VALIDO_TAPADO.jpg" in so2
    assert "BLOCO4.jpg" in ambos and "VALIDO_TAPADO.jpg" in ambos and ambos.count("VALIDO_TAPADO.jpg") == 1
    assert "BLOCO4.jpg" in so4 and "VALIDO_TAPADO.jpg" not in so4


# ------------------------------------------------------------------ publicação: saúde, admin inicial, backup
def test_saude_sem_dados(app):
    r = app.test_client().get("/saude")
    assert r.status_code == 200 and r.get_json() == {"ok": True}


@pytest.mark.parametrize("url", ["/api/admin/backup", "/api/admin/restaurar"])
def test_backup_so_admin(app, url):
    assert app.test_client().get(url).status_code == 401
    assert entrar(app, "vis@teste.com", SENHA_VIS).get(url).status_code == 403


def test_admin_inicial_por_variavel_de_ambiente(tmp_path, monkeypatch):
    monkeypatch.setenv("MAPA_ADMIN_EMAIL", "Chefe@Empresa.com")
    monkeypatch.setenv("MAPA_ADMIN_SENHA_INICIAL", "senha-inicial-forte-1")
    cfg = Config(dados_dir=tmp_path / "boot")
    app = create_app(cfg, iniciar_worker=False)
    con = conectar(cfg.DB_PATH)
    assert [tuple(r) for r in con.execute("SELECT email, papel FROM usuarios")] == [("chefe@empresa.com", "admin")]
    con.close()
    entrar(app, "chefe@empresa.com", "senha-inicial-forte-1")
    # numa segunda partida (com usuários) nada é criado nem alterado
    monkeypatch.setenv("MAPA_ADMIN_EMAIL", "outro@empresa.com")
    create_app(cfg, iniciar_worker=False)
    con = conectar(cfg.DB_PATH)
    assert con.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 1
    con.close()


def test_backup_e_restauracao_sem_perda(app, tmp_path, monkeypatch):
    c = entrar(app, "adm@teste.com", SENHA_ADM)
    r = enviar(c, zip_bytes([(p.name, p.read_bytes()) for p in FOTOS]), "05-11 - lote.zip")
    env, _ = esperar(c, r.get_json()["envio_id"])
    assert env["estado"] == "concluido"
    antes = c.get("/api/relatorio").get_json()
    # restauração recusada quando já há dados
    assert c.get("/api/admin/restaurar").get_json() == {"permitido": False}
    r = c.get("/api/admin/backup")
    assert r.status_code == 200 and r.mimetype == "application/zip"
    bk = r.data
    r = c.post("/api/admin/restaurar", data={"arquivo": (io.BytesIO(bk), "b.zip")},
               headers={"X-CSRF-Token": c.csrf}, content_type="multipart/form-data")
    assert r.status_code == 400 and "vazia" in r.get_json()["erro"]

    # instalação nova (como na hospedagem): admin inicial pelo ambiente, restaura, dados idênticos
    visao.invalidar()
    monkeypatch.setenv("MAPA_ADMIN_EMAIL", "boot@empresa.com")
    monkeypatch.setenv("MAPA_ADMIN_SENHA_INICIAL", "senha-boot-12345")
    cfg2 = Config(dados_dir=tmp_path / "nova")
    app2 = create_app(cfg2, iniciar_worker=False)
    c2 = entrar(app2, "boot@empresa.com", "senha-boot-12345")
    assert c2.get("/api/admin/restaurar").get_json() == {"permitido": True}
    # backup adulterado é recusado sem gravar nada
    ruim = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(bk)) as zi, zipfile.ZipFile(ruim, "w") as zo:
        for n in zi.namelist():
            d = zi.read(n)
            zo.writestr(n, d + b"x" if n.startswith("envios/") else d)
    r = c2.post("/api/admin/restaurar", data={"arquivo": (io.BytesIO(ruim.getvalue()), "b.zip")},
                headers={"X-CSRF-Token": c2.csrf}, content_type="multipart/form-data")
    assert r.status_code == 400 and not list(cfg2.ENVIOS_DIR.iterdir())
    r = c2.post("/api/admin/restaurar", data={"arquivo": (io.BytesIO(bk), "b.zip")},
                headers={"X-CSRF-Token": c2.csrf}, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    # a sessão do admin provisório cai; entra-se com a conta do backup
    assert c2.get("/api/relatorio").status_code == 401
    c3 = entrar(app2, "adm@teste.com", SENHA_ADM)
    depois = c3.get("/api/relatorio").get_json()
    chave = lambda rel: sorted((r["id"], r["data"], r["lat"], r["lon"], r["classe"]) for r in rel["registros"])
    assert chave(depois) == chave(antes)
    assert [p.name for p in cfg2.ENVIOS_DIR.iterdir()] == [env["sha256"] + ".zip"] if "sha256" in env else True
    fid = depois["registros"][0]["id"]
    assert c3.get(f"/api/fotos/{fid}/imagem").status_code == 200
