/* Área do administrador no GitHub Pages: abre os dados cifrados com a senha digitada.
   AES-256-GCM, chave derivada da senha com PBKDF2-SHA256. A senha não fica em nenhum arquivo do site:
   sem ela, admin.bin e fotos/x/*.bin são ilegíveis. A chave só existe na memória desta aba. */
(() => {
  let chave = null;
  // versão do site: evita misturar arquivos de publicações diferentes guardados em cache
  const V = (document.currentScript && document.currentScript.dataset.v) || String(Date.now());
  const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
  // o "sal" vem do próprio admin.bin (bytes 5 a 20), então senha, sal e dados são sempre da mesma publicação
  async function derivar(senha, sal, iter){
    const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(senha.trim()), "PBKDF2", false, ["deriveKey"]);
    return crypto.subtle.deriveKey({name:"PBKDF2", hash:"SHA-256", salt:sal, iterations:iter}, base, {name:"AES-GCM", length:256}, false, ["decrypt"]);
  }
  const abrir = (k, buf) => crypto.subtle.decrypt({name:"AES-GCM", iv:buf.slice(0, 12)}, k, buf.slice(12));
  let resolver;
  window.DADOS_PRONTO = new Promise(r => resolver = r);
  window.decifrar = buf => abrir(chave, buf);
  document.getElementById("cofreForm").addEventListener("submit", async e => {
    e.preventDefault();
    const msg = document.getElementById("cofreMsg"), btn = document.getElementById("cofreBtn");
    btn.disabled = true; msg.textContent = "Conferindo a senha…"; msg.className = "cofre-msg";
    try{
      const r = await fetch(`admin.bin?v=${V}`, {cache:"no-store"});
      if (!r.ok) throw new Error("rede");
      const tudo = new Uint8Array(await r.arrayBuffer());
      const p = await (await fetch(`admin.json?v=${V}`, {cache:"no-store"})).json();
      const k = await derivar(document.getElementById("cofreSenha").value, tudo.slice(5, 21), p.iter);
      let dados;
      try { dados = JSON.parse(new TextDecoder().decode(await abrir(k, tudo.slice(5 + 16)))); }
      catch(_){ msg.textContent = "Senha incorreta. Confira e tente de novo."; msg.className = "cofre-msg erro"; btn.disabled = false; return; }
      chave = k; window.DADOS_MAPA = dados;
      document.getElementById("cofreSenha").value = "";
      document.getElementById("cofre").remove();
      resolver(dados);
    }catch(err){
      msg.textContent = "Não foi possível carregar os dados. Verifique a conexão e tente de novo."; msg.className = "cofre-msg erro"; btn.disabled = false;
    }
  });
})();
