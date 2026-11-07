#!/bin/bash
# Mapa de Intervenções — preparação do servidor Oracle (Ubuntu 24.04) na 1ª inicialização.
# Colado em "Cloud-init script" ao criar a instância. Roda como root, uma única vez.
# Não contém senha nem chave: tudo que é secreto é gerado AQUI, dentro do servidor.
set -euxo pipefail
exec > >(tee -a /var/log/mapa-instalacao.log) 2>&1

REPO="git@github.com:viaaraucaria/mapa-intervencoes.git"
export DEBIAN_FRONTEND=noninteractive

# --- pacotes: Docker, Compose, git
apt-get update
apt-get install -y docker.io docker-compose-v2 git openssl curl cron
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'J'
{"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "5"}}
J
systemctl enable --now docker
usermod -aG docker ubuntu || true

# --- memória de troca em máquinas pequenas (ex.: VM AMD micro de 1 GB)
if [ "$(awk '/MemTotal/{print $2}' /proc/meminfo)" -lt 3000000 ] && [ ! -f /swapfile ]; then
  fallocate -l 3G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# --- firewall do Ubuntu da Oracle: libera só HTTP/HTTPS além do SSH
iptables -C INPUT -p tcp -m multiport --dports 80,443 -m state --state NEW -j ACCEPT 2>/dev/null || \
  iptables -I INPUT 1 -p tcp -m multiport --dports 80,443 -m state --state NEW -j ACCEPT
iptables -C INPUT -p udp --dport 443 -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p udp --dport 443 -j ACCEPT
command -v netfilter-persistent >/dev/null && netfilter-persistent save || true

# --- pastas: código (/opt/mapa), dados (/srv/mapa — fora do Docker, sobrevivem a qualquer atualização)
install -d -m 755 /opt/mapa /opt/mapa/src /opt/mapa/src/main /opt/mapa/src/develop /opt/mapa/estado /var/log/mapa
for amb in producao homolog; do
  install -d -m 700 -o 10001 -g 10001 /srv/mapa/$amb /srv/mapa/$amb/backups
  if [ ! -f /opt/mapa/$amb.env ]; then
    umask 077; echo "MAPA_SECRET_KEY=$(openssl rand -hex 32)" > /opt/mapa/$amb.env
  fi
done

# --- endereços HTTPS (sslip.io aponta o nome para o IP público; certificado Let's Encrypt pelo Caddy)
IP="$(curl -fsS --max-time 10 https://api.ipify.org || curl -fsS --max-time 10 https://ifconfig.me)"
TRACO="${IP//./-}"
if [ ! -f /opt/mapa/caddy.env ]; then
  printf 'DOMINIO_PRODUCAO=mapa.%s.sslip.io\nDOMINIO_HOMOLOG=homolog.%s.sslip.io\n' "$TRACO" "$TRACO" > /opt/mapa/caddy.env
fi

# --- chave SÓ DE LEITURA para o servidor baixar o repositório privado (a pública vai em GitHub > Deploy keys)
if [ ! -f /opt/mapa/chave_github ]; then
  ssh-keygen -q -t ed25519 -N "" -C "servidor-oracle-mapa (somente leitura)" -f /opt/mapa/chave_github
fi
chmod 600 /opt/mapa/chave_github
cp /opt/mapa/chave_github.pub /opt/mapa/CHAVE_PUBLICA_GITHUB.txt
# chave oficial do github.com (docs.github.com › GitHub's SSH key fingerprints)
echo "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" > /opt/mapa/github_known_hosts

# --- verificador de atualizações: a cada 2 min confere main e develop no GitHub
cat > /opt/mapa/puxar.sh <<EOP
#!/bin/bash
# Baixa main (produção) e develop (homologação); se mudou, reconstrói só aquele ambiente.
# A construção da imagem roda os testes: se falharem, a versão no ar continua.
set -uo pipefail
exec 9>/opt/mapa/estado/trava && flock -n 9 || exit 0
export GIT_SSH_COMMAND="ssh -i /opt/mapa/chave_github -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/opt/mapa/github_known_hosts"
for ramo in main develop; do
  dir=/opt/mapa/src/\$ramo
  if [ ! -d \$dir/.git ]; then
    git clone -q --depth 1 --branch \$ramo $REPO \$dir 2>>/var/log/mapa/puxar.log || continue
  fi
  git -C \$dir fetch -q --depth 1 origin \$ramo 2>>/var/log/mapa/puxar.log || continue
  novo=\$(git -C \$dir rev-parse FETCH_HEAD)
  [ "\$novo" = "\$(cat /opt/mapa/estado/\$ramo.sha 2>/dev/null)" ] && continue
  [ "\$novo" = "\$(cat /opt/mapa/estado/\$ramo.falhou 2>/dev/null)" ] && continue
  git -C \$dir reset -q --hard FETCH_HEAD
  [ -f /opt/mapa/src/main/deploy/oracle/atualizar.sh ] || continue
  bash /opt/mapa/src/main/deploy/oracle/atualizar.sh \$ramo \$novo >>/var/log/mapa/deploy.log 2>&1
done
EOP
chmod 700 /opt/mapa/puxar.sh

cat > /etc/systemd/system/mapa-puxar.service <<'U'
[Unit]
Description=Mapa de Intervencoes - baixa e publica atualizacoes do GitHub
After=docker.service network-online.target
[Service]
Type=oneshot
ExecStart=/opt/mapa/puxar.sh
U
cat > /etc/systemd/system/mapa-puxar.timer <<'U'
[Unit]
Description=Confere atualizacoes do GitHub a cada 2 minutos
[Timer]
OnBootSec=1min
OnUnitActiveSec=2min
[Install]
WantedBy=timers.target
U
systemctl daemon-reload
systemctl enable --now mapa-puxar.timer

# --- backup diário da produção (banco + ZIPs), 7 dias girando, às 03:30
cat > /etc/cron.d/mapa-backup <<'C'
30 3 * * * root docker exec mapa-producao python gerenciar.py backup --saida /var/data/backups/backup-$(date +\%a).zip >> /var/log/mapa/backup.log 2>&1
C

cat > /etc/logrotate.d/mapa <<'L'
/var/log/mapa/*.log { weekly rotate 8 compress missingok notifempty }
L

echo "PRONTO. Chave pública para o GitHub (Deploy keys, somente leitura):"
cat /opt/mapa/CHAVE_PUBLICA_GITHUB.txt
