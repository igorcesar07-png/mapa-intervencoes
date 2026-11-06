#!/bin/bash
# Publica uma versão: atualizar.sh <main|develop> <commit>
# Chamado por /opt/mapa/puxar.sh. Os dados (/srv/mapa) nunca são tocados.
set -uo pipefail
ramo="$1"; sha="$2"
case "$ramo" in main) svc=producao ;; develop) svc=homolog ;; *) echo "ramo inválido"; exit 2 ;; esac
cd /opt/mapa
echo "=== $(date -Is) $ramo ($svc) → ${sha:0:7}"

# configuração do Docker/Caddy vem sempre da produção (main)
if [ "$ramo" = main ] || [ ! -f compose.yml ]; then
  cp src/main/deploy/oracle/compose.yml src/main/deploy/oracle/Caddyfile /opt/mapa/
fi

falhou() { echo "FALHOU: $1 — a versão anterior continua no ar."; echo "$sha" > estado/$ramo.falhou; exit 1; }

# cópia do banco antes de trocar a versão da produção (guarda as 10 últimas)
if [ "$svc" = producao ] && docker ps --format '{{.Names}}' | grep -qx mapa-producao; then
  docker exec mapa-producao python -c "
import sqlite3,datetime,os,glob
d='/var/data/backups'; os.makedirs(d,exist_ok=True)
s=sqlite3.connect('/var/data/instancia/mapa.sqlite3'); t=sqlite3.connect(f'{d}/antes-atualizacao-{datetime.datetime.now():%Y%m%d-%H%M%S}.sqlite3'); s.backup(t); t.close()
for f in sorted(glob.glob(f'{d}/antes-atualizacao-*.sqlite3'))[:-10]: os.remove(f)
" && echo "cópia do banco feita" || echo "aviso: cópia do banco não foi feita"
fi

docker image inspect mapa-$svc:atual >/dev/null 2>&1 && docker tag mapa-$svc:atual mapa-$svc:anterior
docker compose build $svc || falhou "construção/testes"
docker compose up -d $svc caddy || falhou "início do contêiner"

for i in $(seq 1 45); do
  st=$(docker inspect --format '{{.State.Health.Status}}' mapa-$svc 2>/dev/null || echo ?)
  [ "$st" = healthy ] && break
  sleep 4
done
if [ "$st" != healthy ]; then
  if docker image inspect mapa-$svc:anterior >/dev/null 2>&1; then
    docker tag mapa-$svc:anterior mapa-$svc:atual && docker compose up -d --no-build $svc
    echo "voltou para a versão anterior"
  fi
  falhou "não respondeu em /saude"
fi
echo "$sha" > estado/$ramo.sha; rm -f estado/$ramo.falhou
docker image prune -f >/dev/null
echo "OK: $svc no ar com ${sha:0:7}"
