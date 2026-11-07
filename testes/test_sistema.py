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


def enviar(c, dados, nome="lote-teste.zip", empresa="Empresa Teste Ltda"):
    return c.post("/api/admin/envios", data={"arquivo": (io.BytesIO(dados), nome), "empresa": empresa},
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

    # importadas ficam na revisão (fora do mapa) até o administrador confirmar
    assert c.get("/api/relatorio").get_json()["registros"] == []
    fila = {e: c.get(f"/api/admin/revisao?estado={e}").get_json()["itens"] for e in ("aguardando", "pendente")}
    auto = {i["arquivo"]: i for i in fila["aguardando"]}
    assert set(auto) == {"WhatsApp Image 2026-10-22 at 15.05.37.jpeg", "WhatsApp Image 2026-10-22 at 20.14.14 (2).jpeg",
                         "WhatsApp Image 2026-10-22 at 16.33.36.jpeg"}
    # carimbo do app da concessionária: lote e estaca lidos na foto conferem com a coordenada
    cb = auto["WhatsApp Image 2026-10-22 at 16.33.36.jpeg"]
    assert cb["carimbo"]["lote"] == "2" and cb["carimbo"]["est"] == 182691 and cb["lote"] == "2" and cb["empresa"] == "Empresa Teste Ltda"
    assert [i["arquivo"] for i in fila["pendente"]] == ["WhatsApp Image 2026-10-22 at 16.24.37.jpeg"]
    assert not fila["pendente"][0]["sugestao"]          # sem dados: nada é atribuído automaticamente
    # a mesma intervenção reenviada com outro arquivo (bytes diferentes, mesmo carimbo): pendente como provável reenvio
    from PIL import Image as _I
    im = _I.open(AMOSTRAS / "WhatsApp Image 2026-10-22 at 16.33.36.jpeg"); bb = io.BytesIO(); im.save(bb, "JPEG", quality=80)
    r3 = enviar(c, zip_bytes([("reenvio.jpg", bb.getvalue())]), "reenvio.zip")
    esperar(c, r3.get_json()["envio_id"])
    pend = c.get("/api/admin/revisao?estado=pendente").get_json()["itens"]
    assert any(i["arquivo"] == "reenvio.jpg" and any(d["campo"] == "duplicata" for d in i["divergencias"]) for i in pend)
    fila["pendente"] = [i for i in pend if i["arquivo"] != "reenvio.jpg"]
    rc = c.post(f"/api/admin/envios/{env['id']}/confirmar", headers={"X-CSRF-Token": c.csrf}).get_json()
    for i in fila["pendente"]:
        if i["sugestao"]:
            assert c.post(f"/api/admin/revisao/{i['foto_id']}", json={"acao": "confirmar", "usar_sugestao": True},
                          headers={"X-CSRF-Token": c.csrf}).status_code == 200
    rel = c.get("/api/relatorio").get_json()
    regs = {r["arquivo"]: r for r in rel["registros"]}
    a = regs["WhatsApp Image 2026-10-22 at 15.05.37.jpeg"]       # carimbo decimal
    assert a["data"] == "2026-10-22T14:54:56" and a["data_fonte"] == "carimbo"
    assert abs(a["lat"] + 25.47282) < 1e-4 and abs(a["lon"] + 49.89629) < 1e-4
    b = regs["WhatsApp Image 2026-10-22 at 16.33.36.jpeg"]       # app da concessionária: estaca 182+691
    assert b["d"]["bloco"] == "2" and abs(b["d"]["est"] - 182691) < 60
    assert rc["confirmadas"] == 3 and rc["pendentes"] == 1
    assert a["empresa"] == "Empresa Teste Ltda"
    # GPS indisponível -> ocorrência e pendente de revisão (fora do mapa), sem inventar posição
    assert "WhatsApp Image 2026-10-22 at 16.24.37.jpeg" not in regs
    sem = fila["pendente"][0] | {"id": fila["pendente"][0]["foto_id"]}
    assert sem["lat_foto"] is None and sem["data"] is not None
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
    assert len(c.get("/api/relatorio").get_json()["registros"]) == len(FOTOS) - 1   # a sem coordenada segue pendente


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
    # exportar dados é só do administrador: o visualizador é barrado no servidor, com ou sem filtros
    for q in ("", "?lote=L1", "?dia=2026-10-29", "?bloco=2", "?mes=2026-10"):
        r = v.get("/api/exportar.csv" + q)
        assert r.status_code == 403, q
        assert not any(n in r.get_data(as_text=True) for n in RESTRITOS | VALIDOS), q
    # fotos restritas: 404 (nem revela que existem)
    for i in range(2, 10):
        assert v.get(f"/api/fotos/{i:016x}/miniatura").status_code == 404
        assert v.get(f"/api/fotos/{i:016x}/imagem").status_code == 404
    assert v.get(f"/api/fotos/{1:016x}/miniatura").status_code == 200
    # a página do visualizador não traz as seções do administrador
    html = v.get("/").get_data(as_text=True)
    assert "anomList" not in html and "semGrid" not in html and 'href="/admin"' not in html and 'id="btnCsv"' not in html and 'id="lCalor"' not in html

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
    v = entrar(app, "adm@teste.com", SENHA_ADM)
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
    kmz = (AMOSTRAS / "B2B3_KMZ_Estaca.kmz").read_bytes()
    c.post("/api/admin/eixo", data={"arquivo": (io.BytesIO(kmz), "B2B3_KMZ_Estaca.kmz")},
           headers={"X-CSRF-Token": c.csrf}, content_type="multipart/form-data")
    r = enviar(c, zip_bytes([(p.name, p.read_bytes()) for p in FOTOS]), "05-11 - lote.zip")
    env, _ = esperar(c, r.get_json()["envio_id"])
    assert env["estado"] == "concluido"
    c.post(f"/api/admin/envios/{env['id']}/confirmar", headers={"X-CSRF-Token": c.csrf})
    antes = c.get("/api/relatorio").get_json()
    assert antes["registros"]
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


def test_data_muito_antes_do_envio_e_correcao_manual(app):
    _semear(app)
    con = conectar(app.config["MAPA"].DB_PATH)
    fid = f"{30:016x}"
    # OCR leu "4" em vez de "31": carimbo 04/10, enviada pelo WhatsApp em 31/10
    processamento.gravar_foto(con, {"id": fid, "arquivo": "WhatsApp Image 2026-10-31 at 17.44.41 (2).jpeg", "lote": "L1",
                                    "data": "2026-10-04T14:48:19", "data_fonte": "carimbo", "lat": -25.2, "lon": -51.0,
                                    "classe": "depois", "thumb": _jpeg(), "importado_em": "2026-11-05T20:00:00"}, None, None)
    con.execute("UPDATE parametros SET valor=CAST(valor AS INTEGER)+1 WHERE chave='versao_dados'")
    oc = con.execute("SELECT tipo FROM ocorrencias WHERE foto_id=? AND resolvida=0", (fid,)).fetchall()
    con.close()
    visao.invalidar()
    assert ("data_antes_envio",) in [tuple(r) for r in oc]
    a = entrar(app, "adm@teste.com", SENHA_ADM)
    lista = a.get("/api/admin/fotos?classe=datas").get_json()["fotos"]
    f = next(x for x in lista if x["id"] == fid)
    assert f["sugestao_data"] == "2026-10-31T14:48:19"
    # visualizador não corrige; data impossível é recusada
    v = entrar(app, "vis@teste.com", SENHA_VIS)
    assert v.post(f"/api/admin/fotos/{fid}/data", json={"data": "2026-10-31T14:48"}, headers={"X-CSRF-Token": v.csrf}).status_code == 403
    assert a.post(f"/api/admin/fotos/{fid}/data", json={"data": "2026-10-33T14:48"}, headers={"X-CSRF-Token": a.csrf}).status_code == 400
    r = a.post(f"/api/admin/fotos/{fid}/data", json={"data": "2026-10-31T14:48"}, headers={"X-CSRF-Token": a.csrf})
    assert r.status_code == 200 and r.get_json()["data"] == "2026-10-31T14:48:00" and r.get_json()["data_original"] == "2026-10-04T14:48:19"
    reg = next(x for x in a.get("/api/relatorio").get_json()["registros"] if x["id"] == fid)
    assert reg["data"] == "2026-10-31T14:48:00" and reg["data_fonte"] == "manual"
    assert not [x for x in a.get("/api/admin/fotos?classe=datas").get_json()["fotos"] if x["id"] == fid]
    # a sinalização na partida não volta a abrir a ocorrência de uma foto já corrigida
    con = conectar(app.config["MAPA"].DB_PATH)
    assert processamento.sinalizar_datas_antes_do_envio(con) == 0
    con.close()


def test_marca_publica_sem_login_e_recursos_protegidos(app):
    c = app.test_client()
    r = c.get("/marca/via-araucaria-logo.png")
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert c.get("/marca/../relatorio.js").status_code == 404
    assert c.get("/recursos/relatorio.js").status_code in (302, 401, 403)
    pagina = c.get("/login").get_data(as_text=True)
    assert "/marca/via-araucaria-logo.png" in pagina and "Overpass" in pagina


# ------------------------------------------------------------------ decisão do administrador sobre inconsistências
def _decidir(c, foto_id, **corpo):
    return c.post(f"/api/admin/fotos/{foto_id}/decisao", json=corpo, headers={"X-CSRF-Token": c.csrf})


def test_decisao_incorporar_remover_excluir_restaurar(app):
    _semear(app)
    adm = entrar(app, "adm@teste.com", SENHA_ADM)
    vis = entrar(app, "vis@teste.com", SENHA_VIS)
    anom, sem = f"{5:016x}", f"{6:016x}"
    rel = adm.get("/api/relatorio").get_json()
    d5 = next(r for r in rel["registros"] if r["id"] == anom)["d"]
    assert d5["anomalia"] and d5.get("est") is not None          # sugestão disponível (estaca mais próxima)
    n_tapados = sum(1 for gp in rel["grupos"] if gp["tapado"])

    # só administrador, com CSRF, e estaca precisa estar no eixo
    assert _decidir(vis, anom, acao="remover").status_code == 403
    assert adm.post(f"/api/admin/fotos/{anom}/decisao", json={"acao": "remover"}).status_code in (400, 403)
    assert _decidir(adm, anom, acao="incorporar", bloco="2", est=999999).status_code == 400
    assert _decidir(adm, anom, acao="incorporar", bloco="9", est=100500).status_code == 400
    assert _decidir(adm, sem, acao="incorporar", bloco="2").status_code == 400      # sem estaca: nada automático

    # incorporar: vira ponto normal na estaca escolhida, sai das inconsistências, entra no calor e no visualizador
    r = _decidir(adm, anom, acao="incorporar", bloco="2", est=100500)
    assert r.status_code == 200, r.get_json()
    rel = adm.get("/api/relatorio").get_json()
    reg = next(x for x in rel["registros"] if x["id"] == anom)
    assert not reg["d"]["anomalia"] and reg["d"]["incorporada"] and reg["d"]["est"] == 100500 and reg["d"]["bloco"] == "2"
    assert reg["coord_original"] == [-25.05, -49.95] and abs(reg["lat"] + 25.0) < 1e-6
    assert sum(1 for gp in rel["grupos"] if gp["tapado"]) == n_tapados + 1
    assert any(x["id"] == anom for x in vis.get("/api/relatorio").get_json()["registros"])
    csv = adm.get("/api/exportar.csv").get_data(as_text=True)
    assert "incorporada na estaca" in csv
    # o registro original não muda
    con = conectar(app.config["MAPA"].DB_PATH)
    assert tuple(con.execute("SELECT lat, lon FROM fotos WHERE id=?", (anom,)).fetchone()) == (-25.05, -49.95)

    # foto sem coordenada também pode ser incorporada, com a estaca informada
    assert _decidir(adm, sem, acao="incorporar", bloco="2", est=101000).status_code == 200
    reg = next(x for x in adm.get("/api/relatorio").get_json()["registros"] if x["id"] == sem)
    assert reg["lat"] is not None and reg["d"]["incorporada"]

    # remover do mapa de calor: fica guardada, não é inconsistência, não conta
    travado = f"{8:016x}"
    assert _decidir(adm, travado, acao="remover").status_code == 200
    rel = adm.get("/api/relatorio").get_json()
    reg = next(x for x in rel["registros"] if x["id"] == travado)
    assert reg["d"]["removida"] and not reg["d"]["anomalia"] and not reg["d"].get("grupo")
    assert not any(x["id"] == travado for x in vis.get("/api/relatorio").get_json()["registros"])
    assert "removida do mapa de calor" in adm.get("/api/exportar.csv").get_data(as_text=True)
    assert con.execute("SELECT COUNT(*) FROM fotos WHERE id=?", (travado,)).fetchone()[0] == 1

    # excluir exige confirmação; some de tudo, mas o original fica e dá para restaurar
    assert _decidir(adm, travado, acao="excluir").status_code == 400
    assert _decidir(adm, travado, acao="excluir", confirmar=True, motivo="foto de teste").status_code == 200
    rel = adm.get("/api/relatorio").get_json()
    assert not any(x["id"] == travado for x in rel["registros"])
    assert rel["excluidas"][0]["id"] == travado and rel["excluidas"][0]["motivo"] == "foto de teste"
    assert "GPS_TRAVADO_A.jpg" not in adm.get("/api/exportar.csv").get_data(as_text=True)
    assert adm.get(f"/api/fotos/{travado}/miniatura").status_code == 200       # administrador ainda vê para restaurar
    assert vis.get(f"/api/fotos/{travado}/miniatura").status_code == 404
    assert con.execute("SELECT COUNT(*) FROM fotos WHERE id=?", (travado,)).fetchone()[0] == 1
    assert _decidir(adm, travado, acao="restaurar").status_code == 200
    reg = next(x for x in adm.get("/api/relatorio").get_json()["registros"] if x["id"] == travado)
    assert reg["d"]["removida"]                                              # volta como estava antes de excluir

    # desfazer: volta às regras automáticas (inconsistência de novo)
    assert _decidir(adm, anom, acao="desfazer").status_code == 200
    reg = next(x for x in adm.get("/api/relatorio").get_json()["registros"] if x["id"] == anom)
    assert reg["d"]["anomalia"] and not reg["d"].get("incorporada") and "coord_original" not in reg

    # histórico com antes e depois
    h = adm.get(f"/api/admin/fotos/{anom}/historico").get_json()["historico"]
    assert [x["acao"] for x in h] == ["decisao_desfazer", "decisao_incorporar"]
    assert h[1]["detalhe"]["depois"]["est"] == 100500
    con.close()


# ------------------------------------------------------------------ classificação pelo carimbo e fila de revisão
from mapa import classificacao  # noqa: E402

EIXO2 = {"2": {"rodovia": "BR-277", "pts": [[-25.0, -50.0 + i * 0.0002, 100000 + i * 20] for i in range(500)]}}
PARAMS = {"limite_anomalia_m": "150"}


def _cls(**r):
    base = {"id": "x", "lat": None, "lon": None, "rodovia": None, "extras": {}}
    base.update(r)
    return classificacao.classificar(base, EIXO2, PARAMS)


def test_classificacao_prioriza_carimbo_e_aponta_divergencias():
    ok = _cls(lat=-25.0, lon=-49.99, rodovia="BR-277", extras={"lote_carimbo": "2", "est_carimbo": 101000})
    assert ok["estado"] == "auto" and ok["final"]["lote"] == "2" and ok["final"]["est"] == 101000
    # só estaca e lote no carimbo, sem coordenada: posição vem do eixo na estaca
    so_est = _cls(rodovia="BR-277", extras={"lote_carimbo": "02", "est_carimbo": 101000})
    assert so_est["estado"] == "auto" and abs(so_est["final"]["lat"] + 25.0) < 1e-9
    # estaca do carimbo longe da que a coordenada indica: pendente, nada trocado
    div = _cls(lat=-25.0, lon=-49.99, rodovia="BR-277", extras={"lote_carimbo": "2", "est_carimbo": 103000})
    assert "103+000, mas a coordenada aponta 101+000 (2.000 m de diferença)" in div["divergencias"][0]["msg"]
    assert div["estado"] == "pendente" and div["final"] is None and any(d["campo"] == "estaca" for d in div["divergencias"])
    assert div["sugestao"]["est"] == 103000 and div["sugestao"]["fonte"] == "estaca do carimbo"
    # segmento fora dos elegíveis
    fora = _cls(rodovia="BR-376", extras={"lote_carimbo": "5", "est_carimbo": 100500})
    assert fora["estado"] == "pendente" and any(d["campo"] == "segmento" for d in fora["divergencias"])
    # lote 4 elegível, mas eixo não carregado: não valida, fica pendente
    l4 = _cls(rodovia="BR-373", extras={"lote_carimbo": "4", "est_carimbo": 203100})
    assert l4["estado"] == "pendente" and any(d["campo"] == "eixo" for d in l4["divergencias"])
    # coordenada longe do eixo
    longe = _cls(lat=-25.05, lon=-49.95)
    assert longe["estado"] == "pendente" and any(d["campo"] == "coordenada" for d in longe["divergencias"])
    assert longe["sugestao"] is None                     # coordenada longe do eixo não vira sugestão
    outro = _cls(lat=-25.0, lon=-49.99, rodovia="BR-373", extras={"lote_carimbo": "4", "est_carimbo": 203100})
    assert outro["estado"] == "pendente" and outro["sugestao"] is None   # carimbo diz lote 4: não sugere o lote 2
    # sem dados
    nada = _cls()
    assert nada["estado"] == "pendente" and nada["sugestao"] is None
    # texto do carimbo novo, relido do OCR guardado (fotos antigas)
    txt = "Data e hora: 31/10/2026 11:02 | Latitude: -25.000000 | Longitude: -49.990000 | Rodovia: BR-277/PR · Lote 02 | Localização: 101+000 — Decrescente | Faixa 1 | Nota: Tapa buraco"
    antiga = _cls(lat=-25.0, lon=-49.99, rodovia="BR-277", extras={"texto_ocr": txt})
    assert antiga["carimbo"]["lote"] == "2" and antiga["carimbo"]["est"] == 101000 and antiga["carimbo"]["nota"] == "Tapa buraco"
    assert antiga["estado"] == "auto"


def test_revisao_importacao_empresa_e_permissoes(app):
    _semear(app)
    con = conectar(app.config["MAPA"].DB_PATH)
    # foto já no mapa cujo carimbo diz outra estaca: sinalizada, mas continua no mapa
    con.execute("UPDATE fotos SET extras=? WHERE id=?",
                (json.dumps({"texto_ocr": "Rodovia: BR-277/PR · Lote 02 | Localização: 105+000 — Crescente"}), f"{1:016x}"))
    from mapa import processamento as P
    assert P.revisar_fotos_existentes(con) == 1
    con.close()
    visao.invalidar()
    adm = entrar(app, "adm@teste.com", SENHA_ADM)
    vis = entrar(app, "vis@teste.com", SENHA_VIS)
    rel = adm.get("/api/relatorio").get_json()
    r1 = next(r for r in rel["registros"] if r["id"] == f"{1:016x}")
    assert r1["divergencias"] and rel["revisao"] == {"divergente": 1}
    assert any(r["id"] == f"{1:016x}" for r in vis.get("/api/relatorio").get_json()["registros"])   # não some do mapa
    dv = adm.get("/api/admin/revisao?estado=divergente").get_json()["itens"][0]
    assert dv["carimbo"]["est"] == 105000 and dv["sistema"]["lote"] == "2"

    # só o administrador vê e decide (o servidor recusa o visualizador)
    assert vis.get("/api/admin/revisao").status_code == 403
    assert vis.post(f"/api/admin/revisao/{1:016x}", json={"acao": "manter_sistema"}, headers={"X-CSRF-Token": vis.csrf}).status_code == 403
    assert vis.post("/api/admin/envios/1/confirmar", headers={"X-CSRF-Token": vis.csrf}).status_code == 403
    assert vis.post("/api/admin/envios", data={"arquivo": (io.BytesIO(b"x"), "a.zip"), "empresa": "X"},
                    headers={"X-CSRF-Token": vis.csrf}, content_type="multipart/form-data").status_code == 403

    # manter como está: encerra a divergência sem mexer na foto
    h = {"X-CSRF-Token": adm.csrf}
    assert adm.post(f"/api/admin/revisao/{1:016x}", json={"acao": "manter_sistema"}, headers=h).status_code == 200
    rel = adm.get("/api/relatorio").get_json()
    assert rel["revisao"] == {} and not next(r for r in rel["registros"] if r["id"] == f"{1:016x}").get("divergencias")

    # foto pendente (simula importação): fora do mapa; correção com segmento inválido é recusada; estaca válida inclui
    con = conectar(app.config["MAPA"].DB_PATH)
    con.execute("INSERT INTO revisao_foto(foto_id, estado, criado_em) VALUES(?, 'pendente', ?)", (f"{6:016x}", agora()))
    con.execute("UPDATE parametros SET valor=CAST(valor AS INTEGER)+1 WHERE chave='versao_dados'")
    con.close()
    visao.invalidar()
    assert not any(r["id"] == f"{6:016x}" for r in adm.get("/api/relatorio").get_json()["registros"])
    assert adm.get(f"/api/fotos/{6:016x}/miniatura").status_code == 200      # o administrador vê a foto pendente
    assert vis.get(f"/api/fotos/{6:016x}/miniatura").status_code == 404
    url = f"/api/admin/revisao/{6:016x}"
    assert adm.post(url, json={"acao": "confirmar"}, headers=h).status_code == 400                       # sem classificação
    assert adm.post(url, json={"acao": "corrigir", "rodovia": "BR-376", "lote": "5", "est": 100100}, headers=h).status_code == 400
    assert adm.post(url, json={"acao": "corrigir", "rodovia": "BR-373", "lote": "4", "est": 100100}, headers=h).status_code == 400
    assert adm.post(url, json={"acao": "corrigir", "rodovia": "BR-277", "lote": "2", "est": 999999}, headers=h).status_code == 400
    assert adm.post(url, json={"acao": "corrigir", "rodovia": "BR-277", "lote": "2", "lat": -25.3, "lon": -49.9}, headers=h).status_code == 400
    r = adm.post(url, json={"acao": "corrigir", "rodovia": "BR-277", "lote": "2", "est": 104000}, headers=h)
    assert r.status_code == 200 and r.get_json()["estado"] == "confirmada"
    reg = next(r for r in adm.get("/api/relatorio").get_json()["registros"] if r["id"] == f"{6:016x}")
    assert reg["d"]["est"] == 104000 and reg["d"]["bloco"] == "2" and reg["coord_original"] == [None, None]

    # empresa: obrigatória no envio, editável pelo administrador, aparece no relatório e no CSV
    assert enviar(adm, b"PK", "x.zip", empresa="").status_code == 400
    con = conectar(app.config["MAPA"].DB_PATH)
    con.execute("INSERT INTO envios(id, sha256, nome_original, arquivo, tamanho, lote, estado, criado_em) VALUES(1,'s','a.zip','a.zip',1,'L1','concluido',?)",
                (agora(),))
    con.execute("UPDATE fotos SET envio_id=1")
    con.close()
    assert adm.post("/api/admin/envios/1/empresa", json={"empresa": "Neovia Conservação"}, headers=h).status_code == 200
    reg = next(r for r in vis.get("/api/relatorio").get_json()["registros"] if r["id"] == f"{1:016x}")
    assert reg["empresa"] == "Neovia Conservação"
    assert "Neovia Conservação" in adm.get("/api/exportar.csv").get_data(as_text=True)
    h2 = adm.get(f"/api/admin/fotos/{6:016x}/historico").get_json()["historico"]
    assert h2[0]["acao"] == "revisao_corrigir"
