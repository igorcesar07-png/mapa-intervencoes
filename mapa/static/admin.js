"use strict";
/* Painel do administrador: envio de ZIP, acompanhamento, ocorrências, classificação, regras, eixo e usuários. */
const $ = s => document.querySelector(s);
const CSRF = () => document.querySelector('meta[name="csrf"]').content;
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const dataBR = iso => iso ? `${iso.slice(8,10)}/${iso.slice(5,7)}/${iso.slice(0,4)} ${iso.slice(11,16)}` : "—";
const ESTADO = {aguardando:"Aguardando", em_andamento:"Em andamento", concluido:"Concluído", erro:"Com erro"};
const mini = id => `/api/fotos/${encodeURIComponent(id)}/miniatura`, grande = id => `/api/fotos/${encodeURIComponent(id)}/imagem`;

async function api(url, opts={}){
  const o = {credentials:"same-origin", cache:"no-store", ...opts, headers:{"X-CSRF-Token":CSRF(), ...(opts.headers||{})}};
  if (o.json !== undefined){ o.body = JSON.stringify(o.json); o.headers["Content-Type"] = "application/json"; delete o.json; }
  const r = await fetch(url, o);
  if (r.status === 401){ location.href = "/login?proximo=/admin"; throw new Error("sessão expirada"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.erro || `Erro ${r.status}`);
  return j;
}
function msg(el, texto, tipo){ el.textContent = texto; el.className = "msg " + (tipo || ""); }
document.addEventListener("click", e => {
  const img = e.target.closest("img[data-full]");
  if (img){ $("#lb img").src = img.dataset.full; $("#lb").classList.add("on"); return; }
  if (e.target.closest("#lb")) $("#lb").classList.remove("on");
});

/* ============================================================ envio de ZIP */
$("#fZip").addEventListener("submit", e => {
  e.preventDefault();
  const f = $("#zip").files[0], m = $("#upMsg");
  if (!f) return;
  if (!/\.zip$/i.test(f.name)) return msg(m, "Escolha um arquivo .zip.", "erro");
  const fd = new FormData(); fd.append("arquivo", f);
  const x = new XMLHttpRequest();
  x.open("POST", "/api/admin/envios");
  x.setRequestHeader("X-CSRF-Token", CSRF());
  $("#upBarra").hidden = false; $("#btnZip").disabled = true;
  x.upload.onprogress = ev => { if (ev.lengthComputable){ $("#upBarra i").style.width = (100*ev.loaded/ev.total) + "%"; msg(m, `Enviando… ${Math.round(100*ev.loaded/ev.total)}%`); } };
  x.onload = () => {
    $("#btnZip").disabled = false; $("#upBarra").hidden = true;
    let j = {}; try { j = JSON.parse(x.responseText); } catch(_){}
    if (x.status === 202){ msg(m, `ZIP recebido (envio #${j.envio_id}). O processamento começou em segundo plano — acompanhe abaixo.`, "ok"); $("#fZip").reset(); }
    else if (x.status === 401) location.href = "/login?proximo=/admin";
    else msg(m, j.erro || `Falha no envio (erro ${x.status}).`, "erro");
    carregarEnvios();
  };
  x.onerror = () => { $("#btnZip").disabled = false; $("#upBarra").hidden = true; msg(m, "A conexão caiu durante o envio. Tente de novo.", "erro"); };
  x.send(fd);
});

let timerEnvios = null, haviaAtivos = false;
async function carregarEnvios(){
  clearTimeout(timerEnvios);
  let ativos = false;
  try{
    const {envios} = await api("/api/admin/envios");
    ativos = envios.some(e => e.estado === "aguardando" || e.estado === "em_andamento");
    $("#tEnvios").innerHTML = envios.length ? envios.map(e => {
      const pct = e.total ? Math.round(100*e.processadas/e.total) : (e.estado === "concluido" ? 100 : 0);
      const res = e.estado === "concluido" ? `${e.novas} nova(s), ${e.repetidas} repetida(s), ${e.ignoradas} ignorada(s)` : "";
      return `<tr>
        <td class="num">${e.id}</td>
        <td>${esc(e.nome_original)}<small>lote: ${esc(e.lote)}${e.origem === "legado" ? " · importado do mapa anterior" : ""}</small></td>
        <td><span class="estado ${e.estado}">${ESTADO[e.estado]}</span></td>
        <td class="num">${e.total ? `${e.processadas}/${e.total}` : "—"}<div class="barra"><i style="width:${pct}%"></i></div></td>
        <td>${esc(e.mensagem || "")}${res && e.mensagem && !e.mensagem.includes("nova") ? `<small>${res}</small>` : ""}</td>
        <td class="num">${dataBR(e.criado_em)}<small>${esc(e.enviado_por_email || "")}</small></td></tr>`;
    }).join("") : `<tr><td colspan="6" class="hint">Nenhum envio ainda.</td></tr>`;
    if (haviaAtivos && !ativos){ carregarOcorrencias(); if (!salvando && !document.querySelector('.cls-estado.pendente')) carregarFotos(); }
    haviaAtivos = ativos;
  }catch(e){ $("#tEnvios").innerHTML = `<tr><td colspan="6" class="msg erro">${esc(e.message)}</td></tr>`; }
  timerEnvios = setTimeout(carregarEnvios, ativos ? 2000 : 10000);
}

/* ============================================================ ocorrências */
async function carregarOcorrencias(){
  const res = $("#ocResolvidas").checked;
  try{
    const {ocorrencias, abertas} = await api("/api/admin/ocorrencias" + (res ? "?resolvidas=1" : ""));
    $("#nOc").textContent = abertas || "";
    $("#lOc").innerHTML = ocorrencias.length ? ocorrencias.map(o => `
      <div class="oc">${o.foto_id ? `<img src="${mini(o.foto_id)}" data-full="${grande(o.foto_id)}" alt="" loading="lazy">` : ""}
        <div><b><span class="tipo">${esc(o.tipo.replace(/_/g," "))}</span>${esc(o.mensagem)}</b>
        <span>${esc(o.arquivo || o.membro || "")}${o.lote ? " · lote " + esc(o.lote) : ""}${o.data ? " · " + dataBR(o.data) : ""}</span></div>
        ${res ? "" : `<button class="btn mini" data-oc="${o.id}" type="button">Revisada</button>`}</div>`).join("")
      : `<p class="hint">Nenhuma ocorrência ${res ? "revisada" : "aberta"}.</p>`;
    $("#lOc").querySelectorAll("[data-oc]").forEach(b => b.onclick = async () => {
      b.disabled = true;
      try { await api(`/api/admin/ocorrencias/${b.dataset.oc}/resolver`, {method:"POST"}); carregarOcorrencias(); }
      catch(e){ b.disabled = false; alert(e.message); }
    });
  }catch(e){ $("#lOc").innerHTML = `<p class="msg erro">${esc(e.message)}</p>`; }
}
$("#ocResolvidas").onchange = carregarOcorrencias;

/* ============================================================ classificação */
/* Cada clique grava na hora no servidor. A tela só diz "salva" depois que o servidor
   confirma o que ficou gravado; em caso de falha, volta ao estado anterior e avisa. */
let inicio = 0, saidas = 0, salvando = 0, filtroAtual = "nao_classificada";
const ROT = {antes:"Antes", depois:"Tapado", outro:"Não relacionada"};
function estadoCard(card, texto, tipo){ const el = card.querySelector(".cls-estado"); el.textContent = texto; el.className = "cls-estado " + (tipo || ""); }
function marcarBotoes(card, classe){ card.querySelectorAll("[data-c]").forEach(x => x.classList.toggle("on", x.dataset.c === classe)); }
async function salvarClasse(card, classe, motivo){
  const botoes = card.querySelectorAll("button");
  botoes.forEach(b => b.disabled = true);
  estadoCard(card, "Salvando…", "salvando");
  salvando++;
  try{
    const j = await api(`/api/admin/fotos/${card.dataset.id}/classe`, {method:"POST", json:{classe, motivo}});
    if (j.classe !== classe) throw new Error("o servidor não confirmou a classificação");
    const antes = card.dataset.classe || "";
    card.dataset.classe = j.classe;
    marcarBotoes(card, j.classe);
    card.querySelector(".outro-form").hidden = true;
    estadoCard(card, `Classificação salva: ${ROT[j.classe]}${j.motivo_exclusao ? ` (${j.motivo_exclusao})` : ""}`, "ok");
    // fotos que saem do filtro atual ajustam a paginação, para "Próximas" não pular nenhuma
    const saiuDoFiltro = filtroAtual === "nao_classificada" ? !antes : (filtroAtual !== "todas" && j.classe !== filtroAtual);
    if (saiuDoFiltro && !card.dataset.saiu){ card.dataset.saiu = "1"; saidas++; }
  }catch(e){
    marcarBotoes(card, card.dataset.classe || "");
    estadoCard(card, "Não foi possível salvar. Tente novamente." + (e.message ? ` (${e.message})` : ""), "erro");
  }finally{
    salvando--;
    botoes.forEach(b => b.disabled = false);
  }
}
async function carregarFotos(){
  try{
    filtroAtual = $("#cClasse").value;
    const q = new URLSearchParams({classe:filtroAtual, lote:$("#cLote").value, inicio});
    const j = await api("/api/admin/fotos?" + q);
    saidas = 0;
    const sl = $("#cLote"), v = sl.value;
    sl.innerHTML = '<option value="">Todos</option>' + j.lotes.map(l => `<option>${esc(l)}</option>`).join(""); sl.value = j.lotes.includes(v) ? v : "";
    const fim = Math.min(inicio + j.fotos.length, j.total);
    $("#cTotal").textContent = j.total ? `${j.total} foto(s) · mostrando ${inicio + 1}–${fim}` : "0 foto(s)";
    $("#cAnt").disabled = inicio === 0; $("#cProx").disabled = fim >= j.total;
    $("#gFotos").innerHTML = j.fotos.length ? j.fotos.map(f => `
      <div class="foto" data-id="${f.id}" data-classe="${f.classe || ""}">
        <img src="${mini(f.id)}" data-full="${grande(f.id)}" alt="" loading="lazy">
        <div class="info"><span class="dt">${dataBR(f.data)}</span>${f.data_fonte === "manual" ? ` <em class="dt-man" title="Corrigida no painel; antes: ${esc(dataBR(f.data_original))}">corrigida</em>` : ""}${f.lat == null ? " · sem coordenada" : ""}
          <button type="button" class="lnk" data-edit-data>Corrigir data</button><br>${esc(f.lote)}
          ${f.sugestao_data ? `<span class="sug">Envio pelo WhatsApp em ${dataBR(f.sugestao_data).slice(0,10)}: confira o dia.</span>` : ""}</div>
        <div class="data-form" hidden>
          <input type="datetime-local" step="1" value="${(f.sugestao_data || f.data || "").slice(0,19)}" aria-label="Data e hora corretas">
          <button type="button" class="btn pri mini" data-salvar-data>Salvar data</button>
        </div>
        <div class="acoes">${["antes","depois","outro"].map(c => `<button type="button" class="btn ${f.classe===c?"on":""}" data-c="${c}">${ROT[c]}</button>`).join("")}</div>
        <div class="outro-form" hidden>
          <input type="text" placeholder="Motivo (ex.: chuva, sem buraco)" maxlength="200">
          <button type="button" class="btn pri mini" data-salvar-outro>Salvar como não relacionada</button>
        </div>
        <p class="cls-estado ${f.classe ? "ok" : ""}">${f.classe ? `Classificação salva: ${ROT[f.classe]}${f.motivo_exclusao ? ` (${esc(f.motivo_exclusao)})` : ""}` : "Ainda não classificada"}</p>
      </div>`).join("") : `<p class="hint">Nenhuma foto nessa seleção.</p>`;
    $("#gFotos").querySelectorAll(".foto").forEach(card => {
      const form = card.querySelector(".outro-form"), inp = form.querySelector("input");
      card.querySelectorAll("[data-c]").forEach(b => b.onclick = () => {
        if (b.dataset.c === "outro"){
          // pendente até clicar em "Salvar como não relacionada"
          form.hidden = false; inp.focus();
          estadoCard(card, "Alteração pendente: informe o motivo (opcional) e clique em “Salvar como não relacionada”.", "pendente");
          return;
        }
        form.hidden = true;
        salvarClasse(card, b.dataset.c, "");
      });
      form.querySelector("[data-salvar-outro]").onclick = () => salvarClasse(card, "outro", inp.value);
      const fd = card.querySelector(".data-form"), inpD = fd.querySelector("input");
      card.querySelector("[data-edit-data]").onclick = () => { fd.hidden = !fd.hidden; if (!fd.hidden) inpD.focus(); };
      fd.querySelector("[data-salvar-data]").onclick = async () => {
        if (!inpD.value){ estadoCard(card, "Informe a data e a hora.", "erro"); return; }
        estadoCard(card, "Salvando a data…", "salvando");
        try{
          const j = await api(`/api/admin/fotos/${card.dataset.id}/data`, {method:"POST", json:{data:inpD.value}});
          card.querySelector(".dt").textContent = dataBR(j.data);
          const s = card.querySelector(".sug"); if (s) s.remove();
          fd.hidden = true;
          estadoCard(card, `Data salva: ${dataBR(j.data)} (antes: ${dataBR(j.data_original)}). O mapa e os gráficos já usam a nova data.`, "ok");
        }catch(e){ estadoCard(card, "Não foi possível salvar a data. " + (e.message || ""), "erro"); }
      };
      inp.addEventListener("keydown", e => { if (e.key === "Enter") salvarClasse(card, "outro", inp.value); });
    });
  }catch(e){ $("#gFotos").innerHTML = `<p class="msg erro">${esc(e.message)}</p>`; }
}
$("#cClasse").onchange = () => { inicio = 0; carregarFotos(); };
$("#cLote").onchange = () => { inicio = 0; carregarFotos(); };
$("#cAnt").onclick = () => { inicio = Math.max(0, inicio - 60); carregarFotos(); };
$("#cProx").onclick = () => { inicio = Math.max(0, inicio + 60 - saidas); carregarFotos(); };
addEventListener("beforeunload", e => { if (salvando > 0 || document.querySelector(".cls-estado.pendente")){ e.preventDefault(); e.returnValue = ""; } });

/* ============================================================ regras */
async function carregarRegras(){
  const p = await api("/api/admin/parametros");
  $("#pLim").value = p.limite_anomalia_m; $("#pTrav").checked = p.gps_travado_ativo === "1"; $("#pNc").checked = p.nao_classificadas_contam === "1";
}
$("#fRegras").addEventListener("submit", async e => {
  e.preventDefault();
  try{
    await api("/api/admin/parametros", {method:"POST", json:{limite_anomalia_m:+$("#pLim").value, gps_travado_ativo:$("#pTrav").checked ? 1 : 0, nao_classificadas_contam:$("#pNc").checked ? 1 : 0}});
    msg($("#pMsg"), "Regras salvas. O relatório já usa os novos valores.", "ok");
  }catch(err){ msg($("#pMsg"), err.message, "erro"); }
});

/* ============================================================ eixo */
const fmtEst = m => { m = Math.round(m/10)*10; return `${Math.floor(m/1000)}+${String(m%1000).padStart(3,"0")}`; };
async function carregarEixo(){
  const {blocos} = await api("/api/admin/eixo");
  $("#lEixo").innerHTML = blocos.length ? blocos.map(b => `<div class="bloco"><b>Bloco ${esc(b.bloco)}</b>${b.rodovia ? " · " + esc(b.rodovia) : ""}
      <span>${b.inicio != null ? `km ${fmtEst(b.inicio)} a ${fmtEst(b.fim)}` : `${b.pontos} pontos sem estaca`} · ${esc(b.fonte)}</span>
      <button class="btn mini perigo" type="button" data-b="${esc(b.bloco)}">Remover</button></div>`).join("")
    : `<p class="hint">Nenhum eixo carregado.</p>`;
  $("#lEixo").querySelectorAll("[data-b]").forEach(bt => bt.onclick = async () => {
    if (bt.dataset.ok !== "1"){ bt.dataset.ok = "1"; bt.textContent = "Confirmar remoção"; return; }
    try { await api(`/api/admin/eixo/${encodeURIComponent(bt.dataset.b)}`, {method:"DELETE"}); carregarEixo(); }
    catch(e){ msg($("#kMsg"), e.message, "erro"); }
  });
}
$("#fKmz").addEventListener("submit", async e => {
  e.preventDefault();
  const f = $("#kmz").files[0]; if (!f) return;
  const fd = new FormData(); fd.append("arquivo", f);
  try{
    const j = await api("/api/admin/eixo", {method:"POST", body:fd});
    msg($("#kMsg"), `Eixo carregado: ${j.blocos.map(b => `Bloco ${b.bloco} (${b.pontos} pontos)`).join(", ")}.`, "ok");
    $("#fKmz").reset(); carregarEixo();
  }catch(err){ msg($("#kMsg"), err.message, "erro"); }
});

/* ============================================================ usuários */
async function carregarUsuarios(){
  const {usuarios, eu} = await api("/api/admin/usuarios");
  $("#tUsu").innerHTML = usuarios.map(u => `<tr data-id="${u.id}">
    <td>${esc(u.email)}</td><td>${esc(u.nome)}</td>
    <td>${u.papel === "admin" ? "Administrador" : "Visualizador"}</td>
    <td>${u.ativo ? "Ativo" : "<b>Desativado</b>"}</td>
    <td class="num">${dataBR(u.ultimo_login)}</td>
    <td>${u.id === eu ? '<span class="hint">você</span>' : `
      <button class="btn mini" type="button" data-a="papel">${u.papel === "admin" ? "Tornar visualizador" : "Tornar administrador"}</button>
      <button class="btn mini" type="button" data-a="ativo">${u.ativo ? "Desativar" : "Reativar"}</button>
      <button class="btn mini" type="button" data-a="senha">Nova senha</button>`}</td></tr>`).join("");
  $("#tUsu").querySelectorAll("[data-a]").forEach(b => b.onclick = async () => {
    const tr = b.closest("tr"), u = usuarios.find(x => x.id === +tr.dataset.id);
    const corpo = b.dataset.a === "papel" ? {papel: u.papel === "admin" ? "visualizador" : "admin"}
               : b.dataset.a === "ativo" ? {ativo: !u.ativo} : {redefinir_senha: true};
    try{
      const j = await api(`/api/admin/usuarios/${u.id}`, {method:"POST", json:corpo});
      if (j.senha_temporaria) $("#uMsg").innerHTML = `Senha temporária de ${esc(u.email)}: <span class="senha">${esc(j.senha_temporaria)}</span> — entregue à pessoa; ela pode trocar em “Conta”. Esta senha não aparece de novo.`;
      carregarUsuarios();
    }catch(e){ msg($("#uMsg"), e.message, "erro"); }
  });
}
$("#fUsu").addEventListener("submit", async e => {
  e.preventDefault();
  try{
    const j = await api("/api/admin/usuarios", {method:"POST", json:{email:$("#uEmail").value, nome:$("#uNome").value, papel:$("#uPapel").value}});
    $("#uMsg").innerHTML = `Usuário criado. Senha temporária: <span class="senha">${esc(j.senha_temporaria)}</span> — entregue à pessoa; ela pode trocar em “Conta”. Esta senha não aparece de novo.`;
    $("#uMsg").className = "msg ok"; $("#fUsu").reset(); carregarUsuarios();
  }catch(err){ msg($("#uMsg"), err.message, "erro"); }
});

/* ============================================================ backup */
$("#btnBackup").addEventListener("click", () => { $("#bkHint").textContent = "Gerando o arquivo… o download começa em instantes."; setTimeout(() => $("#bkHint").textContent = "", 15000); });
async function carregarRestaurar(){
  try { const {permitido} = await api("/api/admin/restaurar"); $("#restaurar").hidden = !permitido; } catch(_){}
}
$("#fRest").addEventListener("submit", e => {
  e.preventDefault();
  const f = $("#rArq").files[0], m = $("#rMsg");
  if (!f) return;
  const fd = new FormData(); fd.append("arquivo", f);
  const x = new XMLHttpRequest();
  x.open("POST", "/api/admin/restaurar"); x.setRequestHeader("X-CSRF-Token", CSRF());
  $("#rBarra").hidden = false; $("#btnRest").disabled = true;
  x.upload.onprogress = ev => { if (ev.lengthComputable){ const p = Math.round(100*ev.loaded/ev.total); $("#rBarra i").style.width = p + "%"; msg(m, p < 100 ? `Enviando… ${p}%` : "Conferindo e restaurando… não feche a página."); } };
  x.onload = () => {
    $("#btnRest").disabled = false; $("#rBarra").hidden = true;
    let j = {}; try { j = JSON.parse(x.responseText); } catch(_){}
    if (x.status === 200){ msg(m, `${j.mensagem} (${j.contagens.fotos} fotos, ${j.contagens.envios} envios, ${j.contagens.usuarios} usuários)`, "ok"); setTimeout(() => location.href = "/login", 4000); }
    else msg(m, j.erro || `Falha (erro ${x.status}).`, "erro");
  };
  x.onerror = () => { $("#btnRest").disabled = false; $("#rBarra").hidden = true; msg(m, "A conexão caiu durante o envio. Tente de novo.", "erro"); };
  x.send(fd);
});

carregarRestaurar(); carregarEnvios(); carregarOcorrencias(); carregarFotos(); carregarRegras(); carregarEixo(); carregarUsuarios();
