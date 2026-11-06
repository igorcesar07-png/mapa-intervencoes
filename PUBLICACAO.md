# Publicação online — GitHub + Render

## Arquitetura

```
 você edita o código ──► GitHub (repositório PRIVADO)
                           │  branch develop ──► CI (testes + imagem Docker) ──► Render: HOMOLOGAÇÃO (grátis)
                           │  branch main    ──► CI (testes + imagem Docker) ──► Render: PRODUÇÃO
                           │                                                     └─ disco persistente /var/data
                           └─ só atualiza se os testes passarem (autoDeployTrigger: checksPass)
```

* **Produção** (`main`): `https://mapa-intervencoes.onrender.com` (nome exato definido na criação).
  Serviço “Starter” (0,5 CPU, 512 MB) + disco de 10 GB em `/var/data` com banco, ZIPs originais e
  chave. A Render tira uma **cópia do disco a cada 24 h, guardada por 7 dias**.
* **Homologação** (`develop`): instância gratuita, **sem disco** — os dados de teste somem a cada
  atualização ou depois de 15 min parada (ela “dorme” e leva ~1 min para acordar). Serve para conferir
  mudanças antes de levar à produção. Nunca envie as fotos reais para lá.
* HTTPS automático (certificado da própria Render); cookie de sessão só por HTTPS; HSTS.
* Nenhum segredo no repositório: `MAPA_SECRET_KEY` é gerada pela Render; e-mail e senha do primeiro
  administrador são digitados no painel da Render (variáveis secretas) e só valem enquanto o banco não
  tiver nenhum usuário.
* Logs: painel da Render → serviço → **Logs** (inícios, envios processados, erros com rota). Histórico
  de cada atualização em **Events**; uma atualização com falha não substitui a versão que está no ar.
* Verificação de saúde: `/saude` (responde `{"ok": true}`, sem nenhum dado).

## Por que Render

| Requisito do sistema | Render | Railway | Fly.io | Vercel / Netlify |
|---|---|---|---|---|
| Disco persistente (SQLite + ZIPs) | ✔ 0,25 US$/GB, cópia diária | ✔ mas 5 GB no plano Hobby | ✔ | ✘ (sem disco) |
| Tesseract + processamento em segundo plano | ✔ (Docker) | ✔ | ✔ | ✘ (funções curtas) |
| Deploy automático pelo GitHub, por branch | ✔ nativo, espera o CI | ✔ | via GitHub Actions | ✔ |
| Dois ambientes no mesmo arquivo | ✔ `render.yaml` | ✔ | manual | — |
| Custo previsível | ✔ preço fixo | por uso | por uso | — |

Limitação conhecida: serviço com disco não tem “deploy sem interrupção” — a cada atualização o site
fica fora por alguns segundos. Um envio de ZIP que estiver sendo processado nesse momento é retomado
sozinho depois (a fila fica no banco).

## Custos (preços de nov/2026)

| Item | Mensal |
|---|---|
| Render — workspace Hobby | US$ 0 |
| Produção — instância Starter | US$ 7,00 |
| Produção — disco 10 GB | US$ 2,50 |
| Homologação — instância Free | US$ 0 |
| Tráfego: 5 GB/mês incluídos; acima disso | US$ 0,15/GB |
| GitHub — repositório privado + Actions (2.000 min/mês) | US$ 0 |
| **Total previsto** | **≈ US$ 9,50/mês** (≈ R$ 50–60, conforme o câmbio) |

Hoje os dados ocupam ~0,6 GB (ZIPs + banco); 10 GB dão folga para muitos lotes. O disco pode crescer
depois, mas não diminuir.

## Passo a passo (uma vez)

### 1. Repositório no GitHub (privado)
1. Crie a conta em github.com (se ainda não tiver) e instale o **GitHub Desktop**.
2. GitHub Desktop → *File → Add local repository* → escolha a pasta `mapa-intervencoes-github`
   (já vem com as branches `main` e `develop` e o primeiro commit).
3. **Publish repository** → marque **Keep this code private** → Publish. Publique também a branch
   `develop` (selecione-a e clique em *Publish branch*).
   > O repositório tem que ser **privado**: as amostras de teste são fotos reais do serviço.

### 2. Render
1. Crie a conta em render.com (pode entrar com o GitHub) e autorize o acesso **só a este repositório**.
2. **New → Blueprint** → escolha o repositório → a Render lê o `render.yaml` e mostra os dois serviços.
3. Preencha as variáveis pedidas (aparecem só nesta tela):
   * `MAPA_ADMIN_EMAIL`: e-mail da conta provisória (pode ser o seu).
   * `MAPA_ADMIN_SENHA_INICIAL`: senha forte, mín. 10 caracteres. Não use a senha do computador.
4. Cadastre o cartão (a produção é paga) e confirme. A primeira construção leva ~5 min.

### 3. Levar os dados atuais (sem perda)
1. No computador, dê dois cliques em **`FAZER_BACKUP.bat`** → gera `mapa-backup.zip` (banco + ZIPs
   originais, cada arquivo com SHA-256).
2. Abra o endereço de **produção**, entre com a conta provisória → **Painel → Backup → Restaurar** e
   envie o `mapa-backup.zip`. Tudo é conferido antes de gravar; se o sistema já tiver qualquer dado,
   a restauração é recusada.
3. O sistema volta para o login: entre com **a sua conta de sempre** (mesmo e-mail e senha do
   computador). A conta provisória deixa de existir.
4. Na Render, apague `MAPA_ADMIN_SENHA_INICIAL` (*Environment*). Ela não é mais usada.
5. Confira os números (fotos, buracos tapados, inconsistências) com os do computador.

### 4. No dia a dia
* **Mudança no sistema**: commit na `develop` → homologação atualiza sozinha → conferiu? *Pull request*
  `develop → main` no GitHub → *Merge* → produção atualiza sozinha (só se os testes passarem).
* **Novas fotos**: enviadas direto pelo painel de produção (como hoje).
* **Backup fora da Render**: Painel → Backup → *Baixar backup completo* (ex.: uma vez por semana).
* **Voltar uma versão**: Render → serviço → Events → *Rollback* na versão anterior (o disco não muda).
