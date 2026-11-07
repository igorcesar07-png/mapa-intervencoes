"use strict";
/* Relatório do Mapa de Intervenções (versão web).
   As regras (duplicatas, estaca, inconsistências (internamente "anomalia"), agrupamento por buraco) rodam no servidor;
   esta página só desenha o que o servidor entregou para o papel do usuário. */

const PAPEL = document.body.dataset.papel;
const ADMIN = PAPEL === "admin";
const CSRF = document.querySelector('meta[name="csrf"]').content;
const MES_NOME = ["","jan","fev","mar","abr","mai","jun","jul","ago","set","out","nov","dez"];
const COR_BLOCO = ["#ff7a1a", "#22b8cf", "#b197fc", "#94d82d", "#f783ac"];

/* ============================================================ utilidades */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtData = iso => iso ? `${iso.slice(8,10)}/${iso.slice(5,7)}/${iso.slice(0,4)} ${iso.slice(11,16)}` : "—";
const fmtNum = (n, d=0) => n == null ? "—" : n.toLocaleString("pt-BR", {minimumFractionDigits:d, maximumFractionDigits:d});
const fmtEst = m => { m = Math.round(m/10)*10; return `${Math.floor(m/1000)}+${String(m%1000).padStart(3,"0")}`; };
const mini = id => `/api/fotos/${encodeURIComponent(id)}/miniatura`;
const grande = id => `/api/fotos/${encodeURIComponent(id)}/imagem`;
function toast(msg, ms=3200){ const t=$("#toast"); t.textContent=msg; t.classList.add("on"); clearTimeout(t._h); t._h=setTimeout(()=>t.classList.remove("on"), ms); }
// camadas: só o administrador tem o painel; o visualizador usa a configuração padrão
// padrão definido pelo administrador em 07/11/2026 (o mesmo do painel do administrador ao abrir)
const PADRAO_CAMADAS = {"#lSeg":true, "#oFundo":true, "#lCalor":true, "#lPontos":true, "#lAnom":false, "#oSoAntes":true, "#lTracado":false, "#lKm":true, "#rRaio":26, "#rMax":1};
const marcado = sel => { const el = $(sel); return el ? el.checked : !!PADRAO_CAMADAS[sel]; };
const valor = (sel, padrao) => { const el = $(sel); return el ? +el.value : padrao; };
/* dia "AAAA-MM-DD" que existe no calendário e não está no futuro (o servidor já valida; aqui é defesa) */
function diaValido(d){
  if (!/^\d{4}-\d{2}-\d{2}$/.test(d || "")) return false;
  const [a, m, di] = d.split("-").map(Number), t = new Date(Date.UTC(a, m-1, di));
  return t.getUTCFullYear() === a && t.getUTCMonth() === m-1 && t.getUTCDate() === di && a >= 2020 && t.getTime() <= Date.now() + 86400000;
}
const diaDe = r => { const d = (r && r.data || "").slice(0,10); return diaValido(d) ? d : ""; };

/* ============================================================ estado */
const S = { blocosSel: new Set(), todos: [], porId: new Map(), derivado: new Map(), grupos: [], tracado: {blocos:{}}, filtroDia: "", versao: null, params: {}, temEstacas: false, faixas: new Map(), det: null };

async function carregar(enquadrar){
  const r = await fetch("/api/relatorio", {credentials:"same-origin", cache:"no-store"});
  if (r.status === 401){ location.href = "/login"; return; }
  if (!r.ok){ toast("Não foi possível carregar os dados. Recarregue a página."); return; }
  const j = await r.json();
  if (window.aplicarAjustes) await window.aplicarAjustes(j);   // ajustes guardados fora (cópia do GitHub Pages)
  S.versao = j.versao; S.params = j.parametros || {}; S.temEstacas = j.tem_estacas;
  S.tracado = {blocos: j.eixo || {}}; _linhas = null; _eixoDesenhado = null;
  S.todos = j.registros.sort((a,b) => (a.data||"9999").localeCompare(b.data||"9999") || a.arquivo.localeCompare(b.arquivo));
  S.porId = new Map(S.todos.map(r => [r.id, r]));
  S.derivado = new Map(S.todos.map(r => { const d = r.d || {}; if (d.est != null) d.x = d.est/1000; r._dup = d.dup || null; return [r.id, d]; }));
  S.grupos = j.grupos;
  statusEixo(); montarFiltros(); desenhar();
  if (enquadrar) enquadrarMapa();
}

/* ============================================================ mapa */
const mapa = L.map("map", {zoomControl:false, preferCanvas:true}).setView([-25.43, -50.3], 10);
L.control.zoom({position:"topright"}).addTo(mapa);
/* Camadas de fundo
   - Ruas: tiles oficiais do OpenStreetMap. A política de uso exige crédito visível e o
     cabeçalho Referer (origem do site); por isso cada <img> de tile envia a origem
     (strict-origin-when-cross-origin), nunca o caminho da página. Uso leve, sem pré-carga extra.
   - Ruas (escuro): os MESMOS tiles do OSM, escurecidos no navegador por CSS (nenhum pedido a mais).
   - Satélite: Esri World Imagery, com o crédito exigido pela Esri.
   A antiga camada da CARTO foi retirada: passou a exigir chave de API e o uso gratuito é só não comercial. */
const OSM_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_CREDITO = '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">Colaboradores do OpenStreetMap</a>';
const opcoesOsm = {maxZoom:19, attribution:OSM_CREDITO, referrerPolicy:"strict-origin-when-cross-origin", keepBuffer:1};
const bases = {
  "Ruas (escuro)": L.tileLayer(OSM_URL, {...opcoesOsm, className:"tiles-escuro"}),
  "Ruas": L.tileLayer(OSM_URL, opcoesOsm),
  "Satélite": L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
    maxZoom:19, maxNativeZoom:18, referrerPolicy:"strict-origin-when-cross-origin", keepBuffer:1,
    attribution:'Imagens &copy; <a href="https://www.esri.com/" target="_blank" rel="noopener">Esri</a> — Esri, Maxar, Earthstar Geographics e comunidade GIS'}),
};
// lembra a camada escolhida (só neste navegador)
let baseInicial = "Ruas (escuro)";
try { const b = localStorage.getItem("mapa_base"); if (b && bases[b]) baseInicial = b; } catch(e){}
bases[baseInicial].addTo(mapa);
mapa.on("baselayerchange", e => { try { localStorage.setItem("mapa_base", e.name); } catch(_){} });
L.control.layers(bases, null, {position:"topright"}).addTo(mapa);
L.control.scale({imperial:false, position:"bottomright"}).addTo(mapa);
const render = L.canvas({padding:.3});
const gTracado = L.layerGroup().addTo(mapa), gKm = L.layerGroup().addTo(mapa), gSeg = L.layerGroup().addTo(mapa);
const gPontos = L.layerGroup().addTo(mapa), gAnom = L.layerGroup().addTo(mapa);
let calor = null;
const marcadores = new Map();

/* ============================================================ eixo */
function ordBloco(a, b){ return (parseFloat(a)||999) - (parseFloat(b)||999) || a.localeCompare(b); }
const corBloco = id => COR_BLOCO[Object.keys(S.tracado.blocos).sort(ordBloco).indexOf(id) % COR_BLOCO.length];
function distM(a, b){ const k=111320, dy=(a[0]-b[0])*k, dx=(a[1]-b[1])*k*Math.cos(a[0]*Math.PI/180); return Math.hypot(dx, dy); }
let _linhas = null;
function linhasTracado(){
  if (_linhas) return _linhas;
  const out = [];
  for (const [id, b] of Object.entries(S.tracado.blocos)){
    let cur = [];
    for (const p of b.pts){
      if (cur.length && distM(cur[cur.length-1], p) > 250){ if (cur.length > 1) out.push({bloco:id, pts:cur}); cur = []; }
      cur.push(p);
    }
    if (cur.length > 1) out.push({bloco:id, pts:cur});
  }
  return _linhas = out;
}
function statusEixo(){
  const ids = Object.keys(S.tracado.blocos).sort(ordBloco);
  $("#blocosList").innerHTML = ids.length ? ids.map(id => {
    const b = S.tracado.blocos[id], e = b.pts.map(p => p[2]).filter(v => v != null);
    const faixa = e.length ? `km ${fmtEst(Math.min(...e))} a ${fmtEst(Math.max(...e))}` : `${b.pts.length} pontos (sem estaca)`;
    return `<div class="bloco"><i style="background:${corBloco(id)}"></i><b>Bloco ${esc(id)}</b>${b.rodovia ? " · " + esc(b.rodovia) : ""}<span>${faixa}</span></div>`;
  }).join("") : `<p class="hint" style="margin:0">Nenhum eixo carregado.</p>`;
  if (ADMIN){
    const lim = S.params.limite_anomalia_m, trav = S.params.gps_travado_ativo === "1";
    $("#anomRegra").innerHTML = ids.length
      ? `Inconsistência: foto a mais de <b>${esc(lim)} m</b> do eixo${trav ? `, ou com <b>coordenada idêntica</b> a outra foto tirada ${esc(S.params.gps_travado_min)}+ min antes/depois (o app repetiu a última posição do GPS)` : ""}. Ajuste no painel do administrador.`
      : `Sem eixo carregado: inconsistência é a foto a mais de <b>${fmtNum(S.params.isolado_m/1000)} km</b> de qualquer outra.`;
  }
}
let _eixoDesenhado = null;
function desenharEixo(){
  const chave = marcado("#lTracado") + "|" + marcado("#lKm") + "|" + Object.keys(S.tracado.blocos).join(",") + "|" + S.versao;
  if (_eixoDesenhado === chave) return; _eixoDesenhado = chave;
  gTracado.clearLayers(); gKm.clearLayers();
  if (marcado("#lTracado")) for (const l of linhasTracado()){
    const b = S.tracado.blocos[l.bloco];
    L.polyline(l.pts.map(p => [p[0], p[1]]), {color:corBloco(l.bloco), weight:2, opacity:.55, renderer:render, interactive:false})
      .addTo(gTracado);
  }
  if (marcado("#lKm")) for (const b of Object.values(S.tracado.blocos)){
   const feitos = new Map();   // o KMZ repete a mesma estaca (ex.: 166+000) a ~20 m: um rótulo por km
   for (const p of b.pts){
    if (p[2] == null || p[2] % 1000 !== 0) continue;
    const ant = feitos.get(p[2]);
    if (ant && Math.abs(ant[0] - p[0]) < .002 && Math.abs(ant[1] - p[1]) < .002) continue;
    feitos.set(p[2], p);
    const m = L.marker([p[0], p[1]], {icon:L.divIcon({className:"", html:`<span class="kmlbl">${p[2]/1000}</span>`, iconSize:[0,0]}), interactive:false, keyboard:false});
    m._km = p[2]/1000; m.addTo(gKm);
   }
  }
  marcosPorZoom();
}
function marcosPorZoom(){
  const z = mapa.getZoom(), passo = z >= 13 ? 1 : z >= 11 ? 5 : z >= 10 ? 10 : 0;
  gKm.eachLayer(m => { const vis = passo && m._km % passo === 0; if (m._icon) m._icon.style.display = vis ? "" : "none"; });
  afastarMarcos();
}
/* Rótulos de km que se encostariam (eixos que se cruzam ou ficam lado a lado) são empurrados para
   cima/baixo, ligados ao ponto por uma linha fina. Nenhum é escondido. */
function afastarMarcos(){
  const vis = [];
  gKm.eachLayer(m => {
    const el = m._icon && m._icon.firstChild;
    if (!el) return;
    el.classList.remove("kmlbl-desl"); el.style.top = ""; el.removeAttribute("data-lado");
    if (m._icon.style.display === "none") return;
    const p = mapa.latLngToLayerPoint(m.getLatLng());
    vis.push({el, x:p.x, y:p.y, w:el.offsetWidth || 24, h:el.offsetHeight || 11});
  });
  vis.sort((a, b) => a.y - b.y || a.x - b.x);
  const postos = [];
  const bate = (r) => postos.some(o => Math.abs(o.x - r.x) < (o.w + r.w) / 2 + 2 && Math.abs(o.y - r.y) < (o.h + r.h) / 2 + 1);
  for (const r of vis){
    let dy = 0;
    for (const t of [0, -1, 1, -2, 2, -3, 3]){
      dy = t * (r.h + 2);
      if (!bate({...r, y:r.y + dy})) break;
    }
    if (dy){
      r.el.classList.add("kmlbl-desl"); r.el.style.top = dy + "px";
      r.el.dataset.lado = dy < 0 ? "c" : "b";
      r.el.style.setProperty("--d", (Math.abs(dy) - r.h / 2) + "px");
    }
    postos.push({...r, y:r.y + dy});
  }
}
mapa.on("zoomend", marcosPorZoom);
gKm.on("layeradd", () => setTimeout(marcosPorZoom, 0));

/* ============================================================ filtros */
function filtros(){
  return {de:$("#fDe").value, ate:$("#fAte").value, mes:$("#fMes").value, lote:$("#fLote").value, blocos:[...S.blocosSel], dia:S.filtroDia};
}
function passa(r, f){
  if (f.lote && r.lote !== f.lote) return false;
  if (f.blocos.length && !f.blocos.includes((S.derivado.get(r.id)||{}).bloco)) return false;
  const d = diaDe(r);
  if ((f.de||f.ate||f.mes||f.dia) && !d) return false;
  if (f.de && d < f.de) return false;
  if (f.ate && d > f.ate) return false;
  if (f.mes && d.slice(0,7) !== f.mes) return false;
  if (f.dia && d !== f.dia) return false;
  return true;
}
function filtrados(){ const f = filtros(); return S.todos.filter(r => !r._dup && r.classe !== "outro" && passa(r, f)); }

/* ---------- filtro de vários blocos (caixas de seleção). Conjunto vazio = Todos. */
const nomeBloco = id => `Bloco ${id}${S.tracado.blocos[id] && S.tracado.blocos[id].rodovia ? " · " + S.tracado.blocos[id].rodovia : ""}`;
const blocosAtivos = () => S.blocosSel.size ? [...S.blocosSel] : Object.keys(S.tracado.blocos);
function montarBlocos(){
  const ids = Object.keys(S.tracado.blocos).sort(ordBloco);
  for (const id of [...S.blocosSel]) if (!ids.includes(id)) S.blocosSel.delete(id);
  const menu = $("#fBlocosMenu");
  menu.innerHTML = `<label class="chk"><input type="checkbox" data-todos ${S.blocosSel.size ? "" : "checked"}> Todos</label>`
    + ids.map(id => `<label class="chk"><input type="checkbox" value="${esc(id)}" ${S.blocosSel.has(id) ? "checked" : ""}> ${esc(nomeBloco(id))}</label>`).join("");
  menu.querySelectorAll("input").forEach(cb => cb.onchange = () => {
    if (cb.hasAttribute("data-todos")) S.blocosSel.clear();
    else if (cb.checked) S.blocosSel.add(cb.value); else S.blocosSel.delete(cb.value);
    if (S.blocosSel.size === ids.length) S.blocosSel.clear();          // todos marcados = Todos
    S.filtroDia = ""; montarFiltros(); desenhar();
  });
  const sel = [...S.blocosSel].sort(ordBloco);
  $("#fBlocosVal").innerHTML = sel.length ? sel.map(id => `<span class="multi-chip">Bloco ${esc(id)}</span>`).join("") : "Todos";
  $("#fBlocosBtn").title = sel.length ? sel.map(nomeBloco).join(", ") : "Todos os blocos";
}
(function(){
  const btn = $("#fBlocosBtn"), menu = $("#fBlocosMenu");
  const abrir = v => { menu.hidden = !v; btn.setAttribute("aria-expanded", String(v)); };
  btn.onclick = e => { e.stopPropagation(); abrir(menu.hidden); };
  document.addEventListener("click", e => { if (!e.target.closest("#fBlocos")) abrir(false); });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !menu.hidden){ abrir(false); btn.focus(); } });
})();

function montarFiltros(){
  const reg = S.todos.filter(r => !r._dup && r.classe !== "outro");
  const diasValidos = [...new Set(reg.map(diaDe).filter(Boolean))].sort();   // cada data uma vez só
  const meses = [...new Set(diasValidos.map(d => d.slice(0,7)))].sort();
  const lotes = [...new Set(reg.map(r => r.lote).filter(Boolean))].sort();
  const sm = $("#fMes"), sl = $("#fLote"), vm = sm.value, vl = sl.value;
  sm.innerHTML = '<option value="">Todos</option>' + meses.map(m => `<option value="${m}">${MES_NOME[+m.slice(5,7)]}/${m.slice(0,4)}</option>`).join("");
  sl.innerHTML = '<option value="">Todos</option>' + lotes.map(l => `<option>${esc(l)}</option>`).join("");
  sm.value = meses.includes(vm) ? vm : ""; sl.value = lotes.includes(vl) ? vl : "";
  const dias = diasValidos.slice(-14);
  const variosAnos = new Set(dias.map(d => d.slice(0,4))).size > 1;
  const rotDia = d => `${d.slice(8,10)}/${d.slice(5,7)}${variosAnos ? "/" + d.slice(2,4) : ""}`;
  if (S.filtroDia && !dias.includes(S.filtroDia)) S.filtroDia = "";
  $("#dias").innerHTML = dias.length > 1 || S.filtroDia ? dias.map(d => `<button type="button" class="chip ${d===S.filtroDia?"on":""}" data-d="${d}" title="${d.split("-").reverse().join("/")}">${rotDia(d)}</button>`).join("") : "";
  $("#dias").querySelectorAll(".chip").forEach(c => c.onclick = () => { S.filtroDia = S.filtroDia === c.dataset.d ? "" : c.dataset.d; montarFiltros(); desenhar(); });
  montarBlocos();
  $("#lotesInfo").textContent = `${lotes.length} lote(s)`;
}

/* ============================================================ desenho */
function desenhar(){
  const regs = filtrados();
  const ids = new Set(regs.map(r => r.id));
  const der = id => S.derivado.get(id) || {};
  const comCoord = regs.filter(r => r.lat != null);
  const anom = ADMIN ? comCoord.filter(r => der(r.id).anomalia) : [];
  const grupos = S.grupos.filter(g => ids.has(g.rep));
  const tapados = grupos.filter(g => g.tapado), soAntes = grupos.filter(g => !g.tapado);

  $("#kInt").textContent = tapados.length;
  if (ADMIN){ $("#kFotos").textContent = regs.length; $("#kAnom").textContent = anom.length; }
  else {
    $("#kDias").textContent = new Set(tapados.map(g => g.dia).filter(Boolean)).size;
    $("#kKm").textContent = new Set(tapados.filter(g => der(g.rep).x != null).map(g => `${(S.tracado.blocos[der(g.rep).bloco]||{}).rodovia}:${Math.floor(der(g.rep).x)}`)).size;
  }

  desenharEixo();
  desenharTrechos(tapados);
  if (calor){ mapa.removeLayer(calor); calor = null; }
  if (marcado("#lCalor") && tapados.length){
    calor = L.heatLayer(tapados.map(g => [g.lat, g.lon, 1]), {
      radius:valor("#rRaio", PADRAO_CAMADAS["#rRaio"]), blur:Math.round(valor("#rRaio", PADRAO_CAMADAS["#rRaio"])*0.8), max:valor("#rMax", PADRAO_CAMADAS["#rMax"]), minOpacity:.35, maxZoom:14,
      gradient:{.15:"#2b6cb0", .35:"#38b2ac", .55:"#ecc94b", .75:"#ed8936", 1:"#e53e3e"}
    }).addTo(mapa);
  }
  gPontos.clearLayers(); gAnom.clearLayers(); marcadores.clear();
  if (marcado("#lPontos")){
    for (const g of (marcado("#oSoAntes") ? grupos : tapados)){
      const raio = g.fotos.length > 1 ? 5.5 : 4.5;
      const m = L.circleMarker([g.lat, g.lon], {renderer:render, radius:raio, _raioBase:raio, weight:1.2, color:"#fff",
        fillColor:g.tapado ? "#1f6feb" : "#8b8e95", fillOpacity:.9});
      m.bindPopup(() => popupGrupo(g), {maxWidth:300});
      m.bindTooltip(() => dicaGrupo(g), {direction:"top", offset:[0,-8], opacity:1, className:"dica-foto"});
      m.addTo(gPontos);
      for (const id of g.fotos) marcadores.set(id, m);
    }
  }
  estiloPontos();
  if (ADMIN && marcado("#lAnom")){
    for (const r of anom){
      const m = L.marker([r.lat, r.lon], {icon:L.divIcon({className:"", html:'<div class="anom-icon"></div>', iconSize:[18,18]}), zIndexOffset:1000});
      m.bindPopup(() => popup(r), {maxWidth:290});
      m.bindTooltip(() => dicaFoto(r, `Inconsistência · ${der(r.id).motivo || ""}`), {direction:"top", offset:[0,-10], opacity:1, className:"dica-foto"});
      m.addTo(gAnom); marcadores.set(r.id, m);
    }
  }
  atualizarLegenda();
  if (ADMIN){
    $("#anomList").innerHTML = anom.length ? anom.map(r => `
      <div class="item bad" data-id="${esc(r.id)}" tabindex="0"><img src="${mini(r.id)}" alt="" loading="lazy">
        <div><b>${esc(der(r.id).motivo)}</b><span>${fmtData(r.data)} · ${esc(r.arquivo)}</span>${r.coord_revisada?'<br><span>coordenada corrigida na releitura do OCR</span>':''}${window.botaoAjuste ? window.botaoAjuste(r.id) : ""}</div></div>`).join("")
      : `<p class="hint" style="margin:0">Nenhuma inconsistência no filtro atual.</p>`;
    $("#anomList").querySelectorAll(".item").forEach(el => el.onclick = () => focar(el.dataset.id));
    const fig = (r, cap) => `<figure><img src="${mini(r.id)}" data-full="${grande(r.id)}" alt="" loading="lazy"><figcaption class="mot">${cap}</figcaption>${window.botaoAjuste ? window.botaoAjuste(r.id) : ""}</figure>`;
    const f = filtros();
    const sem = regs.filter(r => r.lat == null);
    const outros = S.todos.filter(r => r.classe === "outro" && !r._dup && (!f.lote || r.lote === f.lote));
    const fotosAntes = soAntes.flatMap(g => g.fotos.map(id => S.porId.get(id)).filter(Boolean));
    $("#semSum").innerHTML = `Sem coordenada: <b>${sem.length}</b> foto(s)`;
    $("#outSum").innerHTML = `Não relacionadas ao tapa-buraco: <b>${outros.length}</b> foto(s)`;
    $("#antSum").innerHTML = `Buraco sem foto de tapado: <b>${soAntes.length}</b> buraco(s), ${fotosAntes.length} foto(s)`;
    $("#semGrid").innerHTML = sem.map(r => fig(r, `${fmtData(r.data)}${r.classe ? " · " + esc(r.classe) : ""}`)).join("");
    $("#outGrid").innerHTML = outros.map(r => fig(r, esc(r.motivo_exclusao || "não relacionada"))).join("");
    $("#antGrid").innerHTML = fotosAntes.map(r => fig(r, fmtData(r.data))).join("");
    $("#semInfo").textContent = `${sem.length + outros.length + fotosAntes.length} foto(s)`;
  }
  graficoKm(tapados.map(g => S.porId.get(g.rep)));
  graficoDia(tapados.map(g => S.porId.get(g.rep)));
  $("#sub").textContent = S.todos.length
    ? (ADMIN ? `${S.todos.filter(r => !r._dup && r.classe !== "outro").length} fotos · ${periodo(S.todos)}` : `${S.grupos.length} buracos tapados · ${periodo(S.todos)}`)
    : "Nenhum dado ainda.";
}

function periodo(regs){
  const ds = regs.map(diaDe).filter(Boolean).sort();
  if (!ds.length) return "sem datas";
  const a = ds[0].slice(0,10), b = ds[ds.length-1].slice(0,10);
  return a === b ? fmtData(a).slice(0,10) : `${fmtData(a).slice(0,10)} a ${fmtData(b).slice(0,10)}`;
}

/* ============================================================ intensidade por trecho */
/* Métrica: buracos tapados (válidos, com os filtros atuais) por km de rodovia, em trechos
   fixos do eixo do mesmo tamanho do resumo (1, 2 ou 5 km). Divide pelo comprimento real
   do trecho (o último trecho de um bloco pode ser mais curto), para comparar trechos. */
const NIVEIS = [
  {nome:"Sem registro",  min:0,    cor:"#8b8f96", peso:2,  op:.45},
  {nome:"Baixa",         min:0.01, cor:"#ffe08a", peso:4,  op:.95},
  {nome:"Média",         min:2,    cor:"#ffb347", peso:6,  op:.95},
  {nome:"Alta",          min:4,    cor:"#ff6a1a", peso:8,  op:.95},
  {nome:"Muito alta",    min:7,    cor:"#e0241b", peso:10, op:1},
];
const nivelDe = taxa => [...NIVEIS].reverse().find(n => taxa >= n.min) || NIVEIS[0];
const fmtTaxa = t => t.toLocaleString("pt-BR", {maximumFractionDigits:1});
const rotNivel = (n, i) => i === 0 ? "nenhum" : i === NIVEIS.length-1 ? `${fmtTaxa(n.min)} ou mais` : `${i === 1 ? "até" : fmtTaxa(n.min) + " a"} ${fmtTaxa(NIVEIS[i+1].min)}`;
let S_trechos = [];
function calcularTrechos(tapados){
  const bin = +$("#binKm").value, sel = new Set(blocosAtivos()), out = [];
  const cont = new Map();
  for (const g of tapados){ const d = S.derivado.get(g.rep) || {}; if (d.x == null) continue;
    const k = `${d.bloco}|${Math.floor(d.x / bin)}`; cont.set(k, (cont.get(k)||0) + 1); }
  for (const l of linhasTracado()){
    if (!sel.has(l.bloco)) continue;
    if (l.pts[0][2] == null) continue;
    const rod = S.tracado.blocos[l.bloco].rodovia || "";
    let atual = null;
    for (const p of l.pts){
      const k = Math.floor(p[2] / 1000 / bin);
      if (!atual || atual.k !== k){
        if (atual){ atual.pts.push([p[0], p[1]]); out.push(atual); }
        atual = {k, bloco:l.bloco, rodovia:rod, pts:[], e0:p[2], e1:p[2]};
      }
      atual.pts.push([p[0], p[1]]); atual.e1 = p[2];
    }
    if (atual) out.push(atual);
  }
  for (const t of out){
    t.ini = t.k * bin; t.fim = t.ini + bin;
    t.n = cont.get(`${t.bloco}|${t.k}`) || 0;
    t.comp = Math.max((t.e1 - t.e0) / 1000, bin / 2);       // trecho muito curto conta como meio intervalo
    t.taxa = t.n / t.comp;
    t.nivel = t.n ? nivelDe(t.taxa) : NIVEIS[0];
  }
  return out;
}
function desenharTrechos(tapados){
  gSeg.clearLayers();
  S_trechos = marcado("#lSeg") && S.temEstacas ? calcularTrechos(tapados) : [];
  // os de menor intensidade primeiro, para os mais intensos ficarem por cima
  const ord = [...S_trechos].sort((a, b) => NIVEIS.indexOf(a.nivel) - NIVEIS.indexOf(b.nivel));
  for (const t of ord){
    const nv = t.nivel, i = NIVEIS.indexOf(nv);
    if (i > 0) L.polyline(t.pts, {renderer:render, color:"#14161a", weight:nv.peso + 3, opacity:.55, interactive:false, lineCap:"butt"}).addTo(gSeg);
    const linha = L.polyline(t.pts, {renderer:render, color:nv.cor, weight:nv.peso, opacity:nv.op, lineCap:"butt", dashArray: i ? null : "4 5"});
    const txt = () => `<div class="dica-trecho"><b>${esc(t.rodovia)} · Bloco ${esc(t.bloco)}</b><span>km ${fmtNum(t.ini,0)} a ${fmtNum(t.fim,0)}</span>
      <span><b>${t.n}</b> buraco(s) tapado(s) · <b>${fmtTaxa(t.taxa)}</b> por km</span><span class="nv"><i style="background:${nv.cor}"></i>Intensidade: ${nv.nome}</span></div>`;
    linha.bindTooltip(txt, {sticky:true, direction:"top", opacity:1, className:"dica-foto"});
    linha.bindPopup(txt);   // no celular: toque no trecho
    linha.addTo(gSeg);
  }
  atualizarLegenda();
}
const legenda = L.control({position:"bottomleft"});
legenda.onAdd = () => { const d = L.DomUtil.create("div", "legenda-trechos"); L.DomEvent.disableClickPropagation(d); return d; };
legenda.addTo(mapa);
function atualizarLegenda(){
  const el = document.querySelector(".legenda-trechos");
  if (!el) return;
  if (!S_trechos.length){ el.hidden = true; return; }
  el.hidden = false;
  const bin = +$("#binKm").value, qtd = NIVEIS.map(n => S_trechos.filter(t => t.nivel === n).length);
  const fechada = el.dataset.fechada === "1";
  el.innerHTML = `<button type="button" class="leg-tit" aria-expanded="${!fechada}">Buracos tapados por km <span>${fechada ? "▸" : "▾"}</span></button>
    <div class="leg-corpo" ${fechada ? "hidden" : ""}>
    ${NIVEIS.map((n, i) => `<div class="leg-item"><i style="background:${n.cor};height:${Math.max(3, n.peso - 1)}px;${i ? "" : "opacity:.6"}"></i><span>${n.nome}</span><em>${rotNivel(n, i)}</em><small>${qtd[i]}</small></div>`).slice().reverse().join("")}
    ${ADMIN && marcado("#lAnom") ? `<div class="leg-anom"><i></i><span>Inconsistência</span><em></em><small>${gAnom.getLayers().length}</small></div>` : ""}
    <p>Trechos de ${bin} km do eixo, com os filtros atuais. Passe o mouse (ou toque) num trecho para ver o valor. O número à direita é quantos trechos há em cada nível.</p></div>`;
  el.querySelector(".leg-tit").onclick = () => { el.dataset.fechada = fechada ? "0" : "1"; atualizarLegenda(); };
}

/* pontos discretos de longe: os trechos coloridos mandam na visão geral; de perto, cada buraco volta ao tamanho normal */
function estiloPontos(){
  const z = mapa.getZoom();
  document.getElementById("map").classList.toggle("zoom-longe", z <= 11);
  const e = z <= 10 ? {r:1.8, w:0, op:.6} : z <= 12 ? {r:3, w:.8, op:.85} : {r:null, w:1.2, op:.9};
  gPontos.eachLayer(m => {
    if (!m.setRadius) return;
    m.setRadius(e.r ?? m.options._raioBase);
    m.setStyle({weight:e.w, fillOpacity:e.op});
  });
}
mapa.on("zoomend", estiloPontos);

/* fundo discreto: o mapa de fundo perde cor e brilho para as intervenções se destacarem */
function aplicarFundo(){
  const base = Object.entries(bases).find(([, l]) => mapa.hasLayer(l))?.[0] || "";
  const el = document.getElementById("map");
  el.dataset.fundo = !marcado("#oFundo") ? "" : base === "Satélite" ? "sat" : base === "Ruas" ? "ruas" : "escuro";
}
mapa.on("baselayerchange", aplicarFundo);
if (innerWidth <= 820) setTimeout(() => { const el = document.querySelector(".legenda-trechos"); if (el){ el.dataset.fechada = "1"; atualizarLegenda(); } }, 0);

/* ============================================================ popups */
function popup(r, semFoto){
  const d = S.derivado.get(r.id) || {};
  const tags = [];
  if (d.anomalia) tags.push(`<span class="tag bad">Inconsistência · ${esc(d.motivo)}</span>`);
  if (d.travado && !d.anomalia) tags.push(`<span class="tag warn">Mesma coordenada de outra foto em outro horário</span>`);
  if (r.coord_revisada) tags.push(`<span class="tag warn">Coordenada corrigida (OCR)</span>`);
  if (r.data_revisada) tags.push(`<span class="tag warn">Data corrigida (OCR leu ${esc(fmtData(r.data_ocr_original))})</span>`);
  if (r.data_suspeita) tags.push(`<span class="tag warn">Data do carimbo posterior ao envio — conferir</span>`);
  if (r.data_fonte === "nome_arquivo") tags.push(`<span class="tag warn">Data do envio, não da foto</span>`);
  const b = d.bloco != null ? S.tracado.blocos[d.bloco] : null;
  const kmTxt = d.est != null ? `<b>${fmtEst(d.est)}</b> · Bloco ${esc(d.bloco)}${b?.rodovia ? " · " + esc(b.rodovia) : ""}` : "—";
  return `<div class="pp">
    ${!semFoto ? `<img src="${mini(r.id)}" data-full="${grande(r.id)}" alt="Foto da intervenção">` : ""}
    ${tags.join("")}
    <table>
      <tr><td>Data</td><td><b>${fmtData(r.data)}</b> <span style="color:var(--ink-3)">(${esc(r.data_fonte||"sem data")})</span></td></tr>
      <tr><td>Coordenada</td><td class="mono">${r.lat.toFixed(6)}, ${r.lon.toFixed(6)}</td></tr>
      <tr><td>Estaca</td><td>${kmTxt}</td></tr>
      ${r.km != null ? `<tr><td>No carimbo</td><td>km ${fmtNum(r.km, Number.isInteger(r.km) ? 0 : 3)}${Number.isInteger(r.km) ? ' <span style="color:var(--ink-3)">(km do app de câmera, pouco confiável)</span>' : ""}</td></tr>` : ""}
      ${r.rodovia||r.local ? `<tr><td>Local</td><td>${esc([r.rodovia, r.local, r.sentido].filter(Boolean).join(" · "))}</td></tr>` : ""}
      ${d.dist != null ? `<tr><td>Ao eixo</td><td>${fmtNum(d.dist)} m</td></tr>` : ""}
      <tr><td>Lote</td><td>${esc(r.lote)}</td></tr>
      <tr><td>Arquivo</td><td style="word-break:break-all;font-size:11.5px">${esc(r.arquivo)}</td></tr>
    </table>
    ${ADMIN && r.texto_ocr ? `<details><summary>Texto lido no carimbo</summary><p>${esc(r.texto_ocr)}</p></details>` : ""}
    ${ADMIN && !semFoto && window.botaoAjuste ? window.botaoAjuste(r.id) : ""}
  </div>`;
}
/* dica ao passar o mouse: a foto que originou o ponto (a foto do buraco tapado), data e classificação */
const ROT_CLASSE = {antes:"Antes (buraco aberto)", depois:"Tapado", outro:"Não relacionada"};
function dicaFoto(r, situacao){
  if (!r) return `<div class="dica"><div class="dica-sem">Foto indisponível</div></div>`;
  const d = S.derivado.get(r.id) || {}, dia = diaDe(r);
  const quando = dia ? fmtData(r.data) : "Sem data válida no carimbo";
  const est = d.est != null ? `Estaca ${fmtEst(d.est)} · Bloco ${esc(d.bloco)}` : "";
  return `<div class="dica">
    <img class="dica-img" src="${mini(r.id)}" alt="Foto do ponto" width="220" height="165">
    <b>${esc(quando)}</b>
    <span>${esc(situacao)}</span>
    ${est ? `<span>${est}</span>` : ""}
    <span class="dica-arq">${esc(r.arquivo)}</span>
  </div>`;
}
function dicaGrupo(g){
  const rep = S.porId.get(g.rep);
  let sit;
  if (!ADMIN) sit = "Buraco tapado";
  else {
    const n = g.fotos.length;
    sit = g.tapado ? `${ROT_CLASSE[rep && rep.classe] || "Sem classificação"} · buraco com ${n} foto(s)` : `Sem foto do buraco tapado · ${n} foto(s)`;
  }
  return dicaFoto(rep, sit);
}
document.addEventListener("error", e => {
  const t = e.target;
  if (t && t.tagName === "IMG" && t.classList.contains("dica-img")){
    const div = document.createElement("div"); div.className = "dica-sem"; div.textContent = "Foto indisponível";
    t.replaceWith(div);
  }
}, true);
function popupGrupo(g){
  const fotos = g.fotos.map(id => S.porId.get(id)).filter(Boolean);
  const rep = S.porId.get(g.rep);
  if (!ADMIN) return popup(rep);   // visualizador: só a foto do buraco tapado
  const ord = [...fotos].sort((a,b) => (a.classe === "depois") - (b.classe === "depois") || (a.data||"").localeCompare(b.data||""));
  const rot = c => c === "antes" ? "Antes" : c === "depois" ? "Tapado" : "";
  const cab = g.tapado ? `<span class="tag info">1 buraco · ${fotos.length} foto(s)</span>` : `<span class="tag warn">Sem foto do buraco tapado — fora do calor</span>`;
  return `<div class="pp">${cab}
    <div class="fotos">${ord.map(f => `<figure><img src="${mini(f.id)}" data-full="${grande(f.id)}" alt="">${rot(f.classe) || f.data ? `<figcaption>${rot(f.classe)} ${(f.data||"").slice(11,16)}</figcaption>` : ""}${window.botaoAjuste ? window.botaoAjuste(f.id) : ""}</figure>`).join("")}</div>
    ${popup(rep, true).replace('<div class="pp">', "<div>")}
  </div>`;
}
function focar(id){
  const r = S.porId.get(id), m = marcadores.get(id);
  if (!r) return;
  mapa.flyTo([r.lat, r.lon], Math.max(mapa.getZoom(), 16), {duration:.8});
  if (m) setTimeout(() => m.openPopup(), 850);
  if (innerWidth <= 820) $("#painel").classList.remove("open");
}
document.addEventListener("click", e => {
  const img = e.target.closest("img[data-full]");
  if (img){ $("#lb img").src = img.dataset.full; $("#lb").classList.add("on"); return; }
  if (e.target.closest("#lb")) $("#lb").classList.remove("on");
});
function enquadrarMapa(){
  const pts = S.grupos.filter(g => g.tapado).map(g => [g.lat, g.lon]);
  if (pts.length) mapa.fitBounds(L.latLngBounds(pts).pad(.08));
  else if (linhasTracado().length) mapa.fitBounds(L.latLngBounds(linhasTracado().flatMap(l => l.pts.map(p => [p[0], p[1]]))));
}

/* ============================================================ gráficos */
function barras(el, dados, opts={}){
  const W = 340, H = 120, pb = 18, pt = 14, n = dados.length || 1;
  const max = Math.max(1, ...dados.map(d => d.v)), bw = W / n;
  let s = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img">`;
  dados.forEach((d, i) => {
    const h = d.v ? Math.max(2, (H-pb-pt) * d.v / max) : 2;
    s += `<rect class="${d.v ? "b" : "z"}"${d.v && d.cor ? ` style="fill:${d.cor}"` : ""} x="${i*bw+1}" y="${H-pb-h}" width="${Math.max(1,bw-2)}" height="${h}" rx="1.5" data-i="${i}"><title>${esc(d.titulo)}</title></rect>`;
    if (d.v && (n <= 16 || d.v === max)) s += `<text class="lbl" x="${i*bw+bw/2}" y="${H-pb-h-3}" text-anchor="middle">${d.v}</text>`;
  });
  const passo = Math.ceil(n / 8);
  dados.forEach((d, i) => { if (i % passo === 0) s += `<text class="lbl" x="${i*bw+bw/2}" y="${H-4}" text-anchor="middle">${esc(d.rot)}</text>`; });
  el.innerHTML = s + "</svg>";
  if (opts.onClick) el.querySelectorAll("rect").forEach(r => r.onclick = () => opts.onClick(dados[+r.dataset.i]));
}
function graficoKm(regs){
  const el = $("#chKm"), nota = $("#chKmNota");
  const bin = +$("#binKm").value;
  S.faixas = new Map();
  const idsB = blocosAtivos().filter(id => S.tracado.blocos[id]).sort(ordBloco);
  nota.innerHTML = "";
  if (!idsB.length || !S.temEstacas){
    el.innerHTML = `<p class="vazio">O resumo por km precisa do eixo com estacas (KMZ) do bloco.</p>`;
    return;
  }
  // um resumo por rodovia: BR-277 e BR-373 têm quilometragens que se sobrepõem
  const porRod = new Map();
  for (const id of idsB){ const r = S.tracado.blocos[id].rodovia || `Bloco ${id}`; if (!porRod.has(r)) porRod.set(r, []); porRod.get(r).push(id); }
  const pontos = regs.filter(Boolean).map(r => ({r, d:S.derivado.get(r.id) || {}})).filter(o => o.d.x != null);
  el.innerHTML = [...porRod.keys()].map((rod, i) => `<div class="ch-rod"><h3 class="ch-tit">${esc(rod)} <small>Bloco${porRod.get(rod).length > 1 ? "s" : ""} ${porRod.get(rod).map(esc).join(", ")}</small></h3><div class="ch-area" data-i="${i}"></div><p class="hint ch-nota" data-i="${i}"></p></div>`).join("");
  [...porRod.entries()].forEach(([rod, ids], i) => {
    const area = el.querySelector(`.ch-area[data-i="${i}"]`), txt = el.querySelector(`.ch-nota[data-i="${i}"]`);
    const es = ids.flatMap(id => S.tracado.blocos[id].pts.map(p => p[2])).filter(v => v != null);
    if (!es.length){ area.innerHTML = `<p class="vazio">Este bloco não tem estacas no KMZ.</p>`; return; }
    const lo = Math.floor(Math.min(...es)/1000/bin)*bin, hi = Math.ceil(Math.max(...es)/1000/bin)*bin;
    const meus = pontos.filter(o => ids.includes(o.d.bloco) && o.d.x >= lo && o.d.x <= hi);
    if (!meus.length){
      area.innerHTML = `<p class="vazio">Nenhuma intervenção na ${esc(rod)} com os filtros atuais (km ${fmtNum(lo,0)} a ${fmtNum(hi,0)}, faixas de ${bin} km).</p>`;
      return;
    }
    const nb = Math.min(400, Math.max(1, Math.round((hi-lo)/bin)));
    const cont = Array.from({length:nb}, (_,k) => ({ini:lo+k*bin, v:0, regs:[]}));
    for (const o of meus){ const k = Math.min(nb-1, Math.floor((o.d.x-lo)/bin)); cont[k].v++; cont[k].regs.push(o.r); }
    // mesma escala de cores da legenda do mapa: buracos tapados por km de eixo existente na faixa
    const faixaE = cont.map(() => [Infinity, -Infinity]);
    for (const e of es){ const k = Math.min(nb-1, Math.floor((e/1000-lo)/bin)); if (k >= 0){ faixaE[k][0] = Math.min(faixaE[k][0], e); faixaE[k][1] = Math.max(faixaE[k][1], e); } }
    cont.forEach((c, k) => {
      const [a, b] = faixaE[k];
      c.comp = Math.max(isFinite(a) ? (b - a) / 1000 : 0, bin / 2);
      c.taxa = c.v / c.comp; c.nivel = c.v ? nivelDe(c.taxa) : NIVEIS[0];
    });
    barras(area, cont.map((c, k) => ({rot:String(Math.round(c.ini)), v:c.v, cor:c.nivel.cor, regs:c.regs, ini:c.ini, k,
        titulo:`${rod} km ${fmtNum(c.ini,0)}–${fmtNum(c.ini+bin,0)}: ${c.v} buraco(s) · ${fmtTaxa(c.taxa)} por km · intensidade ${c.nivel.nome.toLowerCase()}`})),
      {onClick: d => {
        abrirDetalhe(rod, d.k);
        if (d.regs.length) return mapa.flyToBounds(L.latLngBounds(d.regs.map(r=>[r.lat,r.lon])).pad(.4), {maxZoom:16, duration:.8});
        const p = ids.flatMap(id => S.tracado.blocos[id].pts).filter(p => p[2] != null && p[2]/1000 >= d.ini && p[2]/1000 <= d.ini+bin).map(p=>[p[0],p[1]]);
        if (p.length) mapa.flyToBounds(L.latLngBounds(p), {duration:.8});
      }});
    S.faixas.set(rod, {ids, bin, cont, area});
    const vazios = cont.filter(c => !c.v).length, top = [...cont].sort((a,b)=>b.v-a.v)[0];
    let run = 0, best = {n:0}; cont.forEach((c,k) => { run = c.v ? 0 : run+1; if (run > best.n) best = {n:run, fim:k}; });
    txt.innerHTML = `<b>${meus.length}</b> buraco(s) tapado(s). Mais intervenções: <b>km ${fmtNum(top.ini,0)}–${fmtNum(top.ini+bin,0)}</b> (${top.v}). ${vazios} de ${nb} faixas de ${bin} km sem nenhuma`
      + (best.n ? `; maior trecho sem nenhuma: km ${fmtNum(cont[best.fim-best.n+1].ini,0)}–${fmtNum(cont[best.fim].ini+bin,0)}` : "") + ".";
  });
  nota.innerHTML = `Cor da barra = intensidade (buracos tapados por km), a mesma da legenda do mapa. Buracos projetados na estaca mais próxima do KMZ. Clique numa barra para ver os detalhes do intervalo e ir ao trecho.`;
  // o painel aberto acompanha os filtros; trocar o tamanho da faixa fecha o painel
  if (S.det){ if (S.det.bin === bin && S.faixas.has(S.det.rod)) abrirDetalhe(S.det.rod, S.det.k, true); else fecharDetalhe(); }
}

/* ============================================================ detalhes de uma barra do resumo por km */
const fmtDia = d => d ? `${d.slice(8,10)}/${d.slice(5,7)}` : "—";
const contar = arr => { const m = new Map(); for (const v of arr) m.set(v, (m.get(v)||0)+1); return [...m.entries()]; };
function fecharDetalhe(){
  S.det = null;
  const el = $("#detTrecho"); el.hidden = true; el.innerHTML = "";
  document.querySelectorAll("#chKm rect.sel").forEach(r => r.classList.remove("sel"));
}
function abrirDetalhe(rod, k, manterRolagem){
  const F = S.faixas.get(rod); if (!F || !F.cont[k]) return fecharDetalhe();
  const c = F.cont[k], fim = c.ini + F.bin, el = $("#detTrecho");
  S.det = {rod, k, bin:F.bin};
  document.querySelectorAll("#chKm rect.sel").forEach(r => r.classList.remove("sel"));
  const rect = F.area.querySelector(`rect[data-i="${k}"]`); if (rect) rect.classList.add("sel");
  const der = id => S.derivado.get(id) || {};
  const grupoDe = new Map(S.grupos.map(g => [g.rep, g]));
  const regs = [...c.regs].sort((a, b) => (der(a.id).est ?? 0) - (der(b.id).est ?? 0));
  const ests = regs.map(r => der(r.id).est).filter(v => v != null);
  const blocos = [...new Set(regs.map(r => der(r.id).bloco).filter(Boolean))].sort(ordBloco);
  const blocosEixo = F.ids.filter(id => S.tracado.blocos[id].pts.some(p => p[2] != null && p[2]/1000 >= c.ini && p[2]/1000 < fim));
  const dias = contar(regs.map(r => diaDe(r)).filter(Boolean)).sort((a, b) => a[0].localeCompare(b[0]));
  const lotes = contar(regs.map(r => r.lote).filter(Boolean)).sort((a, b) => b[1] - a[1]);
  const linha = (rot, val) => `<div><dt>${rot}</dt><dd>${val}</dd></div>`;
  // administrador: o que mais caiu neste intervalo (mesmos filtros de data, lote e bloco)
  let extraAdm = "";
  if (ADMIN){
    const f = filtros();
    const noIntervalo = S.todos.filter(r => { const d = der(r.id); return d.x != null && d.x >= c.ini && d.x < fim && F.ids.includes(d.bloco) && passa(r, f); });
    const inc = noIntervalo.filter(r => !r._dup && r.classe !== "outro" && der(r.id).anomalia);
    const semTap = S.grupos.filter(g => !g.tapado && noIntervalo.some(r => r.id === g.rep));
    const fotos = regs.reduce((s, r) => s + ((grupoDe.get(r.id) || {}).fotos || [r.id]).length, 0);
    extraAdm = linha("Fotos dos buracos", `${fotos} (antes e depois do mesmo buraco)`)
      + linha("Inconsistências", inc.length ? `${inc.length} — ${[...new Set(inc.map(r => der(r.id).motivo))].slice(0,3).map(esc).join("; ")}` : "nenhuma")
      + linha("Buracos sem foto de tapado", String(semTap.length));
  }
  // campos que faltam: os que não existem no sistema e os que vieram vazios neste intervalo
  const vazio = (campo, nome) => { const n = regs.filter(r => r[campo] == null || r[campo] === "").length; return n ? `${nome} vazio em ${n} de ${regs.length}` : ""; };
  const vazios = regs.length ? [vazio("local", "local"), vazio("sentido", "sentido"), vazio("km", "km escrito no carimbo"),
    regs.some(r => r.data_fonte && r.data_fonte !== "carimbo" && r.data_fonte !== "exif") ? `data tirada do nome do arquivo em ${regs.filter(r => r.data_fonte && !["carimbo","exif"].includes(r.data_fonte)).length}` : ""].filter(Boolean) : [];
  el.innerHTML = `
    <div class="det-cab">
      <div><span class="det-eyebrow">Intervalo selecionado</span><h3>${esc(rod)} · km ${fmtNum(c.ini,0)}–${fmtNum(fim,0)}</h3></div>
      <button class="btn" type="button" id="detFechar" aria-label="Fechar detalhes e voltar ao relatório">✕ Voltar ao relatório</button>
    </div>
    <dl class="det-resumo">
      ${linha("Rodovia", esc(rod))}
      ${linha("Bloco(s)", blocos.length ? blocos.map(b => "Bloco " + esc(b)).join(", ") : (blocosEixo.length ? blocosEixo.map(b => "Bloco " + esc(b)).join(", ") + " (eixo)" : "—"))}
      ${linha("Intervalo", `km ${fmtNum(c.ini,0)} a ${fmtNum(fim,0)} · faixa de ${F.bin} km`)}
      ${linha("Estacas com buraco", ests.length ? `${fmtEst(Math.min(...ests))} a ${fmtEst(Math.max(...ests))}` : "—")}
      ${linha("Eixo no intervalo", `${fmtNum(c.comp, 1)} km`)}
      ${linha("Buracos tapados", `<b>${c.v}</b>`)}
      ${linha("Intensidade", `<span class="det-nivel"><i style="background:${c.nivel.cor}"></i>${esc(c.nivel.nome)} · ${fmtTaxa(c.taxa)} por km</span>`)}
      ${linha("Dias", dias.length ? dias.map(([d, n]) => `${fmtDia(d)} (${n})`).join(", ") : "—")}
      ${linha("Lote / ZIP", lotes.length ? lotes.map(([l, n]) => `${esc(l)} (${n})`).join(", ") : "—")}
      ${extraAdm}
    </dl>
    ${regs.length ? `<h4 class="det-sub">Buracos neste intervalo <small>clique para ver no mapa</small></h4>
    <ol class="det-lista">${regs.map(r => { const d = der(r.id), g = grupoDe.get(r.id);
      return `<li tabindex="0" data-id="${esc(r.id)}"><img src="${mini(r.id)}" alt="" loading="lazy">
        <div><b>${d.est != null ? "Estaca " + fmtEst(d.est) : "Sem estaca"}${d.bloco ? " · Bloco " + esc(d.bloco) : ""}</b>
        <span>${fmtData(r.data)}${r.lote ? " · " + esc(r.lote) : ""}${d.dist != null ? ` · ${fmtNum(d.dist)} m do eixo` : ""}</span>
        <span>${[r.local, r.sentido].filter(Boolean).map(esc).join(" · ")}${ADMIN && g ? `${r.local || r.sentido ? " · " : ""}${g.fotos.length} foto(s)` : ""}</span>
        <span class="det-arq">${esc(r.arquivo)}</span></div></li>`; }).join("")}</ol>`
    : `<p class="vazio">Nenhum buraco tapado neste intervalo com os filtros atuais.</p>`}
    <p class="det-falta"><b>O que os dados não informam:</b> o motivo (causa ou tipo do defeito), a dimensão ou área do reparo, o material e a equipe não são registrados pelas fotos nem pelo sistema.${vazios.length ? ` Neste intervalo: ${vazios.join("; ")}.` : ""}</p>`;
  el.hidden = false;
  $("#detFechar").onclick = () => { fecharDetalhe(); $("#chKm").scrollIntoView({block:"nearest"}); };
  el.querySelectorAll(".det-lista li").forEach(li => {
    li.onclick = () => focar(li.dataset.id);
    li.onkeydown = e => { if (e.key === "Enter" || e.key === " "){ e.preventDefault(); focar(li.dataset.id); } };
  });
  if (!manterRolagem) el.scrollIntoView({block:"nearest", behavior:"smooth"});
}
document.addEventListener("keydown", e => { if (e.key === "Escape" && S.det && !$("#lb").classList.contains("on")) fecharDetalhe(); });
function graficoDia(regs){
  const c = new Map();
  for (const r of regs.filter(Boolean)){ const d = diaDe(r); if (d) c.set(d, (c.get(d)||0)+1); }
  const dias = [...c.keys()].sort();
  if (!dias.length){ $("#chDia").innerHTML = '<p class="vazio">Nenhuma intervenção com data válida nos filtros atuais.</p>'; return; }
  const out = []; for (let t=Date.parse(dias[0]+"T12:00"); t<=Date.parse(dias.at(-1)+"T12:00"); t+=86400000){ out.push(new Date(t).toISOString().slice(0,10)); if (out.length>370) break; }
  barras($("#chDia"), out.map(d => ({rot:d.slice(8,10)+"/"+d.slice(5,7), v:c.get(d)||0, titulo:`${d.split("-").reverse().join("/")}: ${c.get(d)||0}`, dia:d})),
    {onClick: d => { S.filtroDia = S.filtroDia === d.dia ? "" : d.dia; montarFiltros(); desenhar(); }});
}

/* ============================================================ eventos */
for (const id of ["#fDe","#fAte","#fMes","#fLote"]) $(id).addEventListener("change", () => { S.filtroDia = ""; montarFiltros(); desenhar(); });
for (const id of ["#lCalor","#lPontos","#lAnom","#oSoAntes","#lTracado","#lKm","#lSeg","#rRaio","#rMax","#binKm"]){ const el = $(id); if (el) el.addEventListener("input", desenhar); }
if ($("#oFundo")) $("#oFundo").addEventListener("input", aplicarFundo);
if ($("#btnCsv")) $("#btnCsv").onclick = () => {
  const f = filtros(), q = new URLSearchParams(Object.entries({...f, blocos:f.blocos.join(",")}).filter(([,v]) => v));
  location.href = "/api/exportar.csv" + (q.toString() ? "?" + q : "");
};
$("#cabecalho").addEventListener("click", e => { if (innerWidth <= 820 && !e.target.closest("button,input,select,a,form")) $("#painel").classList.toggle("open"); });

/* atualiza sozinho quando um processamento termina */
async function verificarVersao(){
  try{
    const r = await fetch("/api/versao", {credentials:"same-origin", cache:"no-store"});
    if (r.status === 401){ location.href = "/login"; return; }
    const j = await r.json();
    if (ADMIN) $("#procInfo").textContent = j.processando ? `${j.processando} envio(s) em processamento. O mapa se atualiza sozinho quando terminar.` : "";
    if (S.versao && j.versao !== S.versao){ await carregar(false); toast("Mapa atualizado com os dados mais recentes."); }
  }catch(e){}
}
setInterval(verificarVersao, ADMIN ? 8000 : 30000);
aplicarFundo();
carregar(true).then(verificarVersao);
