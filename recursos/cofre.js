/* Área do administrador no GitHub Pages: abre os dados cifrados.
   AES-256-GCM, chave derivada da senha do site com PBKDF2-SHA256. Nenhuma senha fica em arquivo do site.
   Com o Firebase configurado: o administrador entra com e-mail e senha da conta Firebase; a senha do site
   fica guardada no Firestore em privado/chave, que só o UID do administrador lê (pedida uma única vez).
   Sem o Firebase: pede a senha do site diretamente. */
(() => {
  let chave = null, aguardandoChave = false;
  const V = (document.currentScript && document.currentScript.dataset.v) || String(Date.now());
  const FBA = !!(window.FB && window.FB.ATIVO);
  const $c = id => document.getElementById(id);
  async function derivar(senha, sal, iter){
    const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(senha.trim()), "PBKDF2", false, ["deriveKey"]);
    return crypto.subtle.deriveKey({name:"PBKDF2", hash:"SHA-256", salt:sal, iterations:iter}, base, {name:"AES-GCM", length:256}, false, ["decrypt"]);
  }
  const abrir = (k, buf) => crypto.subtle.decrypt({name:"AES-GCM", iv:buf.slice(0, 12)}, k, buf.slice(12));
  let resolver;
  window.DADOS_PRONTO = new Promise(r => resolver = r);
  window.decifrar = buf => abrir(chave, buf);

  // devolve true se a senha do site abriu os dados
  async function tentar(senhaSite){
    const r = await fetch(`admin.bin?v=${V}`, {cache:"no-store"});
    if (!r.ok) throw new Error("rede");
    const tudo = new Uint8Array(await r.arrayBuffer());
    const p = await (await fetch(`admin.json?v=${V}`, {cache:"no-store"})).json();
    const k = await derivar(senhaSite, tudo.slice(5, 21), p.iter);
    let dados;
    try { dados = JSON.parse(new TextDecoder().decode(await abrir(k, tudo.slice(5 + 16)))); } catch(_) { return false; }
    chave = k; window.DADOS_MAPA = dados;
    $c("cofre").remove();
    resolver(dados);
    return true;
  }
  if (FBA){
    $c("cofreEmailWrap").hidden = false;
    $c("cofreSenhaRot").textContent = "Senha do administrador (conta Firebase)";
    $c("cofreSenha").autocomplete = "current-password";
  }
  $c("cofreForm").addEventListener("submit", async e => {
    e.preventDefault();
    const msg = $c("cofreMsg"), btn = $c("cofreBtn");
    const aviso = (t, erro) => { msg.textContent = t; msg.className = "cofre-msg" + (erro ? " erro" : ""); };
    btn.disabled = true; aviso("Conferindo…");
    try{
      if (!FBA){
        if (!(await tentar($c("cofreSenha").value))) aviso("Senha incorreta. Confira e tente de novo.", true);
      } else if (!aguardandoChave){
        await FB.entrar($c("cofreEmail").value.trim(), $c("cofreSenha").value);
        const doc = await FB.fsGet("privado/chave", true);
        if (doc && doc.senha && await tentar(doc.senha)) return;
        aguardandoChave = true; $c("cofreChaveWrap").hidden = false; $c("cofreChave").focus();
        aviso(doc && doc.senha ? "A senha do site guardada no Firebase não abre estes dados. Informe a senha do site atual."
                               : "Primeira entrada: informe a senha do site (só desta vez; ela fica guardada no Firebase, visível só para você).");
      } else {
        const s = $c("cofreChave").value;
        if (!(await tentar(s))) { aviso("Senha do site incorreta.", true); }
        else { try { await FB.fsGravar("privado/chave", {senha:s.trim()}); } catch(err) { console.warn(err); } }
      }
    }catch(err){
      aviso(err.message === "rede" ? "Não foi possível carregar os dados. Verifique a conexão e tente de novo." : err.message, true);
    }finally{ if (document.getElementById("cofreBtn")) btn.disabled = false; }
  });
})();
