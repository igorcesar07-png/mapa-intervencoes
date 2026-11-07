"""Site do GitHub Pages com dois perfis.
   index.html  visualizador: só os dados que o visualizador do sistema recebe (arquivos públicos), sem exportar.
   admin.html  administrador: dados completos CIFRADOS (AES-256-GCM, chave derivada da senha com PBKDF2-SHA256);
               sem a senha, os arquivos publicados não revelam nada. A senha NÃO fica em nenhum arquivo.
Uso: montar_pages.py <instancia> <saida> <email_adm> <senha_adm> <email_vis> <senha_vis> <senha_do_site>"""
import base64, io, json, os, re, sys, pathlib, shutil
from datetime import datetime
from PIL import Image
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
RAIZ = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))
from mapa import create_app
from mapa.config import Config
INST, OUT = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
ADM, VIS, SENHA_SITE = (sys.argv[3], sys.argv[4]), (sys.argv[5], sys.argv[6]), sys.argv[7]
ITER = 600_000
shutil.rmtree(OUT, ignore_errors=True)
for d in ("fotos", "recursos"): (OUT / d).mkdir(parents=True)

app = create_app(Config(dados_dir=INST), iniciar_worker=False)
def cliente(email, senha):
    c = app.test_client(); pg = c.get("/login")
    t = re.search(rb'name="csrf_token" value="([^"]+)"', pg.data).group(1).decode()
    assert c.post("/login", data={"email": email, "senha": senha, "csrf_token": t}).status_code in (302, 303)
    return c
ca, cv = cliente(*ADM), cliente(*VIS)
rel_a, rel_v = ca.get("/api/relatorio").get_json(), cv.get("/api/relatorio").get_json()
assert rel_a["papel"] == "admin" and rel_v["papel"] == "visualizador"
html_a, html_v = ca.get("/").data.decode(), cv.get("/").data.decode()
publicos = {r["id"] for r in rel_v["registros"]}

def reduzir(dados, lado=1024, q=64):
    im = Image.open(io.BytesIO(dados)).convert("RGB"); im.thumbnail((lado, lado))
    b = io.BytesIO(); im.save(b, "JPEG", quality=q, optimize=True, progressive=True); return b.getvalue()

sal = os.urandom(16)
chave = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=sal, iterations=ITER).derive(SENHA_SITE.encode())
aes = AESGCM(chave)
def cifrar(b):
    iv = os.urandom(12); return iv + aes.encrypt(iv, b, None)

# Poucos arquivos grandes (o GitHub Pages trava ao sincronizar milhares de arquivos):
#   miniaturas dentro dos dados; fotos em pacotes de ~8 MB lidos por trecho (Range).
#   fotos/p*.bin = fotos públicas; fotos/x*.bin = fotos restritas, cada uma cifrada separadamente.
class Pacotes:
    def __init__(self, pref, lim=8 * 1024 * 1024):
        self.pref, self.lim, self.n, self.buf, self.idx = pref, lim, 0, bytearray(), {}
    def add(self, i, dados):
        if self.buf and len(self.buf) + len(dados) > self.lim: self.fechar()
        self.idx[i] = [self.n, len(self.buf), len(dados)]; self.buf += dados
    def fechar(self):
        if self.buf: (OUT / f"fotos/{self.pref}{self.n}.bin").write_bytes(bytes(self.buf)); self.n += 1; self.buf = bytearray()
pub, cif = Pacotes("p"), Pacotes("x")
mini_pub, mini_cif = {}, {}
for r in rel_a["registros"]:
    i = r["id"]
    m = ca.get(f"/api/fotos/{i}/miniatura"); g = ca.get(f"/api/fotos/{i}/imagem")
    uri = "data:image/jpeg;base64," + base64.b64encode(m.data).decode() if m.status_code == 200 else None
    if i in publicos:
        if uri: mini_pub[i] = uri
        if g.status_code == 200: pub.add(i, reduzir(g.data))
    else:
        if uri: mini_cif[i] = uri
        if g.status_code == 200: cif.add(i, cifrar(reduzir(g.data)))
pub.fechar(); cif.fechar()
VERSAO = sal.hex()[:12]
pacote = json.dumps({"relatorio": rel_a, "mini": {**mini_pub, **mini_cif}, "g": pub.idx, "gx": cif.idx, "versao": VERSAO},
                    ensure_ascii=False, separators=(",", ":")).encode()
(OUT / "admin.bin").write_bytes(b"MAPA1" + sal + cifrar(pacote))
(OUT / "admin.json").write_text(json.dumps({"iter": ITER, "sal": base64.b64encode(sal).decode()}))
print("públicas", len(publicos), "restritas", len(rel_a["registros"]) - len(publicos), "pacote", len(pacote))

st = RAIZ / "mapa/static"
js = (st / "relatorio.js").read_text()
def troca(a, b):
    global js
    assert js.count(a) == 1, a[:60]; js = js.replace(a, b)
troca('const CSRF = document.querySelector(\'meta[name="csrf"]\').content;', '')
troca('const mini = id => `/api/fotos/${encodeURIComponent(id)}/miniatura`;',
      'const mini = id => (window.DADOS_MAPA && DADOS_MAPA.mini && DADOS_MAPA.mini[id]) || "";')
troca('const grande = id => `/api/fotos/${encodeURIComponent(id)}/imagem`;', '''const grande = id => `#foto:${id}`;
/* foto em tamanho maior: um trecho de um pacote (fotos/p*.bin públicas, fotos/x*.bin cifradas) */
window.abrirFoto = async id => {
  const D = window.DADOS_MAPA || {}, cifrada = !!(D.gx && D.gx[id]), ref = cifrada ? D.gx[id] : (D.g && D.g[id]);
  if (!ref) return null;
  const [n, ini, tam] = ref;
  try{
    const r = await fetch(`fotos/${cifrada ? "x" : "p"}${n}.bin?v=${D.versao}`, {headers:{Range:`bytes=${ini}-${ini + tam - 1}`}});
    let buf = new Uint8Array(await r.arrayBuffer());
    if (r.status !== 206) buf = buf.slice(ini, ini + tam);
    if (cifrada) buf = new Uint8Array(await window.decifrar(buf));
    return URL.createObjectURL(new Blob([buf], {type:"image/jpeg"}));
  }catch(e){ return null; }
};''')
troca('''  const r = await fetch("/api/relatorio", {credentials:"same-origin", cache:"no-store"});
  if (r.status === 401){ location.href = "/login"; return; }
  if (!r.ok){ toast("Não foi possível carregar os dados. Recarregue a página."); return; }
  const j = await r.json();''', '''  const j = JSON.parse(JSON.stringify((await (window.DADOS_PRONTO || Promise.resolve(window.DADOS_MAPA))).relatorio));''')
troca('''  if (img){ $("#lb img").src = img.dataset.full; $("#lb").classList.add("on"); return; }''',
      '''  if (img){
    const f = img.dataset.full;
    if (f.startsWith("#foto:")){ $("#lb img").src = img.src; $("#lb").classList.add("on"); window.abrirFoto(f.slice(6)).then(u => { if (u) $("#lb img").src = u; }); return; }
    $("#lb img").src = f; $("#lb").classList.add("on"); return;
  }''')
troca('''if ($("#btnCsv")) $("#btnCsv").onclick = () => {
  const f = filtros(), q = new URLSearchParams(Object.entries({...f, blocos:f.blocos.join(",")}).filter(([,v]) => v));
  location.href = "/api/exportar.csv" + (q.toString() ? "?" + q : "");
};''', '''if ($("#btnCsv")) $("#btnCsv").onclick = () => {
  // mesmo CSV do sistema (administrador): todas as fotos do filtro, com situação e motivo
  const num = v => v == null ? "" : String(v).replace(".", ",");
  const f = filtros(), gp = new Map(S.grupos.map(g => [g.id, g]));
  const cab = ["arquivo","data","fonte_data","latitude","longitude","bloco","estaca","km","rodovia","local","sentido","lote","dist_eixo_m","classe","situacao","inconsistencia","motivo","duplicata","buraco"];
  const linhas = [cab.join(";")];
  const regs = S.todos.filter(r => passa(r, f)).sort((a, b) => (a.data||"9999").localeCompare(b.data||"9999") || a.arquivo.localeCompare(b.arquivo));
  for (const r of regs){
    const d = S.derivado.get(r.id) || {}, rod = (S.tracado.blocos[d.bloco]||{}).rodovia || r.rodovia || "";
    const sit = r.lat == null ? "sem coordenada" : d.dup ? "duplicata" : r.classe === "outro" ? "não relacionada" : d.anomalia ? "inconsistência"
      : d.grupo && gp.get(d.grupo) && !gp.get(d.grupo).tapado ? "buraco sem foto de tapado" : d.rep ? "válida" : "foto extra do mesmo buraco";
    linhas.push([r.arquivo, (r.data||"").replace("T"," "), r.data_fonte||"", num(r.lat), num(r.lon), d.bloco||"",
      d.est != null ? fmtEst(d.est) : "", d.est != null ? num((d.est/1000).toFixed(3)) : "", rod, r.local||"", r.sentido||"", r.lote||"",
      d.dist != null ? Math.round(d.dist) : "", r.classe||"", sit, d.anomalia ? "sim" : "não", d.motivo||"", d.dup ? "sim" : "", d.grupo||""]
      .map(v => /[;"\\n]/.test(String(v)) ? '"' + String(v).replace(/"/g,'""') + '"' : v).join(";"));
  }
  const url = URL.createObjectURL(new Blob(["\\ufeff" + linhas.join("\\r\\n")], {type:"text/csv;charset=utf-8"}));
  const a = Object.assign(document.createElement("a"), {href:url, download:"mapa-intervencoes.csv"}); document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
};''')
troca('async function verificarVersao(){', 'async function verificarVersao(){ return;')
troca('setInterval(verificarVersao, ADMIN ? 8000 : 30000);\n', '')
assert "/api/" not in js.replace('"/api/versao"', "")
(OUT / "recursos/relatorio.js").write_text(js)

quando = datetime.now().strftime("%d/%m/%Y")
def pagina(html, admin):
    html = re.sub(r'<meta name="csrf"[^>]*>\n', '', html)
    html = re.sub(r'\s*<div class="conta">.*?</div>\n', '\n', html, count=1, flags=re.S)
    html = html.replace('<link rel="stylesheet" href="/recursos/vendor/leaflet/leaflet.css', '<link rel="stylesheet" href="recursos/leaflet.css')
    html = re.sub(r'href="/recursos/(relatorio|web)\.css\?v=[^"]*"', r'href="recursos/\1.css"', html)
    html = re.sub(r'href="recursos/leaflet\.css\?v=[^"]*"', 'href="recursos/leaflet.css"', html)
    html = re.sub(r'<script src="/recursos/vendor/leaflet/leaflet\.js[^"]*"></script>', '<script src="recursos/leaflet.js"></script>', html)
    html = re.sub(r'<script src="/recursos/vendor/leaflet-heat\.js[^"]*"></script>', '<script src="recursos/leaflet-heat.js"></script>', html)
    if admin:
        html = re.sub(r'<a class="btn pri" href="/admin">Enviar ZIP de fotos</a>',
                      '<span class="hint" style="margin:0">Enviar fotos: só no sistema com login (servidor). Esta página é uma cópia de leitura.</span>', html)
        perfil = ('<div class="conta"><span class="papel adm">Administrador</span><span class="quem">dados completos</span>'
                  '<a href="./">Sair</a></div>')
        html = re.sub(r'(<div class="head" id="cabecalho">)', r'\1\n      ' + perfil, html, count=1)
        html = re.sub(r'<script src="/recursos/relatorio\.js[^"]*"></script>',
                      f'<script src="recursos/cofre.js?v={VERSAO}" data-v="{VERSAO}"></script>\n<script src="recursos/relatorio.js?v={VERSAO}"></script>', html)
        assert '<body data-papel="admin">' in html
        html = html.replace('<body data-papel="admin">', '''<body data-papel="admin">
<div class="cofre" id="cofre" role="dialog" aria-modal="true" aria-labelledby="cofreTit">
  <form class="cofre-caixa" id="cofreForm" autocomplete="off">
    <h1 id="cofreTit">Área do administrador</h1>
    <p>Os dados completos (inconsistências, todas as fotos e exportação) ficam cifrados neste site e só abrem com a senha do administrador.</p>
    <label for="cofreSenha">Senha</label>
    <input type="password" id="cofreSenha" required autofocus autocomplete="current-password">
    <p class="cofre-msg" id="cofreMsg" role="status"></p>
    <div class="cofre-acoes"><a href="./">Voltar ao mapa público</a><button class="btn pri" type="submit" id="cofreBtn">Entrar</button></div>
  </form>
</div>''', 1)
    else:
        perfil = ('<div class="conta"><span class="papel">Visualizador</span><span class="quem">consulta pública</span>'
                  '<a href="admin.html">Entrar como administrador</a></div>')
        html = re.sub(r'(<div class="head" id="cabecalho">)', r'\1\n      ' + perfil, html, count=1)
        html = re.sub(r'<script src="/recursos/relatorio\.js[^"]*"></script>', f'<script src="dados.js?v={VERSAO}"></script>\n<script src="recursos/relatorio.js?v={VERSAO}"></script>', html)
    html = re.sub(r'(<div class="foot"[^>]*>)', r'\1Retrato dos dados de ' + quando + '. ', html, count=1)
    left = re.findall(r'(?:href|src)="/[^"]*"', html)
    assert not left, left
    return html
(OUT / "index.html").write_text(pagina(html_v, False))
(OUT / "admin.html").write_text(pagina(html_a, True))
(OUT / "dados.js").write_text("window.DADOS_MAPA = " + json.dumps({"relatorio": rel_v, "mini": mini_pub, "g": pub.idx, "versao": VERSAO},
                                                                   ensure_ascii=False, separators=(",", ":")) + ";")
shutil.copy(st / "vendor/leaflet/leaflet.css", OUT / "recursos/leaflet.css")
shutil.copytree(st / "vendor/leaflet/images", OUT / "recursos/images")
shutil.copy(st / "vendor/leaflet/leaflet.js", OUT / "recursos/leaflet.js")
shutil.copy(st / "vendor/leaflet-heat.js", OUT / "recursos/leaflet-heat.js")
for n in ("relatorio.css", "web.css"): shutil.copy(st / n, OUT / "recursos" / n)
shutil.copy(pathlib.Path(sys.argv[0]).parent / "cofre.js", OUT / "recursos/cofre.js")
with open(OUT / "recursos/web.css", "a") as f: f.write((pathlib.Path(sys.argv[0]).parent / "cofre.css").read_text())
(OUT / ".nojekyll").write_text("")
(OUT / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
print("ok", quando)
