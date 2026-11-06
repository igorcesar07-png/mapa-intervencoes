# Mapa de Intervenções — imagem de produção/homologação
FROM python:3.13-slim

# Tesseract (leitura do carimbo das fotos) — o mesmo programa usado no computador
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    MAPA_HOST=0.0.0.0 MAPA_HTTPS=1 MAPA_ATRAS_DE_PROXY=1 MAPA_PROXY_CONFIAVEL=* \
    MAPA_DADOS_DIR=/var/data/instancia

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY atualizar_dados.py executar.py gerenciar.py pytest.ini ./
COPY mapa ./mapa
COPY testes ./testes

# testes rodam na construção: se falharem, a imagem não é publicada
RUN python -m pytest -q -p no:cacheprovider && rm -rf /tmp/pytest-*

# usuário sem privilégios; a pasta de dados é o disco persistente montado pela hospedagem
RUN useradd --create-home --uid 10001 mapa && mkdir -p /var/data && chown mapa /var/data
USER mapa

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/saude'%os.environ.get('PORT','8080'),timeout=4)"
CMD ["python", "executar.py"]
