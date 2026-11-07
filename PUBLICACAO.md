# Publicação online — GitHub + Oracle Cloud (Always Free)

## Arquitetura

```
 você edita o código ──► GitHub (repositório PRIVADO) ──► CI: testes + imagem Docker
                              ▲
                              │ a cada 2 min o servidor confere main e develop (chave só de leitura)
                              │
 Oracle Cloud (VM gratuita, São Paulo) ── Ubuntu + Docker
   ├─ Caddy (HTTPS automático, Let's Encrypt)
   │    ├─ https://mapa.<ip>.sslip.io     → PRODUÇÃO    (branch main)
   │    └─ https://homolog.<ip>.sslip.io  → HOMOLOGAÇÃO (branch develop)
   └─ dados em /srv/mapa/producao e /srv/mapa/homolog (fora dos contêineres)
```

* **Atualização automática**: um `git push` na `main` (ou `develop`) é baixado pelo servidor em até
  2 min. A imagem é reconstruída e **os testes rodam dentro da construção**: se falharem, ou se a
  versão nova não responder em `/saude`, a anterior continua no ar (volta sozinha).
* **Sem perda de dados**: banco e ZIPs ficam em `/srv/mapa`, fora do Docker. Antes de cada
  atualização da produção é feita uma cópia do banco (guarda as 10 últimas) e, todo dia às 03:30, um
  backup completo (banco + ZIPs; 7 dias girando) em `/srv/mapa/producao/backups`. Backup fora do
  servidor: Painel → Backup → *Baixar backup completo*.
* **Segredos**: a chave de sessão de cada ambiente e a chave de acesso ao GitHub são geradas dentro
  do servidor na instalação; nada secreto fica no repositório. A chave do GitHub é **somente leitura**
  e só deste repositório.
* **Endereço**: `sslip.io` é um serviço gratuito que transforma o IP em nome (`mapa.1-2-3-4.sslip.io`
  → `1.2.3.4`), o que permite o certificado HTTPS gratuito. Com um domínio próprio, basta trocar o
  nome em `/opt/mapa/caddy.env`.
* **Logs**: `/var/log/mapa/deploy.log` (cada atualização, com o motivo de falha), `puxar.log`,
  `backup.log`, `/var/log/mapa-instalacao.log`; do sistema: `docker logs mapa-producao`.

## Custos

| Item | Mensal |
|---|---|
| VM Ampere A1 (até 2 OCPU / 12 GB) — Always Free | R$ 0 |
| Disco (até 200 GB no total) — Always Free | R$ 0 |
| Tráfego de saída (10 TB/mês) — Always Free | R$ 0 |
| HTTPS (Let's Encrypt) e nome (sslip.io) | R$ 0 |
| GitHub privado + Actions | R$ 0 |
| **Total** | **R$ 0** |

Cuidados: (1) a Oracle pode recolher máquinas *Always Free* ociosas (CPU abaixo de 20% por 7 dias);
converter a conta para *Pay As You Go* evita isso e continua sem custo dentro dos limites — mas aí
qualquer recurso criado fora do gratuito passa a ser cobrado. (2) Às vezes falta máquina ARM na
região; a alternativa gratuita é a VM AMD *micro* (1 GB; o instalador cria memória de troca).

## Instalação (uma vez)

1. **Conta Oracle** (Free Tier, região **Brazil East – São Paulo**).
2. **Criar a instância**: Ubuntu 24.04, forma *VM.Standard.A1.Flex* (2 OCPU, 12 GB), IP público,
   chave SSH do Cloud Shell, e em *Advanced options → Management → Cloud-init script* o conteúdo de
   `deploy/oracle/cloud-init.sh`.
3. **Liberar portas 80 e 443** na *Security List* da rede (VCN) da instância.
4. **Chave do servidor no GitHub**: pelo Cloud Shell, `ssh ubuntu@<ip> cat /opt/mapa/CHAVE_PUBLICA_GITHUB.txt`
   → GitHub → repositório → *Settings → Deploy keys → Add deploy key* (sem marcar *Allow write access*).
   Em até 2 min o servidor baixa o código e publica (a 1ª construção leva ~5–10 min).
5. **Primeiro administrador**: `ssh ubuntu@<ip>` e
   `sudo docker exec -it mapa-producao python gerenciar.py criar-admin --email voce@exemplo.com --nome "Seu nome"`
   (a senha é digitada na hora e não aparece).
6. **Trazer os dados**: no computador, `FAZER_BACKUP.bat` → `mapa-backup.zip`; no endereço de
   produção, Painel → Backup → *Restaurar*. Depois entre com a sua conta de sempre.

## No dia a dia

* **Mudança no sistema**: commit na `develop` → homologação atualiza → conferiu? *Pull request*
  `develop → main` → *Merge* → produção atualiza.
* **Ver o que aconteceu numa atualização**: `ssh ubuntu@<ip> tail -50 /var/log/mapa/deploy.log`.
* **Novas fotos**: enviadas pelo painel de produção.

## Cópia de leitura no GitHub Pages (`gh-pages`)

`https://igorcesar07-png.github.io/mapa-intervencoes/` é uma fotografia estática do relatório, gerada por
`ferramentas/pages/montar_pages.py` a partir de uma cópia do banco (precisa de `pip install cryptography`).

* **Visualizador** (`index.html`): só os dados que o visualizador do sistema recebe — buracos tapados
  válidos, uma foto por buraco. Inconsistências, fotos excluídas e as demais fotos **não estão** nos
  arquivos públicos. Não há botão de exportar.
* **Administrador** (`admin.html`): os dados completos ficam em `admin.bin` e `fotos/x/*.bin`, cifrados
  com AES-256-GCM; a chave é derivada da senha (PBKDF2-SHA256, 600 mil iterações). A senha não está em
  nenhum arquivo. Com a senha: inconsistências, todas as fotos e exportação CSV.
* **Limites** (o GitHub Pages só serve arquivos): não há contas individuais, nem envio de fotos, nem
  registro de quem acessou; a senha de administrador é uma só e, para trocá-la, o site é gerado de novo.
  Tudo que o visualizador vê pode ser salvo pelo próprio navegador — impedir isso de verdade exige não
  publicar os dados (sistema com login). Para contas, envio de arquivos e permissões no servidor, use
  o sistema (Flask) ou um serviço externo de autenticação e armazenamento.
