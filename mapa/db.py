"""SQLite: esquema e acesso. Uma conexão por thread/requisição."""
import datetime as dt
import json
import sqlite3
import threading

ESQUEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS usuarios(
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  nome TEXT NOT NULL DEFAULT '',
  senha_hash TEXT NOT NULL,
  papel TEXT NOT NULL CHECK(papel IN ('admin','visualizador')),
  ativo INTEGER NOT NULL DEFAULT 1,
  versao_sessao INTEGER NOT NULL DEFAULT 1,
  criado_em TEXT NOT NULL,
  ultimo_login TEXT
);
CREATE TABLE IF NOT EXISTS tentativas_login(
  chave TEXT PRIMARY KEY, falhas INTEGER NOT NULL, primeira TEXT NOT NULL, bloqueado_ate TEXT
);
CREATE TABLE IF NOT EXISTS envios(
  id INTEGER PRIMARY KEY,
  sha256 TEXT NOT NULL UNIQUE,
  nome_original TEXT NOT NULL,
  arquivo TEXT NOT NULL,
  tamanho INTEGER NOT NULL,
  lote TEXT NOT NULL,
  origem TEXT NOT NULL DEFAULT 'upload',
  estado TEXT NOT NULL CHECK(estado IN ('aguardando','em_andamento','concluido','erro')),
  mensagem TEXT,
  total INTEGER NOT NULL DEFAULT 0,
  processadas INTEGER NOT NULL DEFAULT 0,
  novas INTEGER NOT NULL DEFAULT 0,
  repetidas INTEGER NOT NULL DEFAULT 0,
  ignoradas INTEGER NOT NULL DEFAULT 0,
  enviado_por INTEGER REFERENCES usuarios(id),
  criado_em TEXT NOT NULL, iniciado_em TEXT, concluido_em TEXT
);
CREATE TABLE IF NOT EXISTS fotos(
  id TEXT PRIMARY KEY,
  sha1 TEXT UNIQUE,
  envio_id INTEGER REFERENCES envios(id),
  membro TEXT,
  arquivo TEXT NOT NULL,
  lote TEXT,
  data TEXT, data_fonte TEXT,
  lat REAL, lon REAL, coord_fonte TEXT,
  km REAL, rodovia TEXT, local TEXT, sentido TEXT,
  classe TEXT CHECK(classe IS NULL OR classe IN ('antes','depois','outro')),
  motivo_exclusao TEXT,
  extras TEXT NOT NULL DEFAULT '{}',
  miniatura BLOB,
  importado_em TEXT NOT NULL,
  classificado_por INTEGER, classificado_em TEXT
);
CREATE INDEX IF NOT EXISTS fotos_envio ON fotos(envio_id);
CREATE TABLE IF NOT EXISTS ocorrencias(
  id INTEGER PRIMARY KEY,
  envio_id INTEGER REFERENCES envios(id),
  foto_id TEXT REFERENCES fotos(id),
  membro TEXT,
  tipo TEXT NOT NULL,
  mensagem TEXT NOT NULL,
  resolvida INTEGER NOT NULL DEFAULT 0,
  resolvida_por INTEGER, resolvida_em TEXT,
  criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eixo_blocos(
  bloco TEXT PRIMARY KEY, rodovia TEXT NOT NULL DEFAULT '', fonte TEXT NOT NULL DEFAULT '',
  pts TEXT NOT NULL, atualizado_em TEXT NOT NULL
);
-- decisão do administrador sobre uma foto (a linha original em "fotos" nunca é alterada):
--   remover    = fica fora do mapa de calor, mas a foto e o registro continuam guardados
--   incorporar = entra no mapa como ponto normal, na estaca escolhida (lat/lon do eixo nessa estaca)
--   excluida   = some do sistema (relatório, listas, exportações); continua no banco e pode ser restaurada
-- O histórico de cada mudança fica na auditoria (antes e depois).
CREATE TABLE IF NOT EXISTS decisoes_foto(
  foto_id TEXT PRIMARY KEY REFERENCES fotos(id),
  decisao TEXT CHECK(decisao IS NULL OR decisao IN ('remover','incorporar')),
  est REAL, bloco TEXT, lat REAL, lon REAL,
  excluida INTEGER NOT NULL DEFAULT 0,
  motivo TEXT,
  por INTEGER, quando TEXT
);
CREATE TABLE IF NOT EXISTS parametros(chave TEXT PRIMARY KEY, valor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS auditoria(
  id INTEGER PRIMARY KEY, usuario_id INTEGER, acao TEXT NOT NULL, detalhe TEXT, quando TEXT NOT NULL
);
"""

# Valores das regras já existentes no projeto (mesmos números do index.html).
PARAMETROS_PADRAO = {
    "limite_anomalia_m": "150",        # foto a mais disso do eixo = anomalia
    "gps_travado_ativo": "1",          # coordenada idêntica em fotos com >= 5 min de diferença
    "gps_travado_min": "5",
    "isolado_m": "5000",               # sem eixo: foto a mais de 5 km de qualquer outra
    "agrupa_m": "25",                  # mesmo buraco: <= 25 m do centro do grupo, mesmo dia
    "nao_classificadas_contam": "1",   # regra atual: foto sem classe antes/tapado conta como válida
    "versao_dados": "1",
}

_local = threading.local()


def agora():
    return dt.datetime.now().isoformat(timespec="seconds")


def conectar(caminho):
    con = sqlite3.connect(caminho, timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def inicializar(caminho):
    con = conectar(caminho)
    con.executescript(ESQUEMA)
    for k, v in PARAMETROS_PADRAO.items():
        con.execute("INSERT OR IGNORE INTO parametros(chave, valor) VALUES(?,?)", (k, v))
    con.close()


def parametros(con):
    return {r["chave"]: r["valor"] for r in con.execute("SELECT chave, valor FROM parametros")}


def set_parametro(con, chave, valor):
    con.execute("INSERT INTO parametros(chave, valor) VALUES(?,?) ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
                (chave, str(valor)))


def nova_versao(con):
    con.execute("UPDATE parametros SET valor = CAST(valor AS INTEGER) + 1 WHERE chave='versao_dados'")


def auditar(con, usuario_id, acao, detalhe=None):
    con.execute("INSERT INTO auditoria(usuario_id, acao, detalhe, quando) VALUES(?,?,?,?)",
                (usuario_id, acao, json.dumps(detalhe, ensure_ascii=False) if detalhe is not None else None, agora()))
