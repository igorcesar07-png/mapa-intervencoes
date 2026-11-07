# Mapa de Intervenções — versão web com login

Relatório e mapa de calor dos buracos tapados, acessível só para quem tem conta.
Dois papéis:

| | Administrador | Visualizador |
|---|---|---|
| Relatório, filtros, mapa de calor, gráficos | ✔ | ✔ (só dados válidos) |
| Exportar CSV | ✔ todas as fotos, com situação e motivo | ✔ só os buracos tapados válidos |
| Inconsistências, fotos sem coordenada, não relacionadas, buracos sem foto de tapado, fotos extras do mesmo buraco, duplicatas | ✔ | ✘ |
| Enviar ZIP, acompanhar processamento, ocorrências, classificar fotos, regras, eixo, usuários | ✔ | ✘ |

A filtragem por papel é feita **no servidor** (`mapa/visao.py`): o visualizador nunca recebe
os registros restritos, nem na API do relatório, nem no CSV, nem nas fotos (`404`).

## Instalação (uma vez)

```bash
python3 -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```
Também é preciso o **Tesseract OCR** (o mesmo do `atualizar_dados.py`):
Windows: https://github.com/UB-Mannheim/tesseract/wiki · Linux: `sudo apt install tesseract-ocr` · macOS: `brew install tesseract`.

## Conta inicial de administrador

```bash
python gerenciar.py criar-admin --email igordalmolin.eng@gmail.com --nome "Igor"
```
A senha (mín. 10 caracteres) é pedida no terminal e não aparece na tela. Só o hash (scrypt) vai
para o banco; nenhuma senha fica no código ou em arquivo de configuração. O comando recusa criar
um segundo administrador sem `--adicional`.

Outros comandos:
```bash
python gerenciar.py criar-usuario --email equipe@empresa.com --papel visualizador
python gerenciar.py redefinir-senha --email alguem@empresa.com
python gerenciar.py listar-usuarios
```
Pelo painel do administrador (aba **Usuários**) também dá para criar visualizadores: o sistema
gera uma senha temporária, mostrada uma única vez, que a pessoa troca em **Conta**.

## Trazer os dados atuais (22/10 e 29/10)

```bash
python gerenciar.py importar-legado ../projeto/dados.json \
  --zip "22-10_-_TAPA_BURACO.zip" --zip "Fotos_servic_o_29_10.zip"
```
Importa as fotos já lidas **com a classificação manual e as correções feitas**, o eixo dos
Blocos 2 e 3 e liga cada foto ao ZIP original (para abrir em tamanho real). Esses ZIPs ficam
registrados como enviados: se forem enviados de novo, são recusados como repetidos.

## Rodar

```bash
python executar.py            # http://127.0.0.1:8080 — só neste computador
```
Variáveis úteis: `MAPA_PORTA`, `MAPA_DADOS_DIR` (onde ficam banco e ZIPs; padrão `./instancia`),
`MAPA_MAX_ZIP_MB` (padrão 2048), `MAPA_SECRET_KEY` (se não definida, é gerada em
`instancia/chave_secreta`, com permissão 600).

**Publicação online:** ver `PUBLICACAO.md` (GitHub + servidor gratuito da Oracle, produção e
homologação, atualização automática, backup e migração dos dados). Rode sempre um único processo: a fila de processamento fica dentro dele.

## Envio de ZIP

1. **Validação na hora do envio:** extensão .zip, arquivo ZIP íntegro (`testzip`), sem senha,
   limite de tamanho, de quantidade de arquivos e de tamanho descompactado, razão de compressão
   suspeita (zip bomb) e ao menos uma foto. ZIP inválido é recusado com a mensagem do motivo.
2. **O ZIP aceito é guardado intacto**, com permissão somente leitura, em `instancia/envios/<sha256>.zip`.
   Nada é extraído para o disco: as fotos são lidas direto do ZIP, em memória.
3. **Processamento em segundo plano** (fila no banco, uma thread de trabalho): estados
   *aguardando → em andamento → concluído* ou *com erro*, com progresso (fotos lidas/total) e
   mensagem. Se o servidor reiniciar no meio, o envio volta para a fila e é refeito do início
   (as fotos só são gravadas no final, numa única transação).
4. **Não repete trabalho:** o mesmo ZIP (mesmo SHA-256) é recusado; a mesma foto (mesmo SHA-1 do
   conteúdo, que é o `id` já usado no mapa) é contada como "já existente" e não é relida.
5. **Caminhos maliciosos** (`../`, caminho absoluto, `C:\`, link simbólico, caractere nulo) não
   são lidos e viram ocorrência. Arquivos que não são foto (vídeo, .exe…) também.
6. Quando termina, a versão dos dados muda e o relatório aberto se atualiza sozinho.
7. **Empresa executora (obrigatória):** informada no envio e associada a todas as fotos do ZIP.
   Aparece no painel de detalhes do ponto, no relatório e no CSV. Dá para corrigir depois, na
   tabela de envios ("editar").

## Leitura do carimbo e revisão antes de entrar no mapa

* **Ordem de leitura de cada foto:** (1) rodovia, lote e estaca/km escritos no carimbo
  (`Rodovia: BR-373/PR · Lote 04`, `Localização: 203+100 — Decrescente | Faixa 1`, `Nota: …`);
  (2) coordenada escrita no carimbo; (3) EXIF, nome do arquivo e data. O carimbo é lido sempre que
  o Tesseract está instalado; se a coordenada do carimbo diferir da do EXIF, as duas ficam guardadas
  e a foto vai para revisão.
* **Segmentos elegíveis:** BR-277 lotes 02 e 03 e BR-373 lote 04 (`mapa/classificacao.py`).
* **Classificação automática** só quando os dados conferem: a estaca do carimbo existe no eixo do
  lote do carimbo e/ou a coordenada cai nesse lote, a até o limite do eixo (Regras). A foto fica
  *aguardando confirmação* e **só entra no mapa quando o administrador confirma** (por foto ou o
  envio inteiro, no botão "Confirmar N no mapa").
* **Pendente** quando falta dado, quando há conflito (estaca do carimbo x coordenada, lote do
  carimbo x lote da coordenada, rodovia x lote, carimbo x EXIF, coordenada longe do eixo), quando o
  segmento não é elegível, quando o eixo do lote não está carregado ou quando é provável reenvio
  (mesma data/hora e posição de foto existente). A foto fica fora do mapa até o administrador
  corrigir rodovia, lote, estaca ou coordenada e confirmar. Uma sugestão (estaca do carimbo ou da
  coordenada) pode ser mostrada, mas nunca é aplicada sozinha, e nunca é de outro lote.
* **Fotos que já estavam no mapa:** ao iniciar o sistema (e no botão "Reanalisar fotos do mapa") o
  carimbo guardado é comparado com o sistema. Divergências aparecem em *Divergências no mapa*; a foto
  **continua no mapa** até o administrador escolher "manter como está" ou corrigir.
* **Nada é sobrescrito:** a linha original da foto não muda; a classificação fica em `revisao_foto`
  e cada decisão (antes e depois) na auditoria, visível no histórico da foto.
* **Permissões no servidor:** todas as rotas de envio, revisão, confirmação, decisão e edição
  exigem administrador (403 para visualizador), além de ocultar os botões na tela.

## Regras existentes preservadas

| Regra | Onde estava | Onde está agora |
|---|---|---|
| Leitura da foto: EXIF (DateTimeOriginal + GPS) → carimbo por OCR (3 formatos) → nome do arquivo; nunca a data do arquivo | `atualizar_dados.py` | o **mesmo arquivo, sem alteração**, chamado pelo servidor |
| Travas de data posterior ao envio do WhatsApp e de ponto isolado a +15 km (releitura) | `atualizar_dados.py` | idem |
| HEIC/PNG convertidos para JPEG mantendo o EXIF | conversão manual feita no lote 29/10 | automática no envio |
| Duplicata: mesmo carimbo (data/hora) e mesma posição (30 m com segundos, 2 m sem) | `index.html` | `mapa/regras.py` |
| Estaca e bloco pelo KMZ de estacas; quebra de linha a +250 m | `index.html` | `mapa/regras.py`, `mapa/eixo.py` |
| Inconsistência: > 150 m do eixo, ou coordenada idêntica com ≥ 5 min de diferença (GPS travado); sem eixo, > 5 km de qualquer foto | `index.html` | `mapa/regras.py` (limite e GPS travado ajustáveis no painel) |
| Mesmo buraco: mesmo dia, ≤ 25 m do centro do grupo; antes e depois contam uma vez | `index.html` | `mapa/regras.py` |
| Buraco tapado: o grupo tem foto "depois" (tapado) — ou foto sem classificação | `index.html` | `mapa/regras.py` (ajustável no painel) |
| Fotos "não relacionadas" (chuva, captura de tela, sem buraco) fora do relatório | classificação manual 29/10 | idem + classificação pelo painel |

Conferência: com os dados de 22/10 e 29/10 importados, o servidor dá os mesmos números da página
anterior — 461 fotos, **142 buracos tapados**, 33 inconsistências, 64 sem coordenada, 12 não
relacionadas, 19 buracos sem foto de tapado.

## Pontos que dependem de confirmação

1. **Classificação antes / tapado / não relacionada não é automática.** No lote 29/10 ela foi feita
   olhando foto por foto; não existe regra no projeto que reconheça isso pela imagem. Fotos novas
   entram **não classificadas** e, pela regra que já valia no lote 22/10, **contam como buraco
   tapado** (aparecem para o visualizador). Para esconder do visualizador até alguém classificar,
   desmarque "Fotos ainda não classificadas contam como buraco tapado" em **Regras**.
   → Confirmar qual comportamento deve valer.
2. **"Sem buraco tampado"**: entendi como o buraco que só tem foto de *antes* (sem foto do serviço
   feito). Fotos "não relacionadas" (chuva etc.) são outra categoria. Ambas ficam restritas ao
   administrador. → Confirmar se é isso.
3. **"Fotos de buracos no mesmo local"**: entendi como as fotos extras do mesmo buraco (antes,
   outras fotos do tapado) — o visualizador vê uma foto por buraco, a do serviço feito. O critério
   de "mesmo local" é o já existente (mesmo dia, ≤ 25 m). → Confirmar se buracos em dias
   diferentes no mesmo ponto também devem ser tratados como repetição (hoje não são).
4. **Limite de 150 m** para inconsistência e a janela de 5 min para GPS travado são os valores atuais;
   podem ser ajustados no painel.
5. **Bloco 4 (BR-373)**: falta o KMZ de estacas. Até ele ser carregado, fotos da BR-373 aparecem
   como inconsistência (longe do eixo dos Blocos 2 e 3).

## Testes

```bash
python -m pytest
```
31 testes: nada acessível sem login (páginas, API, fotos e até o JavaScript), bloqueio após 5
senhas erradas, CSRF obrigatório, visualizador barrado no servidor em todas as rotas de
administrador, fluxo completo de envio em segundo plano com fotos reais (data e coordenada do
carimbo, estaca 182+691, foto sem GPS virando ocorrência, foto em tamanho real), ZIP repetido
e foto repetida, ZIPs inválidos e maliciosos, e um caso de cada categoria restrita conferindo que
nada aparece para o visualizador no relatório, no CSV com filtros ou nas fotos; filtro de vários
blocos; datas impossíveis; classificação; verificação de saúde sem dados; admin inicial por
variável de ambiente; backup e restauração (com backup adulterado recusado e dados idênticos depois).

## Estrutura

```
atualizar_dados.py   leitura das fotos (original, sem alteração)
gerenciar.py         contas e importação pela linha de comando
executar.py          servidor (waitress), escuta em 127.0.0.1
mapa/
  regras.py          duplicata, estaca, inconsistência ("anomalia" no código), buraco, tapado
  visao.py           o que cada papel recebe (relatório e CSV)
  processamento.py   validação do ZIP, fila e processamento em segundo plano
  seguranca.py       login, papéis, CSRF, limite de tentativas, cabeçalhos
  eixo.py            KMZ/KML de estacas (XML lido com defusedxml)
  backup.py          backup completo e restauração numa instalação vazia
  rotas.py           páginas e API
  templates/, static/  relatório (mesmo formato do index.html), login, painel, conta
testes/              testes automatizados e amostras
Dockerfile           imagem da hospedagem (com Tesseract); roda os testes na construção
deploy/oracle/       servidor Oracle: instalação (cloud-init), Docker Compose, Caddy (HTTPS), atualização
.github/workflows/   CI: testes + construção da imagem a cada envio ao GitHub
```
