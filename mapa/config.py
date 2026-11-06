"""Configuração lida do ambiente. Nenhuma senha ou chave fica no código."""
import os
import secrets
import stat
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _int(nome, padrao):
    try:
        return int(os.environ.get(nome, padrao))
    except ValueError:
        return padrao


class Config:
    def __init__(self, dados_dir=None, **extra):
        self.DADOS_DIR = Path(dados_dir or os.environ.get("MAPA_DADOS_DIR", RAIZ / "instancia")).resolve()
        self.DB_PATH = self.DADOS_DIR / "mapa.sqlite3"
        self.ENVIOS_DIR = self.DADOS_DIR / "envios"          # ZIPs enviados (somente leitura)
        self.TMP_DIR = self.DADOS_DIR / "tmp"
        self.HTTPS = os.environ.get("MAPA_HTTPS", "0") == "1"  # liga cookie Secure atrás de HTTPS
        self.MAX_ZIP_BYTES = _int("MAPA_MAX_ZIP_MB", 2048) * 1024 * 1024
        self.MAX_DESCOMPACTADO_BYTES = _int("MAPA_MAX_DESCOMPACTADO_MB", 8192) * 1024 * 1024
        self.MAX_MEMBROS = _int("MAPA_MAX_ARQUIVOS_ZIP", 10000)
        self.MAX_FOTO_BYTES = _int("MAPA_MAX_FOTO_MB", 60) * 1024 * 1024
        self.MAX_RAZAO_COMPRESSAO = _int("MAPA_MAX_RAZAO_COMPRESSAO", 200)
        self.SESSAO_HORAS = _int("MAPA_SESSAO_HORAS", 12)
        self.LOGIN_MAX_FALHAS = _int("MAPA_LOGIN_MAX_FALHAS", 5)
        self.LOGIN_BLOQUEIO_MIN = _int("MAPA_LOGIN_BLOQUEIO_MIN", 15)
        self.WORKER = os.environ.get("MAPA_WORKER", "1") == "1"
        self.WORKER_THREADS = _int("MAPA_WORKER_THREADS", max(2, os.cpu_count() or 2))
        for k, v in extra.items():
            setattr(self, k, v)

    def preparar_pastas(self):
        for p in (self.DADOS_DIR, self.ENVIOS_DIR, self.TMP_DIR):
            p.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(p, 0o700)
            except OSError:
                pass

    def chave_secreta(self):
        """MAPA_SECRET_KEY do ambiente; senão um arquivo gerado na 1ª execução (permissão 600)."""
        env = os.environ.get("MAPA_SECRET_KEY")
        if env:
            return env
        arq = self.DADOS_DIR / "chave_secreta"
        if not arq.exists():
            fd = os.open(arq, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(32))
        return arq.read_text().strip()
