#!/usr/bin/env python3
"""Inicia o servidor. Por padrão só escuta neste computador (127.0.0.1).

  python executar.py                 # http://127.0.0.1:8080
  MAPA_PORTA=9000 python executar.py

Para abrir a outras máquinas, ponha um proxy HTTPS na frente (nginx, Caddy…),
defina MAPA_HTTPS=1 e MAPA_HOST=0.0.0.0 — só com autorização de quem responde pela rede.
Use um único processo: a fila de processamento roda dentro dele.
"""
import os

from mapa import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("MAPA_HOST", "127.0.0.1")
    # PORT é a variável padrão das plataformas de hospedagem (Render, Railway…)
    porta = int(os.environ.get("MAPA_PORTA") or os.environ.get("PORT") or "8080")
    try:
        from waitress import serve
        print(f"Mapa de Intervenções em http://{host}:{porta}")
        extra = {}
        if os.environ.get("MAPA_ATRAS_DE_PROXY") == "1":
            # publicado por túnel (Cloudflare/Tailscale) que conecta neste computador:
            # usa o IP real do visitante (limite de tentativas de login) e reconhece o HTTPS
            # MAPA_PROXY_CONFIAVEL: 127.0.0.1 no túnel local; "*" na hospedagem, onde só o
            # balanceador da plataforma alcança o contêiner
            extra = dict(trusted_proxy=os.environ.get("MAPA_PROXY_CONFIAVEL", "127.0.0.1"),
                         trusted_proxy_count=int(os.environ.get("MAPA_PROXY_SALTOS", "1")),
                         trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"},
                         clear_untrusted_proxy_headers=True)
            print("Modo publicação: atrás de proxy HTTPS.")
        serve(app, host=host, port=porta, threads=8, max_request_body_size=app.config["MAX_CONTENT_LENGTH"], **extra)
    except ImportError:
        app.run(host=host, port=porta, threaded=True)
