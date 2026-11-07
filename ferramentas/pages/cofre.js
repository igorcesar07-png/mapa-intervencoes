/* Área do administrador no GitHub Pages: abre os dados cifrados com a senha digitada.
   AES-256-GCM, chave derivada da senha com PBKDF2-SHA256. A senha não fica em nenhum arquivo do site:
   sem ela, admin.bin e fotos/x/*.bin são ilegíveis. A chave só existe na memória desta aba. */
(() => {
  let chave = null;
  const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
  async function derivar(senha){
    const p = await (await fetch("admin.json", {cache:"no-store"})).json();
    const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(senha), "PBKDF2", false, ["deriveKey"]);
    return crypto.subtle.deriveKey({name:"PBKDF2", hash:"SHA-256", salt:b64(p.sal), iterations:p.iter}, base, {name:"AES-GCM", length:256}, false, ["decrypt"]);
  }
  const abrir = (k, buf) => crypto.subtle.decrypt({name:"AES-GCM", iv:buf.slice(0, 12)}, k, buf.slice(12));
  let resolver;
  window.DADOS_PRONTO = new Promise(r => resolver = r);
  window.abrirFotoCifrada = async id => {
    try{
      const buf = new Uint8Array(await (await fetch(`fotos/x/${encodeURIComponent(id)}.bin`)).arrayBuffer());
      return URL.createObjectURL(new Blob([await abrir(chave, buf)], {type:"image/jpeg"}));
    }catch(e){ return null; }
  };
  document.getElementById("cofreForm").addEventListener("submit", async e => {
    e.preventDefault();
    const msg = document.getElementById("cofreMsg"), btn = document.getElementById("cofreBtn");
    btn.disabled = true; msg.textContent = "Conferindo a senha…"; msg.className = "cofre-msg";
    try{
      const k = await derivar(document.getElementById("cofreSenha").value);
      const r = await fetch("admin.bin", {cache:"no-store"});
      if (!r.ok) throw new Error("rede");
      const tudo = new Uint8Array(await r.arrayBuffer());
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
