# app/app_online_movil.py
# -------------------------------------------------------------
# App móvil (Windows) con:
# - Cámara en /capturar (o subir imagen)
# - Generación de código (sha1 8-hex)
# - Composición square/fullbleed + PDF 7x5.5"
# - Impresión automática (Sumatra o email) según AUTO_PRINT_MODE
# - Subida a tu web (REMOTE_UPLOAD_URL) con token
# - Respuesta con view_url = VIEW_BASE_URL/<codigo>
# -------------------------------------------------------------

import os, io, json, base64, hashlib, subprocess, threading, time
from pathlib import Path
from datetime import datetime

from flask import Flask, request, jsonify, render_template_string, send_file, redirect, url_for
from waitress import serve
from PIL import Image, ImageOps
from fpdf import FPDF
import requests

# ----------- ENV -----------
PORT                = int(os.getenv("PORT", "5000"))
HOST                = os.getenv("HOST", "0.0.0.0")

# impresión
AUTO_PRINT_MODE     = os.getenv("AUTO_PRINT_MODE", "off").lower()  # sumatra|email|off
PRINT_LAYOUT        = os.getenv("PRINT_LAYOUT", "square").lower()  # square|fullbleed
SUMATRA_PATH        = os.getenv("SUMATRA_PATH", r"C:\Program Files\SumatraPDF\SumatraPDF.exe")

# subida
REMOTE_UPLOAD_URL   = os.getenv("REMOTE_UPLOAD_URL", "").strip()
REMOTE_UPLOAD_TOKEN = os.getenv("REMOTE_UPLOAD_TOKEN", "").strip()
VIEW_BASE_URL       = os.getenv("VIEW_BASE_URL", "").strip()
UPLOAD_JWT_SECRET   = os.getenv("UPLOAD_JWT_SECRET", "change_this_secret").strip()

# ePrint (opcional)
PRINTER_EMAIL       = os.getenv("PRINTER_EMAIL", "").strip()
SENDER_EMAIL        = os.getenv("SENDER_EMAIL", "").strip()
SENDGRID_API_KEY    = os.getenv("SENDGRID_API_KEY", "").strip()

EMAIL_ENABLED       = bool(PRINTER_EMAIL and SENDER_EMAIL and SENDGRID_API_KEY)

# ---- paths ----
BASE_DIR = Path(__file__).resolve().parent.parent  # /app/.. (raíz del repo)
DATA_DIR = BASE_DIR / "data"
UPLOADS  = DATA_DIR / "uploads"
OUT_DIR  = DATA_DIR / "out"
for p in (DATA_DIR, UPLOADS, OUT_DIR):
    p.mkdir(parents=True, exist_ok=True)

# tamaño postal (en píxeles si 300 DPI, 7x5.5 in → 2100x1650)
PX_W, PX_H = 2100, 1650  # landscape

app = Flask(__name__)

# =============================================================
# Utilidades
# =============================================================

def sha1_8(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()[:8]

def open_exif(path: Path) -> Image.Image:
    im = Image.open(path)
    return ImageOps.exif_transpose(im).convert("RGB")

def resize_cover(img: Image.Image, tw: int, th: int) -> Image.Image:
    w, h = img.size
    scale = max(tw / w, th / h)
    nw, nh = int(w * scale), int(h * scale)
    im = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - tw) // 2
    top  = (nh - th) // 2
    return im.crop((left, top, left + tw, top + th))

def compose_image(img_path: Path, layout: str) -> Image.Image:
    """
    Devuelve imagen compuesta 2100x1650:
    - square: recorte cuadrado centrado, encajado y con márgenes
    - fullbleed: a sangre (cover) ocupando todo
    """
    base = Image.new("RGB", (PX_W, PX_H), (255, 255, 255))
    im = open_exif(img_path)

    if layout == "fullbleed":
        comp = resize_cover(im, PX_W, PX_H)
        base.paste(comp, (0, 0))
        return base

    # square
    sq = min(im.width, im.height)
    cx = (im.width - sq) // 2
    cy = (im.height - sq) // 2
    im_sq = im.crop((cx, cy, cx + sq, cy + sq))
    # área destino (márgenes amplios)
    margin_x, margin_y = 180, 120
    side = min(PX_W - 2 * margin_x, PX_H - 2 * margin_y)
    im_sq = im_sq.resize((side, side), Image.LANCZOS)
    x = (PX_W - side) // 2
    y = (PX_H - side) // 2
    base.paste(im_sq, (x, y))
    return base

def save_pdf(img: Image.Image, pdf_path: Path):
    """7x5.5 in landscape, sin márgenes"""
    pdf = FPDF(orientation='L', unit='in', format=(7.0, 5.5))
    pdf.add_page()
    tmp_jpg = pdf_path.with_suffix(".tmp.jpg")
    img.save(tmp_jpg, "JPEG", quality=92)
    pdf.image(str(tmp_jpg), x=0, y=0, w=7.0, h=5.5)
    pdf.output(str(pdf_path))
    try:
        tmp_jpg.unlink()
    except:
        pass

def print_sumatra(pdf_path: Path) -> bool:
    try:
        if not Path(SUMATRA_PATH).exists():
            print("❌ SumatraPDF no encontrado:", SUMATRA_PATH)
            return False
        # -silent para no mostrar UI, -print-to-default envía a impresora predeterminada
        res = subprocess.run([SUMATRA_PATH, "-print-to-default", "-silent", str(pdf_path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok = (res.returncode == 0)
        print(("🖨️ Sumatra OK" if ok else "❌ Sumatra error"), "returncode=", res.returncode)
        return ok
    except Exception as e:
        print("❌ Sumatra excepción:", e)
        return False

def print_email(path: Path, code: str, mime: str) -> bool:
    """HP ePrint por email mediante SendGrid"""
    if not EMAIL_ENABLED:
        print("❌ ePrint no configurado (PRINTER_EMAIL/SENDER_EMAIL/SENDGRID_API_KEY faltan)")
        return False
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
        ts = datetime.now().strftime("%H:%M:%S")
        subject = f"Print {code} {ts}"
        attach_name = f"postal_{code}.{'pdf' if mime=='application/pdf' else 'jpg'}"
        enc = base64.b64encode(path.read_bytes()).decode()
        msg = Mail(from_email=SENDER_EMAIL, to_emails=PRINTER_EMAIL, subject=subject, plain_text_content=subject)
        msg.attachment = Attachment(FileContent(enc), FileName(attach_name), FileType(mime), Disposition("attachment"))
        sg = SendGridAPIClient(api_key=SENDGRID_API_KEY)
        resp = sg.send(msg)
        ok = resp.status_code in (200, 202)
        print(("✅ ePrint OK" if ok else "❌ ePrint ERROR"), "status=", resp.status_code)
        return ok
    except Exception as e:
        print("❌ ePrint excepción:", e)
        return False

def auto_print(comp_img, code: str) -> None:
    """Imprime/manda a ePrint en escala de grises y calidad baja para máxima velocidad."""
    mode = AUTO_PRINT_MODE

    # --- Escala de grises (reduce tamaño y acelera procesamiento) ---
    gray = comp_img.convert("L").convert("RGB")

    # --- Siempre generamos el PDF (grises) por si lo necesitas / Sumatra ---
    pdf_path = OUT_DIR / f"{code}.pdf"
    save_pdf(gray, pdf_path)

    if mode == "sumatra":
        # Si alguna vez imprimes local, el PDF ya viene en grises
        print_sumatra(pdf_path)

    elif mode == "email":
        # ⚡ Preset "rápido": ~1000px lado mayor, JPEG baseline, calidad baja
        jpg_path = OUT_DIR / f"{code}.jpg"

        w, h = gray.size
        MAX = 1000  # lado mayor ~1000 px (muy ligero)
        if max(w, h) > MAX:
            r = MAX / max(w, h)
            gray = gray.resize((int(w * r), int(h * r)), Image.LANCZOS)

        gray.save(
            jpg_path,
            "JPEG",
            quality=60,          # calidad baja (rápido y pequeño)
            optimize=True,
            progressive=False,   # baseline (muchas colas ePrint lo prefieren)
            subsampling="4:2:0"  # archivo más pequeño, sin pérdida relevante
        )

        print_email(jpg_path, code, "image/jpeg")  # adjunta JPG en grises

    else:
        print("ℹ️ AUTO_PRINT_MODE=off (sin impresión)")

def upload_remote(code: str, img_path: Path):
    """Sube el JPG original a tu web principal (/subir_postal) y devuelve la URL de vista si la respuesta la trae."""
    if not (REMOTE_UPLOAD_URL and REMOTE_UPLOAD_TOKEN and VIEW_BASE_URL):
        print("ℹ️ Subida remota desactivada (faltan REMOTE_UPLOAD_URL / TOKEN / VIEW_BASE_URL).")
        return None
    try:
        headers = {"Authorization": f"Bearer {REMOTE_UPLOAD_TOKEN}"}
        files = {"imagen": (f"{code}.jpg", img_path.read_bytes(), "image/jpeg")}
        data  = {"codigo": code}
        r = requests.post(REMOTE_UPLOAD_URL, headers=headers, files=files, data=data, timeout=30)
        print("🌐 Subida remota:", r.status_code, (r.text or "")[:200])

        # Intenta devolver la URL absoluta si la respuesta trae "url" o "view_url"
        try:
            j = r.json()
        except Exception:
            j = {}

        view_url = j.get("view_url") or j.get("url") or ""
        if view_url:
            # Si es relativa, prepende el dominio (VIEW_BASE_URL debe ser dominio raíz)
            if view_url.startswith("/"):
                view_url = f"{VIEW_BASE_URL.rstrip('/')}{view_url}"
            # Si ya es absoluta, la usamos tal cual
        else:
            # Construcción por defecto si el server no devolvió "url"
            view_url = f"{VIEW_BASE_URL.rstrip('/')}/view_image/{code}"

        # Adjunta la URL calculada al objeto response para que /subir pueda leerla
        r._view_url = view_url
        return r
    except Exception as e:
        print("❌ Error subida remota:", e)
        return None

# =============================================================
# Rutas
# =============================================================

@app.get("/favicon.ico")
def favicon():
    return ("", 204)

@app.get("/")
def home():
    HTML = f"""
    <!doctype html><meta charset="utf-8">
    <title>App móvil – cámara e impresión</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>body{{margin:0;background:#000;color:#eee;font-family:system-ui,Segoe UI,Roboto,Arial}}
    .wrap{{max-width:740px;margin:0 auto;padding:22px}}
    .btn{{display:inline-block;padding:12px 16px;border:2px solid #fff;border-radius:10px;color:#fff;text-decoration:none;font-weight:700}}
    .muted{{color:#9aa0a6}}
    </style>
    <div class="wrap">
      <h2>📱 App móvil – cámara + impresión</h2>
      <p class="muted">Modo impresión: <b>{AUTO_PRINT_MODE}</b> · Layout: <b>{PRINT_LAYOUT}</b></p>
      <p><a class="btn" href="/capturar">📷 Abrir cámara</a></p>
      <p class="muted">Tras tomar la foto, se imprimirá automáticamente y se subirá a tu web. Verás el enlace a <code>{VIEW_BASE_URL}/&lt;código&gt;</code>.</p>
    </div>"""
    return HTML

# -------- HTML de captura --------
CAPTURAR_HTML = """
<!doctype html><meta charset="utf-8">
<title>Capturar foto</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{margin:0;background:#000;color:#e6e9ee;font-family:system-ui,Segoe UI,Roboto,Arial}
  .wrap{max-width:820px;margin:0 auto;padding:16px}
  video,canvas,img{width:100%;border-radius:12px;border:1px solid #333;background:#0f1117}
  .row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
  .btn{appearance:none;border:2px solid #fff;background:#000;color:#fff;font-weight:700;border-radius:10px;padding:10px 14px;cursor:pointer}
  .btn.sec{border-color:#3498db;color:#bfe6ff}
  .muted{color:#9aa0a6}
</style>
<div class="wrap">
  <h3>📷 Cámara</h3>
  <video id="v" playsinline autoplay muted></video>
  <div class="row">
    <button class="btn" id="bShoot">Capturar</button>
    <label class="btn sec">Subir imagen
      <input type="file" id="file" accept="image/*" style="display:none">
    </label>
  </div>
  <p class="muted">Consejos: si usas datos móviles, la app comprime la imagen y la escala (máx 1600px).</p>
  <div id="res"></div>
</div>
<script>
const v = document.getElementById('v');
const res = document.getElementById('res');
const bShoot = document.getElementById('bShoot');
const input = document.getElementById('file');

async function initCam(){
  try{
    const st = await navigator.mediaDevices.getUserMedia({
      video:{ facingMode:'environment', width:{ideal:1280}, height:{ideal:720} }, audio:false
    });
    v.srcObject = st;
  }catch(e){ alert('No se pudo abrir la cámara: '+e); }
}

function scaleAndJpeg(videoOrImage){
  // escala al lado mayor 1600px y exporta JPG calidad 0.82
  let w = videoOrImage.videoWidth || videoOrImage.naturalWidth;
  let h = videoOrImage.videoHeight|| videoOrImage.naturalHeight;
  const max = 1600;
  if (Math.max(w,h) > max){ const r = max/Math.max(w,h); w=Math.round(w*r); h=Math.round(h*r); }
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  c.getContext('2d').drawImage(videoOrImage,0,0,w,h);
  return new Promise(resolve=>{
    c.toBlob(b=>resolve(b),'image/jpeg',0.82);
  });
}

async function postBlob(blob){
  const fd = new FormData();
  fd.append('foto', blob, 'foto.jpg');
  res.innerHTML = '<p class="muted">Subiendo…</p>';
  const r = await fetch('/subir',{ method:'POST', body:fd });
  const j = await r.json();
  if(j.ok){
    res.innerHTML = `<p><b>✅ OK</b> · código: <code>${j.code}</code></p>
                     <p><a class="btn" href="${j.view_url}" target="_blank">Ver en la web</a></p>`;
  }else{
    res.innerHTML = `<p style="color:#ff7b7b">❌ Error: ${j.error||'desconocido'}</p>`;
  }
}

bShoot.onclick = async ()=>{
  if(!v.videoWidth){ alert('Cámara aún no inicia'); return; }
  const b = await scaleAndJpeg(v);
  postBlob(b);
};
input.onchange = async ()=>{
  if(!input.files || !input.files[0]) return;
  const img = new Image();
  img.onload = async ()=>{ const b = await scaleAndJpeg(img); postBlob(b); URL.revokeObjectURL(img.src); };
  img.src = URL.createObjectURL(input.files[0]);
};

initCam();
</script>
"""
@app.get("/capturar")
def capturar():
    return CAPTURAR_HTML

@app.post("/subir")
def subir():
    f = request.files.get("foto")
    if not f: return jsonify(ok=False, error="No file"), 400
    raw = f.read()
    if not raw: return jsonify(ok=False, error="Empty"), 400

    code = sha1_8(raw)
    jpg_path = UPLOADS / f"{code}.jpg"
    with open(jpg_path, "wb") as out:
        out.write(raw)

    comp = compose_image(jpg_path, PRINT_LAYOUT)

    # Imprimir (según modo)
    try:
        auto_print(comp, code)
    except Exception as e:
        print("❌ auto_print:", e)

    # Subida a web principal
    r = upload_remote(code, jpg_path)
    if r is not None and r.ok:
        # Prioriza la URL que adjuntamos en upload_remote
        view_url = getattr(r, "_view_url", None)
        if not view_url:
            # Fallback si no adjuntamos
            try:
                j = r.json()
                u = j.get("view_url") or j.get("url") or ""
                view_url = u if u.startswith("http") else f"{VIEW_BASE_URL.rstrip('/')}{u}" if u.startswith("/") else f"{VIEW_BASE_URL.rstrip('/')}/view_image/{code}"
            except Exception:
                view_url = f"{VIEW_BASE_URL.rstrip('/')}/view_image/{code}"
    else:
        view_url = f"{VIEW_BASE_URL.rstrip('/')}/view_image/{code}"

    return jsonify(ok=True, code=code, view_url=view_url)
# --- Silenciar peticiones de iconos (sin archivo) ---
@app.route('/favicon.ico')
@app.route('/apple-touch-icon.png')
@app.route('/apple-touch-icon-precomposed.png')
@app.route('/favicon-16x16.png')
@app.route('/favicon-32x32.png')
def no_favicon():
    return ("", 204)

# =============================================================
# Arranque
# =============================================================
if __name__ == "__main__":
    print(f"✅ App móvil lista en http://{HOST}:{PORT} · modo={AUTO_PRINT_MODE} · layout={PRINT_LAYOUT}")
    serve(app, host=HOST, port=PORT)
