"use strict";
document.getElementById("fSenha").addEventListener("submit", async e => {
  e.preventDefault();
  const msg = document.getElementById("msg"), nova = document.getElementById("nova").value;
  if (nova !== document.getElementById("nova2").value){ msg.textContent = "As duas senhas novas não conferem."; msg.className = "msg erro"; return; }
  const csrf = document.querySelector('meta[name="csrf"]');
  const r = await fetch("/api/conta/senha", {method:"POST", credentials:"same-origin",
    headers:{"Content-Type":"application/json", "X-CSRF-Token":csrf.content},
    body:JSON.stringify({atual:document.getElementById("atual").value, nova})});
  const j = await r.json().catch(() => ({}));
  if (r.ok){ csrf.content = j.csrf; e.target.reset(); msg.textContent = "Senha trocada. Outras sessões abertas desta conta foram encerradas."; msg.className = "msg ok"; }
  else { msg.textContent = j.erro || "Não foi possível trocar a senha."; msg.className = "msg erro"; }
});
