# ===============================
# app_online_movil.py — Cámara + Código + AutoPrint + Subida a tu web (Render/PC)
# ===============================
import os, io, json, base64, hashlib, time, threading
from pathlib import Path
from datetime import datetime, timedelta
from typing import List
from flask import Flask, request, jsonify, send_file, render_template_string, redirect, url_for, session
from PIL import Image, ImageOps, ImageDraw, ImageFont
from fpdf import FPDF
import requests, jwt

# ---------- Rutas y configuración ----------
BASE = Path(__file__).resolve().parent
DATA = Path(os.getenv("DATA_DIR", BASE.parent / "data"))
UPL  = DATA / "uploads"
PDFS = DATA / "pdfs"
for d in (DATA, UPL, PDFS): d.mkdir(parents=True, exist_ok=True)

HOST = os.getenv("HOST","0.0.0.0")
PORT = int(os.getenv("PORT","5000"))

# Layout de impresión: square | fullbleed
PRINT_LAYOUT = os.getenv("PRINT_LAYOUT","square").lower()

# Auto-impresión
AUTO_PRINT_MODE  = os.getenv("AUTO_PRINT_MODE","off").lower()   # email | sumatra | off
PRINTER_EMAIL    = os.getenv("PRINTER_EMAIL","").strip()
SENDER_EMAIL     = os.getenv("SENDER_EMAIL","").strip()
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY","").strip()
EMAIL_ENABLED    = bool(PRINTER_EMAIL and SENDER_EMAIL and SENDGRID_API_KEY)
SUMATRA_PATH     = os.getenv("SUMATRA_PATH", r"C:\Program Files\SumatraPDF\SumatraPDF.exe")

# Subida a tu web principal (Render grande)
REMOTE_UPLOAD_URL   = os.getenv("REMOTE_UPLOAD_URL","").strip()      # ej: https://www.postcardporto.com/subir_postal
REMOTE_UPLOAD_TOKEN = os.getenv("REMOTE_UPLOAD_TOKEN","").strip()    # = UPLOAD_TOKEN de tu web
VIEW_BASE_URL       = os.getenv("VIEW_BASE_URL","").strip()          # ej: https://www.postcardporto.com/view_image

# Tickets para subida web (browser)
UPLOAD_JWT_SECRET = os.getenv("UPLOAD_JWT_SECRET","ul_secret_cambia_esto")

# Lienzo 7×5.5" @ 300dpi
W,H = 2100,1650

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY","movil_public_secret")

# ---------- Helpers de imagen ----------
def _sha8(b: bytes) -> str: return hashlib.sha1(b).hexdigest()[:8]
@app.get("/favicon.ico")
def favicon():
    # Respuesta vacía (204) o sirve un icono desde /static
    return ("", 204)
    # o, si prefieres un icono:
    # return send_file(BASE / "static" / "favicon.ico", mimetype="image/x-icon")

def open_image(path: Path) -> Image.Image:
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)  # corrige EXIF
    return im.convert("RGB")

def resize_cover(img: Image.Image, tw:int, th:int) -> Image.Image:
    w,h = img.size
    scale = max(tw/w, th/h)
    nw, nh = int(w*scale), int(h*scale)
    img = img.resize((nw,nh), Image.LANCZOS)
    left = max(0,(nw-tw)//2); top = max(0,(nh-th)//2)
    return img.crop((left, top, left+tw, top+th))

def compose_fullbleed(code:str, img_path:Path) -> Image.Image:
    base = Image.new("RGB",(W,H),(255,255,255))
    user = open_image(img_path)
    user = resize_cover(user, W, H)
    base.paste(user, (0,0))
    d = ImageDraw.Draw(base)
    try: font = ImageFont.truetype("DejaVuSans-Bold.ttf", 72)
    except: font = ImageFont.load_default()
    tw = d.textlength(code, font=font); pad=30
    d.rectangle([W-int(tw)-pad*2, H-130, W-30, H-30], fill=(255,255,255,230))
    d.text((W-int(tw)-pad*1.5, H-120), code, fill=(20,20,20), font=font)
    return base

def compose_square(code:str, img_path:Path, margin:int=120, anchor:str="center") -> Image.Image:
    base = Image.new("RGB",(W,H),(255,255,255))
    side = min(W,H) - 2*margin
    x,y = (W-side)//2, (H-side)//2
    im = open_image(img_path)
    sq = min(im.width, im.height)
    if anchor=="top":    left, top = (im.width-sq)//2, 0
    elif anchor=="bottom": left, top = (im.width-sq)//2, im.height-sq
    else:                left, top = (im.width-sq)//2, (im.height-sq)//2
    crop = im.crop((left, top, left+sq, top+sq)).resize((side,side), Image.LANCZOS)
    base.paste(crop, (x,y))
    d = ImageDraw.Draw(base)
    d.rectangle([x,y,x+side,y+side], outline=(230,230,230), width=6)
    try: font = ImageFont.truetype("DejaVuSans-Bold.ttf", 46)
    except: font = ImageFont.load_default()
    tw = d.textlength(code, font=font); pad=24
    d.rectangle([W-int(tw)-pad*2, H-92, W-30, H-30], fill=(255,255,255,210))
    d.text((W-int(tw)-pad*1.5, H-82), code, fill=(20,20,20), font=font)
    return base

def save_pdf(img:Image.Image, pdf_path:Path):
    pdf = FPDF(orientation='L', unit='in', format=(7.0,5.5))
    pdf.add_page()
    tmp = pdf_path.with_suffix(".tmp.jpg")
    img.save(tmp, quality=92)
    pdf.image(str(tmp), x=0, y=0, w=7.0, h=5.5)
    pdf.output(pdf_path)
    try: tmp.unlink()
    except: pass

# ---------- Impresión ----------
def send_eprint(pdf_path:Path, code:str) -> bool:
    if not EMAIL_ENABLED:
        print("ℹ️ ePrint no configurado"); return False
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
        subject = f"Print {code} {datetime.utcnow().strftime('%H:%M:%S')}"
        enc = base64.b64encode(pdf_path.read_bytes()).decode()
        msg = Mail(from_email=SENDER_EMAIL, to_emails=PRINTER_EMAIL, subject=subject, plain_text_content=subject)
        msg.attachment = Attachment(FileContent(enc), FileName(f"postal_{code}.pdf"), FileType("application/pdf"), Disposition("attachment"))
        sg = SendGridAPIClient(api_key=SENDGRID_API_KEY)
        resp = sg.send(msg)
        ok = resp.status_code in (200,202)
        print(("✅ ePrint OK" if ok else "❌ ePrint ERROR"), "status=", resp.status_code)
        return ok
    except Exception as e:
        print("❌ ePrint excepción:", e); return False

def print_sumatra(pdf_path:Path) -> bool:
    try:
        if not Path(SUMATRA_PATH).exists(): 
            print("❌ SUMATRA_PATH no existe:", SUMATRA_PATH); return False
        import subprocess
        subprocess.run([SUMATRA_PATH,"-print-to-default","-print-settings","noscale","-silent",str(pdf_path)], check=True)
        print("✅ Impreso con Sumatra:", pdf_path.name); return True
    except Exception as e:
        print("❌ Sumatra error:", e); return False

def auto_print(pdf_path:Path, code:str):
    mode = (AUTO_PRINT_MODE or "off").lower()
    if mode == "email":  send_eprint(pdf_path, code)
    elif mode == "sumatra": print_sumatra(pdf_path)
    else: print("ℹ️ AUTO_PRINT_MODE=off")

# ---------- Subida a web principal ----------
def upload_remote(code:str, img_path:Path):
    if not (REMOTE_UPLOAD_URL and REMOTE_UPLOAD_TOKEN): return
    try:
        # recomprime a JPG 85 para robustez
        try:
            im = Image.open(img_path).convert("RGB")
            buf = io.BytesIO(); im.save(buf, "JPEG", quality=85, optimize=True)
            data_bytes = buf.getvalue()
        except Exception:
            data_bytes = img_path.read_bytes()
        headers = {"Authorization": f"Bearer {REMOTE_UPLOAD_TOKEN}"}
        for attempt in range(1,4):
            try:
                files = {"imagen": (f"{code}.jpg", data_bytes, "image/jpeg")}
                data  = {"codigo": code, "source":"browser"}
                r = requests.post(REMOTE_UPLOAD_URL, headers=headers, files=files, data=data, timeout=60)
                print("🌐 Subida remota:", r.status_code, (r.text or "")[:200])
                if r.ok: break
            except Exception as e:
                print(f"❌ Subida remota intento {attempt}:", e)
            time.sleep(2*attempt)
    except Exception as e:
        print("❌ Error subida remota:", e)

# ---------- UI ----------
INDEX_HTML = """<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Postales – Captura y AutoPrint</title>
<style>
:root{--card:#15171e;--muted:#9aa0a6;--text:#e6e9ee;--border:rgba(255,255,255,.08)}
*{box-sizing:border-box}body{margin:0;background:#000;color:var(--text);font-family:system-ui,Segoe UI,Roboto,Arial}
.wrap{max-width:980px;margin:0 auto;padding:18px}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:14px;box-shadow:0 8px 24px rgba(0,0,0,.35);margin:10px 0}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.btn{padding:12px 14px;border-radius:10px;border:1px solid #fff;background:#000;color:#fff;font-weight:700;cursor:pointer}
.input{padding:10px;border-radius:10px;border:1px solid var(--border);background:#0f1117;color:#e6e9ee;min-width:260px}
.preview{width:100%;border-radius:14px;border:1px solid var(--border);background:#0f1117}
a{color:#7acaff}
</style>
<div class="wrap">
  <h2>Postales – Captura y AutoPrint</h2>
  <div class="card">
    <p><b>Usar cámara del teléfono</b>: <a href="/capturar">/capturar</a></p>
    <h3>📤 Subir archivo</h3>
    <form id="uploadForm" action="/upload" method="post" enctype="multipart/form-data" class="row">
      <input type="file" name="foto" accept="image/*" class="input" required>
      <button class="btn" type="submit">Subir</button>
    </form>
    <small class="muted">Tras subir: genera código, crea PDF 7×5.5", <b>imprime</b> (modo: """+AUTO_PRINT_MODE+""") y sube a tu web si está configurado.</small>
  </div>

  <div class="card">
    <label class="muted">Código</label>
    <input id="code" class="input" placeholder="—" readonly>
    <div class="row" style="margin-top:8px">
      <button id="bPDF" class="btn">⬇ PDF</button>
      <button id="bPrint" class="btn">🖨 Reimprimir</button>
      <a id="viewlink" href="#" target="_blank" style="display:none">➡️ Ver en tu web</a>
    </div>
  </div>

  <img id="prev" class="preview" alt="Previsualización">
</div>
<script>
async function refresh(){
  const j = await (await fetch('/last')).json();
  document.getElementById('code').value = j.code||'';
  if(j.view_url){ const a=document.getElementById('viewlink'); a.href=j.view_url; a.style.display='inline-block'; }
  const r = await fetch('/preview'); if(r.ok){ document.getElementById('prev').src = URL.createObjectURL(await r.blob()); }
}
document.getElementById('bPDF').onclick  = ()=> location='/render_pdf';
document.getElementById('bPrint').onclick = async()=>{ const r=await fetch('/imprimir'); const j=await r.json(); alert(j.ok?'Imprimiendo…':'Error: '+(j.error||'')); };
refresh();
</script>
"""

CAPTURAR_HTML = """<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Capturar y subir</title>
<style>
body{margin:0;background:#000;color:#e6e9ee;font-family:system-ui,Segoe UI,Roboto,Arial}
.wrap{max-width:520px;margin:0 auto;padding:16px}
.card{background:#15171e;border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:12px;margin:10px 0}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
button{padding:12px 14px;border-radius:10px;border:1px solid #fff;background:#000;color:#fff;font-weight:700}
input{padding:10px;border-radius:10px;border:1px solid rgba(255,255,255,.14);background:#0f1117;color:#e6e9ee;width:100%}
video,canvas{width:100%;max-height:360px;background:#000;border-radius:12px;border:1px solid rgba(255,255,255,.08)}
.muted{color:#9aa0a6}.ok{color:#22c55e}
</style>
<div class=wrap>
  <h2>📷 Capturar y subir</h2>
  <div class=card>
    <div class=row>
      <button id=start>🎥 Activar cámara</button>
      <button id=shot disabled>📸 Capturar y subir</button>
      <span class=muted>o seleccionar archivo:</span>
      <input type=file id=file accept="image/*">
    </div>
    <video id=v playsinline muted></video>
    <canvas id=c style="display:none"></canvas>
    <p id=msg class=muted></p>
  </div>

  <div class=card>
    <label>Código</label>
    <input id=code placeholder="autogenerado (opcional)">
    <div class=muted>Si lo dejas vacío se genera automático.</div>
  </div>

  <div class=card>
    <a id=lnk class=ok target=_blank style="display:none">➡️ Abrir en /view_image</a>
  </div>
</div>
<script>
const v=document.getElementById('v'), c=document.getElementById('c'), start=document.getElementById('start'),
      shot=document.getElementById('shot'), file=document.getElementById('file'),
      msg=document.getElementById('msg'), code=document.getElementById('code'), lnk=document.getElementById('lnk');

async function ticket(){ const r=await fetch('/upload_ticket',{method:'POST'}); const j=await r.json(); if(!j.ticket) throw 'no ticket'; return j.ticket; }
async function postBlob(blob){
  const tk = await ticket();
  const fd = new FormData();
  if(code.value) fd.append('codigo', code.value.trim());
  fd.append('foto', blob, 'cam.jpg');              // usamos /upload directo (más simple)
  const r = await fetch('/upload?tk='+encodeURIComponent(tk), {method:'POST', body: fd});
  const j = await r.json().catch(()=>null);
  if(j && j.status==='ok' && j.view_url){ lnk.href=j.view_url; lnk.style.display='inline-block'; lnk.textContent='➡️ Ver '+j.codigo; }
  msg.textContent = '✅ Subido '+(j && j.codigo ? j.codigo : '');
}

start.onclick = async()=>{
  try{
    const s = await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'}, audio:false});
    v.srcObject=s; await v.play(); shot.disabled=false; msg.textContent='Cámara lista';
  }catch(e){ msg.textContent='No se pudo activar la cámara: '+e; }
};

shot.onclick = async()=>{
  const w=v.videoWidth, h=v.videoHeight; if(!w||!h){ msg.textContent='Cámara no lista'; return; }
  c.width=w; c.height=h; c.getContext('2d').drawImage(v,0,0,w,h);
  c.toBlob(b=>postBlob(b).catch(e=>msg.textContent='❌ '+e),'image/jpeg',0.92);
};

file.onchange = async(e)=>{
  const f=e.target.files[0]; if(!f) return;
  postBlob(f).catch(e=>msg.textContent='❌ '+e);
};
</script>
"""

# ---------- Rutas ----------
@app.get("/")
def index(): return render_template_string(INDEX_HTML)

@app.get("/capturar")
def capturar(): return render_template_string(CAPTURAR_HTML)

@app.post("/upload_ticket")
def upload_ticket():
    payload = {"iss":"postcardporto","iat":datetime.utcnow(),"exp":datetime.utcnow()+timedelta(minutes=5)}
    token = jwt.encode(payload, UPLOAD_JWT_SECRET, algorithm="HS256")
    return jsonify(ticket=token)

@app.get("/last")
def last():
    return jsonify(code=session.get("last_code",""),
                   view_url=session.get("last_view_url",""))

@app.post("/upload")
def upload():
    # opcional: valida ticket si llegó como query (desde /capturar)
    tk = request.args.get("tk","").strip()
    if tk:
        try: jwt.decode(tk, UPLOAD_JWT_SECRET, algorithms=["HS256"])
        except Exception as e: return jsonify(status="error", error=f"invalid_ticket:{e}"), 401

    f = request.files.get("foto")
    if not f: return redirect(url_for("index"))
    raw = f.read()
    if not raw: return redirect(url_for("index"))

    code = _sha8(raw)
    session["last_code"] = code
    img_path = UPL / f"{code}.jpg"
    img_path.write_bytes(raw)
    session["last_image"] = str(img_path)

    # Componer y PDF
    comp = compose_square(code, img_path) if PRINT_LAYOUT=="square" else compose_fullbleed(code, img_path)
    pdf_path = PDFS / f"{code}.pdf"
    comp.save(PDFS / f"{code}_print.jpg", "JPEG", quality=92)
    save_pdf(comp, pdf_path)

    # Auto-impresión
    try: auto_print(pdf_path, code)
    except Exception as e: print("Impresión fallo:", e)

    # Subida a tu web (si está configurado)
    view_url = ""
    if REMOTE_UPLOAD_URL and REMOTE_UPLOAD_TOKEN:
        threading.Thread(target=upload_remote, args=(code, img_path), daemon=True).start()
        if VIEW_BASE_URL:
            view_url = f"{VIEW_BASE_URL.rstrip('/')}/{code}"
    session["last_view_url"] = view_url

    # Si la subida fue desde XHR de /capturar, devuelve JSON
    if tk:
        return jsonify(status="ok", codigo=code, view_url=view_url or "")

    return redirect(url_for("index"))

@app.get("/preview")
def preview():
    ip = session.get("last_image"); code = session.get("last_code","")
    if not ip or not Path(ip).exists():
        blank = Image.new("RGB",(W,H),(30,34,42))
        b = io.BytesIO(); blank.save(b,"JPEG",quality=85); b.seek(0)
        return send_file(b, mimetype="image/jpeg")
    comp = compose_square(code, Path(ip)) if PRINT_LAYOUT=="square" else compose_fullbleed(code, Path(ip))
    b = io.BytesIO(); comp.save(b,"JPEG",quality=85); b.seek(0)
    return send_file(b, mimetype="image/jpeg")

@app.get("/render_pdf")
def render_pdf():
    ip = session.get("last_image"); code = session.get("last_code","PDF")
    if not ip or not Path(ip).exists(): return "Sin imagen", 400
    comp = compose_square(code, Path(ip)) if PRINT_LAYOUT=="square" else compose_fullbleed(code, Path(ip))
    out = PDFS / f"{code}.pdf"; save_pdf(comp, out)
    return send_file(out, mimetype="application/pdf", as_attachment=True, download_name=out.name)

@app.get("/imprimir")
def imprimir():
    try:
        code = session.get("last_code","PRINT")
        ip   = session.get("last_image")
        if not ip or not Path(ip).exists(): return jsonify(ok=False, error="Sin imagen")
        comp = compose_square(code, Path(ip)) if PRINT_LAYOUT=="square" else compose_fullbleed(code, Path(ip))
        out = PDFS / f"{code}.pdf"; save_pdf(comp, out)
        auto_print(out, code)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

# (opcional) vista local para pruebas
@app.get("/view_image/<code>")
def view_local(code):
    code = (code or "").strip().lower()
    orig = UPL / f"{code}.jpg"
    comp = PDFS / f"{code}_print.jpg"
    if not orig.exists() and not comp.exists():
        return f"<h3 style='color:#fff;background:#000;padding:24px'>❌ Código {code} no encontrado</h3>", 404
    html = f"""<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>
    <div style='background:#000;color:#fff;font-family:Arial;padding:18px;max-width:900px;margin:auto'>
      <h2>📬 Código {code}</h2>
      {'<img src=\"/local_img/'+code+'?t=1\" style=\"width:100%;border-radius:12px\">' if orig.exists() else ''}
      {'<h3>Postal compuesta</h3><img src=\"/local_comp/'+code+'?t=1\" style=\"width:100%;border-radius:12px\">' if comp.exists() else ''}
    </div>"""
    return render_template_string(html)

@app.get("/local_img/<code>")
def local_img(code):
    p = UPL / f"{code}.jpg"
    if not p.exists(): return "404", 404
    return send_file(p, mimetype="image/jpeg")

@app.get("/local_comp/<code>")
def local_comp(code):
    p = PDFS / f"{code}_print.jpg"
    if not p.exists(): return "404", 404
    return send_file(p, mimetype="image/jpeg")

# ---------- Arranque ----------
if __name__ == "__main__":
    from waitress import serve
    print(f"🚀 Sirviendo app_online_movil en http://{HOST}:{PORT}  DATA={DATA}")
    serve(app, host=HOST, port=PORT)
