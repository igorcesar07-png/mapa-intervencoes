/* Ajustes do administrador que valem para todos (GitHub Pages + Firebase).
 *
 * O site continua estático; o Firebase guarda só o que o administrador muda:
 *   ajustes/{idDaFoto}  correções da foto (classe, data, estaca, incluir/excluir) e, já calculado,
 *                       o que o visualizador passa a ver dessa foto (visivel/pub/mini)
 *   config/camadas      camadas padrão de todos
 *   privado/chave       senha que abre os dados cifrados (só o administrador lê)
 * Leitura de ajustes/config é pública; gravação só com login do administrador
 * (regra do Firestore pelo UID). Nenhuma senha fica neste arquivo: a chave da API do
 * Firebase (firebase-config.js) é um identificador público, não um segredo.
 */
(() => {
  const CFG = window.FIREBASE_CFG || {};
  const ATIVO = !!(CFG.apiKey && CFG.projectId);
  const BASE = ATIVO ? `https://firestore.googleapis.com/v1/projects/${CFG.projectId}/databases/(default)/documents` : "";
  const INTENCOES = ["classe", "data", "est", "bloco", "lat", "lon", "incluir"];
  const CAMPOS_PUB = ["id", "arquivo", "lote", "data", "data_fonte", "lat", "lon", "km", "rodovia", "local", "sentido"];
  const AJ = new Map();
  let sessao = null, ajustesLidos = false, camadasLidas = false, baseCache = null;

  /* ---------------- Firebase (REST, sem biblioteca) ---------------- */
  async function entrar(email, senha){
    const r = await fetch(`https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=${CFG.apiKey}`,
      {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({email, password:senha, returnSecureToken:true})});
    const j = await r.json();
    if (!r.ok) throw new Error(/INVALID|PASSWORD|EMAIL_NOT_FOUND|USER/.test(j.error?.message || "") ? "E-mail ou senha incorretos." : "Falha no login do Firebase.");
    sessao = {email:j.email, uid:j.localId, idToken:j.idToken, refreshToken:j.refreshToken, exp:Date.now() + (+j.expiresIn)*1000};
    return sessao;
  }
  async function token(){
    if (!sessao) throw new Error("Entre como administrador.");
    if (Date.now() > sessao.exp - 120000){
      const r = await fetch(`https://securetoken.googleapis.com/v1/token?key=${CFG.apiKey}`,
        {method:"POST", headers:{"Content-Type":"application/x-www-form-urlencoded"}, body:`grant_type=refresh_token&refresh_token=${encodeURIComponent(sessao.refreshToken)}`});
      const j = await r.json(); if (!r.ok) throw new Error("A sessão expirou. Recarregue a página e entre de novo.");
      sessao.idToken = j.id_token; sessao.refreshToken = j.refresh_token; sessao.exp = Date.now() + (+j.expires_in)*1000;
    }
    return sessao.idToken;
  }
  const lerDoc = d => { try { return d?.fields?.json ? JSON.parse(d.fields.json.stringValue) : null; } catch(_) { return null; } };
  async function fsGet(caminho, comLogin){
    const h = comLogin ? {Authorization:`Bearer ${await token()}`} : {};
    const r = await fetch(`${BASE}/${caminho}`, {headers:h, cache:"no-store"});
    if (r.status === 404) return null;
    if (!r.ok) throw new Error(`Firestore ${r.status}`);
    return lerDoc(await r.json());
  }
  async function fsListar(colecao){
    const out = []; let pt = "";
    do {
      const r = await fetch(`${BASE}/${colecao}?pageSize=300${pt ? "&pageToken=" + encodeURIComponent(pt) : ""}`, {cache:"no-store"});
      if (r.status === 404) break;
      if (!r.ok) throw new Error(`Firestore ${r.status}`);
      const j = await r.json();
      for (const d of j.documents || []) out.push([decodeURIComponent(d.name.split("/").pop()), lerDoc(d)]);
      pt = j.nextPageToken || "";
    } while (pt);
    return out;
  }
  async function fsGravar(caminho, obj){
    const r = await fetch(`${BASE}/${caminho}`, {method:"PATCH",
      headers:{Authorization:`Bearer ${await token()}`, "Content-Type":"application/json"},
      body:JSON.stringify({fields:{json:{stringValue:JSON.stringify(obj)}, por:{stringValue:sessao.email}, quando:{timestampValue:new Date().toISOString()}}})});
    if (!r.ok) throw new Error(r.status === 403 ? "Sem permissão para gravar (confira a regra do Firestore com o seu UID)." : `Falha ao gravar (${r.status}).`);
  }
  async function fsApagar(caminho){
    const r = await fetch(`${BASE}/${caminho}`, {method:"DELETE", headers:{Authorization:`Bearer ${await token()}`}});
    if (!r.ok && r.status !== 404) throw new Error(`Falha ao apagar (${r.status}).`);
  }
  window.FB = {ATIVO, entrar, fsGet, fsGravar, sessao: () => sessao};

  /* ---------------- camadas padrão ---------------- */
  function aplicarCamadas(obj){
    for (const [k, v] of Object.entries(obj || {})){
      PADRAO_CAMADAS[k] = v;
      const el = document.querySelector(k);
      if (el){ if (el.type === "checkbox") el.checked = !!v; else el.value = v; }
    }
    if (typeof aplicarFundo === "function") try { aplicarFundo(); } catch(_){}
  }
  function lerCamadasDaTela(){
    const o = {};
    document.querySelectorAll("#secCamadas input").forEach(el => { if (el.id) o["#" + el.id] = el.type === "checkbox" ? el.checked : +el.value; });
    return o;
  }

  /* ---------------- regras do sistema refeitas no navegador ---------------- */
  // mesmas regras de mapa/regras.py: buraco = fotos válidas do mesmo dia a <= 25 m do centro do grupo
  function reagrupar(j){
    const nc = !!+((j.parametros || {}).nao_classificadas_contam ?? 1), agrupa = +((j.parametros || {}).agrupa_m ?? 25);
    for (const r of j.registros){ const d = r.d || (r.d = {}); delete d.grupo; delete d.rep; }
    const validas = j.registros.filter(r => r.lat != null && !r.d.dup && r.classe !== "outro" && !r.d.anomalia);
    const ord = validas.map((r, i) => [r, i]).sort((a, b) => { const x = a[0].data || "", y = b[0].data || ""; return x < y ? -1 : x > y ? 1 : a[1] - b[1]; }).map(x => x[0]);
    const grupos = [];
    for (const r of ord){
      const dia = (r.data || "").slice(0, 10);
      const alvo = grupos.find(g => g.dia === dia && distM([g.lat, g.lon], [r.lat, r.lon]) <= agrupa);
      if (alvo){ alvo.fotos.push(r); alvo.lat = alvo.fotos.reduce((s, f) => s + f.lat, 0) / alvo.fotos.length; alvo.lon = alvo.fotos.reduce((s, f) => s + f.lon, 0) / alvo.fotos.length; }
      else grupos.push({dia, lat:r.lat, lon:r.lon, fotos:[r]});
    }
    j.grupos = grupos.map(g => {
      const tapado = g.fotos.some(f => f.classe === "depois" || (f.classe == null && nc));
      const rep = g.fotos.find(f => f.classe === "depois") || g.fotos[0], gid = "g" + rep.id;
      for (const f of g.fotos){ f.d.grupo = gid; f.d.rep = f === rep; }
      return {id:gid, lat:g.lat, lon:g.lon, dia:g.dia, tapado, rep:rep.id, fotos:g.fotos.map(f => f.id)};
    });
  }
  function aplicarIntencoes(j){
    const regs = new Map(j.registros.map(r => [r.id, r]));
    let mudou = false;
    for (const [id, a] of AJ){
      const r = regs.get(id); if (!r || !a) continue;
      const d = r.d || (r.d = {});
      if (a.data){ r.data = a.data; r.data_fonte = "manual"; mudou = true; }
      if (a.est != null){ d.est = a.est; d.bloco = a.bloco; r.lat = a.lat; r.lon = a.lon; d.dist = 0; d.anomalia = false; d.motivo = null; d.travado = false; mudou = true; }
      if (a.classe !== undefined){ r.classe = a.classe; mudou = true; }
      if (a.incluir){ if (r.classe !== "depois") r.classe = "depois"; d.anomalia = false; d.motivo = null; d.dup = null; mudou = true; }
      if (INTENCOES.some(k => a[k] !== undefined)) r.ajuste_admin = true;
    }
    if (mudou) reagrupar(j);
  }
  // o que o visualizador vê: a foto representativa de cada buraco tapado, só com campos públicos
  function vistaPublica(j){
    const regs = new Map(j.registros.map(r => [r.id, r])), v = new Map();
    for (const g of j.grupos){
      if (!g.tapado) continue;
      const r = regs.get(g.rep); if (!r) continue;
      const p = {}; for (const k of CAMPOS_PUB) p[k] = r[k] ?? null;
      // arredondado: o servidor (Python) e o navegador somam médias com diferença só na última casa
      const rd = (x, n) => typeof x === "number" ? +x.toFixed(n) : x;
      p.lat = rd(p.lat, 7); p.lon = rd(p.lon, 7);
      const d = r.d || {}; p.d = {}; for (const k of ["bloco", "est", "dist", "grupo"]) if (d[k] != null) p.d[k] = k === "est" || k === "dist" ? rd(d[k], 2) : d[k];
      p.glat = rd(g.lat, 7); p.glon = rd(g.lon, 7);
      v.set(r.id, p);
    }
    return v;
  }
  function aplicarVisibilidade(j){
    for (const [id, a] of AJ){
      if (!a || a.visivel === undefined) continue;
      j.registros = j.registros.filter(r => r.id !== id);
      j.grupos = j.grupos.filter(g => g.rep !== id);
      if (a.visivel && a.pub){
        const r = JSON.parse(JSON.stringify(a.pub)), {glat, glon} = r; delete r.glat; delete r.glon;
        j.registros.push(r);
        j.grupos.push({id:(r.d && r.d.grupo) || "g" + id, lat:glat ?? r.lat, lon:glon ?? r.lon, dia:(r.data || "").slice(0, 10), tapado:true, rep:id, fotos:[id]});
        if (a.mini){ window.DADOS_MAPA.mini = window.DADOS_MAPA.mini || {}; window.DADOS_MAPA.mini[id] = a.mini; }
      }
    }
  }

  /* chamado por carregar(), antes de montar mapa, filtros e gráficos */
  window.aplicarAjustes = async j => {
    if (!ATIVO) return;
    try{
      if (!camadasLidas){ camadasLidas = true; aplicarCamadas(await fsGet("config/camadas")); }
      if (!ajustesLidos){ ajustesLidos = true; for (const [id, a] of await fsListar("ajustes")) if (a) AJ.set(id, a); }
    }catch(e){ console.warn("Ajustes do Firebase indisponíveis:", e); return; }
    if (ADMIN) aplicarIntencoes(j); else aplicarVisibilidade(j);
    if (ADMIN && sessao) prepararTelaAdmin();
  };

  /* ---------------- gravação (administrador) ---------------- */
  async function salvar(id, mudanca){
    const atual = AJ.get(id) || {}, novo = {...atual};
    if (mudanca === null) for (const k of INTENCOES) delete novo[k];          // desfazer
    else for (const [k, v] of Object.entries(mudanca)) { if (v === undefined) delete novo[k]; else novo[k] = v; }
    const antes = new Map(AJ);
    AJ.set(id, novo);
    // recalcula a visão do administrador e, a partir dela, o que muda para os visualizadores
    if (!baseCache) baseCache = vistaPublica(JSON.parse(JSON.stringify(window.DADOS_MAPA.relatorio)));
    const j = JSON.parse(JSON.stringify(window.DADOS_MAPA.relatorio)); aplicarIntencoes(j);
    const quer = vistaPublica(j), base = baseCache;
    const ids = new Set([...quer.keys(), ...base.keys(), ...[...AJ.keys()]]);
    const gravar = [];
    for (const x of ids){
      const w = quer.get(x), b = base.get(x), a = AJ.get(x) || {};
      const doc = {}; for (const k of INTENCOES) if (a[k] !== undefined) doc[k] = a[k];
      if (w && (!b || JSON.stringify(w) !== JSON.stringify(b))){ doc.visivel = true; doc.pub = w; if (!b && window.DADOS_MAPA.mini?.[x]) doc.mini = window.DADOS_MAPA.mini[x]; }
      else if (!w && b) doc.visivel = false;
      const velho = antes.get(x) || {};
      if (JSON.stringify(doc) !== JSON.stringify(velho)) gravar.push([x, doc]);
    }
    try{
      for (const [x, doc] of gravar){
        if (Object.keys(doc).length){ await fsGravar(`ajustes/${encodeURIComponent(x)}`, doc); AJ.set(x, doc); }
        else { await fsApagar(`ajustes/${encodeURIComponent(x)}`); AJ.delete(x); }
      }
    }catch(e){ AJ.clear(); for (const [k, v] of antes) AJ.set(k, v); throw e; }
    await carregar(false);
    return gravar.length;
  }

  /* ---------------- tela do administrador ---------------- */
  window.botaoAjuste = id => (ATIVO && sessao) ? `<button type="button" class="btn-aj" data-aj="${esc(id)}">Ajustar</button>` : "";
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-aj]");
    if (!b) return;
    e.preventDefault(); e.stopPropagation(); abrirPainel(b.dataset.aj);
  }, true);

  function prepararTelaAdmin(){
    const sec = document.getElementById("secCamadas");
    if (sec && !document.getElementById("btnPadrao")){
      const div = document.createElement("div"); div.className = "row aj-padrao";
      div.innerHTML = `<button type="button" class="btn pri" id="btnPadrao">Salvar como padrão para todos</button><span class="hint" id="padraoMsg" role="status"></span>`;
      sec.appendChild(div);
      document.getElementById("btnPadrao").onclick = async () => {
        const m = document.getElementById("padraoMsg"); m.textContent = "Salvando…";
        try{ await fsGravar("config/camadas", lerCamadasDaTela()); m.textContent = "Salvo: todos passam a abrir assim."; }
        catch(err){ m.textContent = err.message; }
      };
    }
  }

  const SIT = r => {
    const d = S.derivado.get(r.id) || {};
    if (r.lat == null) return "Sem coordenada (fora do mapa)";
    if (r._dup) return "Repetida (duplicata) — fora do mapa";
    if (r.classe === "outro") return "Não relacionada — fora do mapa";
    if (d.anomalia) return `Inconsistência — fora do calor (${d.motivo || ""})`;
    const g = S.grupos.find(x => x.id === d.grupo);
    if (g && !g.tapado) return "Buraco sem foto de tapado — fora do calor";
    if (g && g.rep === r.id) return "No mapa: foto do buraco tapado";
    return "No mapa: foto extra do mesmo buraco";
  };
  function estParaPonto(bloco, est){
    const b = S.tracado.blocos[bloco]; if (!b) return null;
    const pts = b.pts.filter(p => p[2] != null).sort((a, c) => a[2] - c[2]);
    if (!pts.length || est < pts[0][2] - 50 || est > pts.at(-1)[2] + 50) return null;
    for (let i = 0; i < pts.length - 1; i++){
      const a = pts[i], c = pts[i+1];
      if (est >= a[2] && est <= c[2]){ const t = c[2] === a[2] ? 0 : (est - a[2]) / (c[2] - a[2]); return [a[0] + t*(c[0]-a[0]), a[1] + t*(c[1]-a[1])]; }
    }
    const q = est < pts[0][2] ? pts[0] : pts.at(-1); return [q[0], q[1]];
  }
  function abrirPainel(id){
    const r = S.porId.get(id); if (!r) return;
    const d = S.derivado.get(id) || {}, a = AJ.get(id) || {};
    let el = document.getElementById("ajPainel");
    if (!el){ el = document.createElement("aside"); el.id = "ajPainel"; el.className = "aj-painel"; el.setAttribute("role", "dialog"); el.setAttribute("aria-label", "Ajustar foto"); document.body.appendChild(el); }
    const blocos = Object.keys(S.tracado.blocos).sort(ordBloco);
    const temAjuste = INTENCOES.some(k => a[k] !== undefined);
    el.innerHTML = `
      <div class="aj-cab"><div><span class="det-eyebrow">Ajuste do administrador · vale para todos</span><h3>${esc(r.arquivo)}</h3></div>
        <button type="button" class="btn" id="ajFechar" aria-label="Fechar">✕</button></div>
      <img class="aj-foto" src="${mini(id)}" alt="" data-full="${grande(id)}">
      <p class="aj-sit"><b>${esc(SIT(r))}</b>${temAjuste ? ' · <span class="aj-tag">ajustada</span>' : ""}</p>
      <div class="aj-grupo"><span class="aj-rot">Classificação</span>
        <div class="aj-bts">
          <button type="button" class="btn ${r.classe === "antes" ? "on" : ""}" data-cl="antes">Antes</button>
          <button type="button" class="btn ${r.classe === "depois" ? "on" : ""}" data-cl="depois">Tapado</button>
          <button type="button" class="btn ${r.classe === "outro" ? "on" : ""}" data-cl="outro">Não relacionada / excluir</button>
        </div></div>
      <div class="aj-grupo"><button type="button" class="btn pri" id="ajIncluir">Incluir no mapa de calor</button>
        <span class="hint">Conta como buraco tapado, mesmo sendo inconsistência ou buraco sem foto de tapado.</span></div>
      <div class="aj-grupo"><span class="aj-rot">Data e hora</span>
        <div class="aj-linha"><input type="datetime-local" step="1" id="ajData" value="${esc((r.data || "").slice(0, 19))}"><button type="button" class="btn" id="ajSalvarData">Salvar data</button></div></div>
      <div class="aj-grupo"><span class="aj-rot">Estaca</span>
        <div class="aj-linha"><select id="ajBloco">${blocos.map(b => `<option value="${esc(b)}" ${b === d.bloco ? "selected" : ""}>Bloco ${esc(b)}</option>`).join("")}</select>
        <input type="text" id="ajEst" inputmode="numeric" placeholder="250+930" value="${d.est != null ? fmtEst(d.est) : ""}"><button type="button" class="btn" id="ajSalvarEst">Salvar estaca</button></div>
        <span class="hint">A foto vai para o ponto do eixo nessa estaca (corrige GPS errado ou foto sem coordenada).</span></div>
      ${temAjuste ? `<button type="button" class="btn ghost" id="ajDesfazer">Desfazer os ajustes desta foto</button>` : ""}
      <p class="aj-msg" id="ajMsg" role="status"></p>`;
    el.hidden = false;
    const msg = (t, tipo) => { const m = document.getElementById("ajMsg"); m.textContent = t; m.className = "aj-msg " + (tipo || ""); };
    const fazer = async (mud, ok) => {
      el.querySelectorAll("button,input,select").forEach(x => x.disabled = true); msg("Salvando para todos…");
      try{ const n = await salvar(id, mud); abrirPainel(id); document.getElementById("ajMsg").textContent = `${ok} Salvo no Firebase${n > 1 ? ` (${n} registros atualizados)` : ""}.`; document.getElementById("ajMsg").className = "aj-msg ok"; }
      catch(err){ el.querySelectorAll("button,input,select").forEach(x => x.disabled = false); msg(err.message, "erro"); }
    };
    document.getElementById("ajFechar").onclick = () => { el.hidden = true; };
    el.querySelectorAll("[data-cl]").forEach(b => b.onclick = () => fazer({classe:b.dataset.cl, incluir:undefined}, "Classificação alterada."));
    document.getElementById("ajIncluir").onclick = () => fazer({incluir:true, classe:undefined}, "Incluída no mapa de calor.");
    document.getElementById("ajSalvarData").onclick = () => {
      const v = document.getElementById("ajData").value;
      if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(v)) return msg("Informe data e hora.", "erro");
      const iso = v.length === 16 ? v + ":00" : v.slice(0, 19), t = Date.parse(iso);
      if (isNaN(t) || +iso.slice(0, 4) < 2020 || t > Date.now() + 86400000) return msg("Data impossível.", "erro");
      fazer({data:iso}, "Data corrigida.");
    };
    document.getElementById("ajSalvarEst").onclick = () => {
      const m = document.getElementById("ajEst").value.trim().match(/^(\d{1,4})\s*\+\s*(\d{1,3})$/);
      if (!m) return msg("Estaca no formato km+metros, ex.: 250+930.", "erro");
      const est = +m[1]*1000 + +m[2], bloco = document.getElementById("ajBloco").value, p = estParaPonto(bloco, est);
      if (!p) return msg(`A estaca ${fmtEst(est)} não está no eixo do Bloco ${bloco}.`, "erro");
      fazer({est, bloco, lat:+p[0].toFixed(6), lon:+p[1].toFixed(6)}, "Estaca corrigida.");
    };
    const dz = document.getElementById("ajDesfazer"); if (dz) dz.onclick = () => fazer(null, "Ajustes desfeitos.");
    document.addEventListener("keydown", function esc_(e){ if (e.key === "Escape"){ el.hidden = true; document.removeEventListener("keydown", esc_); } });
  }
})();
