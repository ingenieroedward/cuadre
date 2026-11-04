import asyncio
import csv
import hashlib
import io
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from fastapi.responses import Response
from webauthn import (generate_authentication_options, generate_registration_options, options_to_json,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

import avisos
import icons
import lector

BASE = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR", "./data"))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "finanzas.db"
MAX_MB = 25


def _secreto():
    """La llave de sesión vive en el volumen: sobrevive reinicios sin depender de variables de entorno."""
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    f = DATA / "secret.key"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
    return f.read_text().strip()


PEM, PUBLICA = avisos.claves(DATA)


@asynccontextmanager
async def lifespan(app):
    async def ciclo():
        while True:
            try:
                await asyncio.to_thread(avisos.revisar, db, PEM)
            except Exception as e:
                print("avisos:", e, flush=True)
            await asyncio.sleep(900)
    t = asyncio.create_task(ciclo())
    yield
    t.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=_secreto(), same_site="lax",
                   https_only=os.environ.get("HTTPS_ONLY", "0") == "1", max_age=60 * 60 * 24 * 30)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
tpl = Jinja2Templates(directory=str(BASE / "templates"))
tpl.env.filters["cop"] = avisos.cop
tpl.env.filters["ico"] = icons.cat
tpl.env.filters["color"] = icons.color
tpl.env.filters["inicial"] = lambda t: (t or "?").strip()[:1].upper()
MES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
DIA_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def mes_txt(mes):
    a, m_ = mes.split("-")
    return f"{MES_ES[int(m_) - 1].capitalize()} {a}"


def mover_mes(mes, d):
    a, m_ = int(mes[:4]), int(mes[5:7]) - 1 + d
    return f"{a + m_ // 12:04d}-{m_ % 12 + 1:02d}"


def fecha_larga(iso):
    d = date.fromisoformat(iso)
    return f"{DIA_ES[d.weekday()]} {d.day} {MES_ES[d.month - 1][:3]}"
tpl.env.globals["ico"] = icons.svg
tpl.env.globals["marca"] = icons.marca
CATEGORIAS = list(lector.CATEGORIAS_BASE) + ["Transferencias recibidas", "Transferencias enviadas", "Traslado propio",
                                              "Pago de tarjeta", "Reversado", "Devoluciones", "Arriendo", "Ocio", "Otros"]
EXC = avisos.EXC
BANCOS = ["Nu", "Bancolombia", "Davivienda", "BBVA", "Nequi", "Otro"]


# ---------- DB ----------
def db():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return f"{salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 200_000).hex()}"


def check_pw(pw, stored):
    return secrets.compare_digest(hash_pw(pw, stored.split("$")[0]), stored)


def init():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, nombre TEXT, pw TEXT, admin INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS movs(id INTEGER PRIMARY KEY, user_id INTEGER, banco TEXT, fecha TEXT, descripcion TEXT,
        monto REAL, categoria TEXT, origen TEXT, hash TEXT UNIQUE, archivo TEXT);
    CREATE TABLE IF NOT EXISTS reglas(id INTEGER PRIMARY KEY, kw TEXT UNIQUE, categoria TEXT);
    CREATE INDEX IF NOT EXISTS ix_f ON movs(fecha);
    CREATE TABLE IF NOT EXISTS push_subs(id INTEGER PRIMARY KEY, user_id INTEGER, endpoint TEXT UNIQUE, p256dh TEXT, auth TEXT);
    CREATE TABLE IF NOT EXISTS recordatorios(id INTEGER PRIMARY KEY, titulo TEXT, dia INTEGER, antes INTEGER DEFAULT 0,
        para INTEGER DEFAULT 0, activo INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS pagos_tarjeta(id INTEGER PRIMARY KEY, user_id INTEGER, banco TEXT, fecha_limite TEXT,
        pago_minimo REAL, a_pagar REAL, deuda_total REAL, pagado INTEGER DEFAULT 0, UNIQUE(user_id,banco,fecha_limite));
    CREATE TABLE IF NOT EXISTS presupuestos(id INTEGER PRIMARY KEY, categoria TEXT UNIQUE, limite REAL);
    CREATE TABLE IF NOT EXISTS avisos_log(clave TEXT PRIMARY KEY, creado TEXT);
    CREATE TABLE IF NOT EXISTS config(clave TEXT PRIMARY KEY, valor TEXT);
    CREATE TABLE IF NOT EXISTS passkeys(id INTEGER PRIMARY KEY, user_id INTEGER, credential_id TEXT UNIQUE, public_key TEXT,
        sign_count INTEGER DEFAULT 0, nombre TEXT, creado TEXT);
    """)
    # Compatibilidad: si defines ADMIN_PASSWORD se crea el primer admin al arrancar.
    # Si no, la primera visita muestra la pantalla "Crear administrador" (recomendado).
    if not c.execute("SELECT 1 FROM users").fetchone() and os.environ.get("ADMIN_PASSWORD"):
        u = os.environ.get("ADMIN_USER", "admin").strip().lower()
        c.execute("INSERT INTO users(username,nombre,pw,admin) VALUES(?,?,?,1)",
                  (u, u.capitalize(), hash_pw(os.environ["ADMIN_PASSWORD"])))
        c.commit()
    c.close()


init()


def hay_usuarios():
    c = db()
    r = c.execute("SELECT 1 FROM users").fetchone()
    c.close()
    return bool(r)


# ---------- Auth ----------
class NoAuth(Exception):
    pass


@app.exception_handler(NoAuth)
async def _noauth(request, exc):
    return RedirectResponse("/login" if hay_usuarios() else "/configurar", 303)


def user(request: Request):
    uid = request.session.get("uid")
    if not uid:
        raise NoAuth()
    c = db()
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    c.close()
    if not u:
        raise NoAuth()
    return u


def render(request, name, **ctx):
    ctx.setdefault("u", None)
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("err", request.query_params.get("err"))
    return tpl.TemplateResponse(request, name, ctx)


def volver(request, destino, msg="", err=""):
    q = (f"?msg={msg}" if msg else f"?err={err}" if err else "")
    return RedirectResponse(destino + q.replace(" ", "%20"), 303)


INTENTOS = {}   # (ip, usuario) -> [timestamps de fallos]


def bloqueado(clave):
    t = [x for x in INTENTOS.get(clave, []) if time.time() - x < 600]
    INTENTOS[clave] = t
    return len(t) >= 6


@app.get("/configurar")
def configurar_form(request: Request):
    if hay_usuarios():
        return RedirectResponse("/login", 303)
    return render(request, "configurar.html")


@app.post("/configurar")
def configurar(request: Request, nombre: str = Form(...), username: str = Form(...), password: str = Form(...),
               password2: str = Form(...)):
    if hay_usuarios():
        return RedirectResponse("/login", 303)
    if len(password) < 8 or password != password2:
        return volver(request, "/configurar", err="La clave debe tener 8 o más caracteres y coincidir en ambos campos")
    c = db()
    cur = c.execute("INSERT INTO users(username,nombre,pw,admin) VALUES(?,?,?,1)",
                    (username.strip().lower(), nombre.strip(), hash_pw(password)))
    c.commit()
    request.session["uid"] = cur.lastrowid
    c.close()
    return RedirectResponse("/", 303)


@app.get("/login")
def login_form(request: Request):
    if not hay_usuarios():
        return RedirectResponse("/configurar", 303)
    return render(request, "login.html")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    clave = (request.client.host if request.client else "?", username.strip().lower())
    if bloqueado(clave):
        return volver(request, "/login", err="Demasiados intentos. Espera 10 minutos.")
    c = db()
    u = c.execute("SELECT * FROM users WHERE username=?", (clave[1],)).fetchone()
    c.close()
    if u and check_pw(password, u["pw"]):
        INTENTOS.pop(clave, None)
        request.session["uid"] = u["id"]
        return RedirectResponse("/", 303)
    INTENTOS.setdefault(clave, []).append(time.time())
    return volver(request, "/login", err="Usuario o clave incorrectos")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


@app.get("/cuenta")
def cuenta(request: Request):
    u = user(request)
    c = db(); pks = c.execute("SELECT id,nombre,creado FROM passkeys WHERE user_id=? ORDER BY id", (u["id"],)).fetchall(); c.close()
    return render(request, "cuenta.html", u=u, pks=pks)


@app.post("/cuenta/clave")
def cambiar_clave(request: Request, actual: str = Form(...), nueva: str = Form(...), nueva2: str = Form(...)):
    u = user(request)
    if not check_pw(actual, u["pw"]):
        return volver(request, "/cuenta", err="La clave actual no es correcta")
    if len(nueva) < 8 or nueva != nueva2:
        return volver(request, "/cuenta", err="La clave nueva debe tener 8 o más caracteres y coincidir")
    c = db()
    c.execute("UPDATE users SET pw=? WHERE id=?", (hash_pw(nueva), u["id"]))
    c.commit(); c.close()
    return volver(request, "/cuenta", msg="Clave cambiada")


@app.post("/cuenta/datos")
def cambiar_datos(request: Request, nombre: str = Form(...), username: str = Form(...)):
    u = user(request)
    c = db()
    try:
        c.execute("UPDATE users SET nombre=?, username=? WHERE id=?", (nombre.strip(), username.strip().lower(), u["id"]))
        c.commit()
    except sqlite3.IntegrityError:
        c.close()
        return volver(request, "/cuenta", err="Ese usuario ya existe")
    c.close()
    return volver(request, "/cuenta", msg="Datos actualizados")


# ---------- PWA ----------
@app.get("/sw.js")
def sw():
    return FileResponse(BASE / "static" / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(BASE / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/health")
def health():
    return {"ok": True}


# ---------- Dashboard ----------
@app.get("/")
def dashboard(request: Request, mes: str = ""):
    u = user(request)
    mes = mes or date.today().strftime("%Y-%m")
    c = db()
    tot = c.execute(f"""SELECT COALESCE(SUM(CASE WHEN monto>0 THEN monto END),0) ing,
        COALESCE(SUM(CASE WHEN monto<0 THEN -monto END),0) gas FROM movs WHERE substr(fecha,1,7)=? AND {EXC}""", (mes,)).fetchone()
    cats = c.execute(f"""SELECT categoria, SUM(-monto) v FROM movs WHERE monto<0 AND {EXC} AND substr(fecha,1,7)=?
        GROUP BY categoria ORDER BY v DESC""", (mes,)).fetchall()
    pers = c.execute(f"""SELECT u.nombre, COALESCE(SUM(CASE WHEN m.monto>0 THEN m.monto END),0) ing,
        COALESCE(SUM(CASE WHEN m.monto<0 THEN -m.monto END),0) gas FROM movs m JOIN users u ON u.id=m.user_id
        WHERE substr(m.fecha,1,7)=? AND m.{EXC} GROUP BY u.id""", (mes,)).fetchall()
    hist = c.execute(f"""SELECT substr(fecha,1,7) mes, COALESCE(SUM(CASE WHEN monto>0 THEN monto END),0) ing,
        COALESCE(SUM(CASE WHEN monto<0 THEN -monto END),0) gas FROM movs WHERE {EXC} GROUP BY 1 ORDER BY 1 DESC LIMIT 6""").fetchall()
    gasto = {x["categoria"]: x["v"] for x in cats}
    pres = [{"categoria": p["categoria"], "limite": p["limite"], "gasto": gasto.get(p["categoria"], 0)}
            for p in c.execute("SELECT categoria, limite FROM presupuestos ORDER BY categoria")]
    pagos = c.execute("""SELECT p.*, u.nombre FROM pagos_tarjeta p JOIN users u ON u.id=p.user_id
        WHERE p.pagado=0 ORDER BY p.fecha_limite LIMIT 3""").fetchall()
    n_pk = c.execute("SELECT COUNT(*) n FROM passkeys WHERE user_id=?", (u["id"],)).fetchone()["n"]
    c.close()
    pagos = [dict(p, dias=(date.fromisoformat(p["fecha_limite"]) - date.today()).days) for p in pagos]
    return render(request, "dashboard.html", u=u, mes=mes, tot=tot, cats=[dict(x) for x in cats],
                  pers=[dict(x) for x in pers], hist=[dict(x) for x in reversed(hist)], pres=pres, pagos=pagos, sin_passkey=(n_pk == 0),
                  mes_txt=mes_txt(mes), prev=mover_mes(mes, -1), next=mover_mes(mes, 1), mes_txt_f=mes_txt)


# ---------- Movimientos ----------
def filtros(mes, persona, cat, q):
    w, p = ["1=1"], []
    if mes:
        w.append("substr(m.fecha,1,7)=?"); p.append(mes)
    if persona:
        w.append("m.user_id=?"); p.append(persona)
    if cat:
        w.append("m.categoria=?"); p.append(cat)
    if q:
        w.append("m.descripcion LIKE ?"); p.append(f"%{q}%")
    return " AND ".join(w), p


@app.get("/movimientos")
def movimientos(request: Request, mes: str = "", persona: int = 0, cat: str = "", q: str = ""):
    u = user(request)
    w, p = filtros(mes, persona, cat, q)
    c = db()
    rows = c.execute(f"""SELECT m.*, u.nombre FROM movs m JOIN users u ON u.id=m.user_id
        WHERE {w} ORDER BY m.fecha DESC, m.id DESC LIMIT 1000""", p).fetchall()
    users = c.execute("SELECT id,nombre FROM users ORDER BY nombre").fetchall()
    c.close()
    grupos = []
    for r in rows:
        if not grupos or grupos[-1]["fecha"] != r["fecha"]:
            grupos.append({"fecha": r["fecha"], "txt": fecha_larga(r["fecha"]), "total": 0, "rows": []})
        if r["categoria"] not in lector.EXCLUIDAS:
            grupos[-1]["total"] += r["monto"]
        grupos[-1]["rows"].append(r)
    return render(request, "movimientos.html", u=u, rows=rows, grupos=grupos, users=users, cats=CATEGORIAS,
                  f={"mes": mes, "persona": persona, "cat": cat, "q": q}, hoy=date.today().isoformat())


@app.post("/movimientos/nuevo")
def nuevo(request: Request, bg: BackgroundTasks, fecha: str = Form(...), descripcion: str = Form(...), monto: float = Form(...),
          tipo: str = Form(...), categoria: str = Form("Otros"), persona: int = Form(...)):
    user(request)
    m = -abs(monto) if tipo == "gasto" else abs(monto)
    h = hashlib.sha256(f"manual{secrets.token_hex(8)}".encode()).hexdigest()
    c = db()
    c.execute("INSERT INTO movs(user_id,banco,fecha,descripcion,monto,categoria,origen,hash) VALUES(?,?,?,?,?,?,?,?)",
              (persona, "Manual", fecha, descripcion.strip()[:200], m, categoria, "manual", h))
    c.commit(); c.close()
    bg.add_task(avisos.revisar_presupuestos, db, PEM)
    return RedirectResponse("/movimientos", 303)


@app.post("/movimientos/{mid}/categoria")
def cambiar_cat(request: Request, mid: int, categoria: str = Form(...), aplicar_similares: str = Form("")):
    user(request)
    c = db()
    c.execute("UPDATE movs SET categoria=? WHERE id=?", (categoria, mid))
    if aplicar_similares:
        d = c.execute("SELECT descripcion FROM movs WHERE id=?", (mid,)).fetchone()
        if d:
            kw = lector.clave_regla(d["descripcion"])
            if kw:
                c.execute("INSERT OR REPLACE INTO reglas(kw,categoria) VALUES(?,?)", (kw, categoria))
                c.execute("UPDATE movs SET categoria=? WHERE lower(descripcion) LIKE ?", (categoria, f"%{kw}%"))
    c.commit(); c.close()
    return RedirectResponse(request.headers.get("referer", "/movimientos"), 303)


@app.post("/movimientos/{mid}/borrar")
def borrar(request: Request, mid: int):
    user(request)
    c = db(); c.execute("DELETE FROM movs WHERE id=?", (mid,)); c.commit(); c.close()
    return RedirectResponse(request.headers.get("referer", "/movimientos"), 303)


@app.get("/export.csv")
def export(request: Request, mes: str = "", persona: int = 0, cat: str = "", q: str = ""):
    user(request)
    w, p = filtros(mes, persona, cat, q)
    c = db()
    rows = c.execute(f"""SELECT m.fecha,u.nombre,m.banco,m.descripcion,m.monto,m.categoria FROM movs m
        JOIN users u ON u.id=m.user_id WHERE {w} ORDER BY m.fecha""", p).fetchall()
    c.close()
    buf = io.StringIO(); wr = csv.writer(buf)
    wr.writerow(["fecha", "persona", "banco", "descripcion", "monto", "categoria"])
    wr.writerows([tuple(r) for r in rows])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=movimientos.csv"})


# ---------- Subir PDFs ----------
@app.get("/subir")
def subir_form(request: Request):
    u = user(request)
    c = db(); users = c.execute("SELECT id,nombre FROM users ORDER BY nombre").fetchall(); c.close()
    return render(request, "subir.html", u=u, users=users, bancos=BANCOS, resultado=None)


@app.post("/subir")
async def subir(request: Request, bg: BackgroundTasks, archivos: list[UploadFile] = File(...), persona: int = Form(...),
                banco: str = Form("Nu"), tipo: str = Form("cuenta"), clave: str = Form("")):
    u = user(request)
    c = db()
    reglas = [(r["kw"], r["categoria"]) for r in c.execute("SELECT kw,categoria FROM reglas")]
    resultado = []
    for f in archivos:
        datos = await f.read()
        if len(datos) > MAX_MB * 1024 * 1024:
            resultado.append((f.filename, "Archivo demasiado grande", 0, 0, None)); continue
        try:
            movs, info = lector.leer_pdf(datos, clave, tipo, reglas)
        except Exception as e:
            nombre = type(e).__name__.lower()
            msg = ("Clave incorrecta (o PDF no legible)" if "password" in nombre or "pdfminer" in nombre
                   or "password" in str(e).lower() else f"No se pudo leer ({type(e).__name__})")
            resultado.append((f.filename, msg, 0, 0, None)); continue
        nuevos, vistos = 0, {}
        for m in movs:
            k = f"{persona}|{banco}|{m['origen']}|{m['fecha']}|{m['descripcion']}|{m['monto']}"
            vistos[k] = vistos.get(k, 0) + 1
            h = hashlib.sha256(f"{k}|{vistos[k]}".encode()).hexdigest()
            try:
                c.execute("""INSERT INTO movs(user_id,banco,fecha,descripcion,monto,categoria,origen,hash,archivo)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (persona, banco, m["fecha"], m["descripcion"], m["monto"],
                                                   m["categoria"], m["origen"], h, f.filename))
                nuevos += 1
            except sqlite3.IntegrityError:
                pass  # duplicado: ya estaba cargado
        aviso = None
        if info:
            c.execute("""INSERT INTO pagos_tarjeta(user_id,banco,fecha_limite,pago_minimo,a_pagar,deuda_total)
                VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,banco,fecha_limite) DO UPDATE SET
                pago_minimo=excluded.pago_minimo, a_pagar=excluded.a_pagar, deuda_total=excluded.deuda_total""",
                      (persona, banco, info["fecha_limite"], info["pago_minimo"], info["a_pagar"], info["deuda_total"]))
            aviso = f"Pago mínimo {avisos.cop(info['pago_minimo'] or 0)}, vence el {info['fecha_limite']}. Te avisaremos antes."
        resultado.append((f.filename, "OK" if movs else "Sin movimientos reconocidos", len(movs), nuevos, aviso))
    c.execute("INSERT OR REPLACE INTO config(clave,valor) VALUES('ultima_subida',?)", (avisos.ahora().isoformat(),))
    c.commit()
    users = c.execute("SELECT id,nombre FROM users ORDER BY nombre").fetchall(); c.close()
    bg.add_task(avisos.revisar_presupuestos, db, PEM)
    return render(request, "subir.html", u=u, users=users, bancos=BANCOS, resultado=resultado)


# ---------- Miembros ----------
@app.get("/miembros")
def miembros(request: Request):
    u = user(request)
    c = db(); users = c.execute("SELECT id,username,nombre,admin FROM users ORDER BY nombre").fetchall(); c.close()
    return render(request, "miembros.html", u=u, users=users)


@app.post("/miembros")
def crear_miembro(request: Request, username: str = Form(...), nombre: str = Form(...), password: str = Form(...)):
    u = user(request)
    if not u["admin"]:
        return volver(request, "/miembros", err="Solo el administrador puede agregar miembros")
    if len(password) < 8:
        return volver(request, "/miembros", err="La clave debe tener 8 o más caracteres")
    c = db()
    try:
        c.execute("INSERT INTO users(username,nombre,pw) VALUES(?,?,?)",
                  (username.strip().lower(), nombre.strip(), hash_pw(password)))
        c.commit()
    except sqlite3.IntegrityError:
        c.close(); return volver(request, "/miembros", err="Ese usuario ya existe")
    c.close()
    return volver(request, "/miembros", msg="Miembro agregado")


@app.post("/miembros/{uid}/clave")
def reset_clave(request: Request, uid: int, password: str = Form(...)):
    u = user(request)
    if not u["admin"]:
        return volver(request, "/miembros", err="Solo el administrador puede cambiar claves de otros")
    if len(password) < 8:
        return volver(request, "/miembros", err="La clave debe tener 8 o más caracteres")
    c = db(); c.execute("UPDATE users SET pw=? WHERE id=?", (hash_pw(password), uid)); c.commit(); c.close()
    return volver(request, "/miembros", msg="Clave restablecida")


# ---------- Avisos ----------
@app.get("/avisos")
def pagina_avisos(request: Request):
    u = user(request)
    c = db()
    d = dict(
        recs=c.execute("SELECT r.*, u.nombre FROM recordatorios r LEFT JOIN users u ON u.id=r.para ORDER BY r.dia").fetchall(),
        pres=c.execute("SELECT * FROM presupuestos ORDER BY categoria").fetchall(),
        pagos=c.execute("""SELECT p.*, u.nombre FROM pagos_tarjeta p JOIN users u ON u.id=p.user_id
            ORDER BY p.pagado, p.fecha_limite DESC LIMIT 6""").fetchall(),
        users=c.execute("SELECT id,nombre FROM users ORDER BY nombre").fetchall(),
        dia_ext=avisos.cfg(c, "dia_extracto", 4), hora=avisos.cfg(c, "hora_aviso", 9),
        n_disp=c.execute("SELECT COUNT(*) n FROM push_subs WHERE user_id=?", (u["id"],)).fetchone()["n"])
    c.close()
    return render(request, "avisos.html", u=u, cats=CATEGORIAS, **d)


@app.get("/push/clave")
def push_clave(request: Request):
    user(request)
    return {"key": PUBLICA}


@app.post("/push/suscribir")
async def push_suscribir(request: Request):
    u = user(request)
    s = await request.json()
    try:
        ep, k = s["endpoint"], s["keys"]
        c = db()
        c.execute("INSERT OR REPLACE INTO push_subs(user_id,endpoint,p256dh,auth) VALUES(?,?,?,?)",
                  (u["id"], ep, k["p256dh"], k["auth"]))
        c.commit(); c.close()
    except (KeyError, TypeError):
        return JSONResponse({"ok": False}, 400)
    return {"ok": True}


@app.post("/push/probar")
async def push_probar(request: Request):
    u = user(request)
    n = await asyncio.to_thread(avisos.enviar, db, PEM, [u["id"]], "Cuadre funciona",
                                "Así te llegarán los avisos de pagos y presupuestos.")
    return {"enviados": n}


@app.post("/avisos/recordatorio")
def nuevo_recordatorio(request: Request, titulo: str = Form(...), dia: int = Form(...), antes: int = Form(0),
                       para: int = Form(0)):
    user(request)
    c = db()
    c.execute("INSERT INTO recordatorios(titulo,dia,antes,para) VALUES(?,?,?,?)",
              (titulo.strip()[:80], max(1, min(31, dia)), max(0, min(15, antes)), para))
    c.commit(); c.close()
    return volver(request, "/avisos", msg="Recordatorio creado")


@app.post("/avisos/recordatorio/{rid}/borrar")
def borrar_recordatorio(request: Request, rid: int):
    user(request)
    c = db(); c.execute("DELETE FROM recordatorios WHERE id=?", (rid,)); c.commit(); c.close()
    return RedirectResponse("/avisos", 303)


@app.post("/avisos/presupuesto")
def nuevo_presupuesto(request: Request, bg: BackgroundTasks, categoria: str = Form(...), limite: float = Form(...)):
    user(request)
    c = db()
    c.execute("INSERT OR REPLACE INTO presupuestos(categoria,limite) VALUES(?,?)", (categoria, abs(limite)))
    c.commit(); c.close()
    bg.add_task(avisos.revisar_presupuestos, db, PEM)
    return volver(request, "/avisos", msg="Presupuesto guardado")


@app.post("/avisos/presupuesto/{pid}/borrar")
def borrar_presupuesto(request: Request, pid: int):
    user(request)
    c = db(); c.execute("DELETE FROM presupuestos WHERE id=?", (pid,)); c.commit(); c.close()
    return RedirectResponse("/avisos", 303)


@app.post("/avisos/pago/{pid}/pagado")
def marcar_pagado(request: Request, pid: int):
    user(request)
    c = db(); c.execute("UPDATE pagos_tarjeta SET pagado=1-pagado WHERE id=?", (pid,)); c.commit(); c.close()
    return RedirectResponse("/avisos", 303)


@app.post("/avisos/config")
def config_avisos(request: Request, dia_extracto: int = Form(...), hora_aviso: int = Form(...)):
    user(request)
    c = db()
    c.execute("INSERT OR REPLACE INTO config(clave,valor) VALUES('dia_extracto',?)", (str(max(1, min(28, dia_extracto))),))
    c.execute("INSERT OR REPLACE INTO config(clave,valor) VALUES('hora_aviso',?)", (str(max(0, min(23, hora_aviso))),))
    c.commit(); c.close()
    return volver(request, "/avisos", msg="Configuración guardada")


# ---------- Passkeys (WebAuthn) ----------
def rp(request: Request):
    host = request.headers.get("host", "localhost")
    return host.split(":")[0], f"{request.url.scheme}://{host}"


def _json(opts):
    return Response(content=options_to_json(opts), media_type="application/json")


@app.post("/passkey/registro/opciones")
def pk_reg_opciones(request: Request):
    u = user(request)
    rp_id, _ = rp(request)
    c = db(); ya = c.execute("SELECT credential_id FROM passkeys WHERE user_id=?", (u["id"],)).fetchall(); c.close()
    opts = generate_registration_options(
        rp_id=rp_id, rp_name="Cuadre", user_id=f"u{u['id']}".encode(), user_name=u["username"], user_display_name=u["nombre"],
        authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED,
                                                               user_verification=UserVerificationRequirement.PREFERRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(x["credential_id"])) for x in ya])
    request.session["pk_chal"] = bytes_to_base64url(opts.challenge)
    return _json(opts)


@app.post("/passkey/registro/verificar")
async def pk_reg_verificar(request: Request):
    u = user(request)
    body = await request.json()
    chal = request.session.pop("pk_chal", None)
    rp_id, origin = rp(request)
    if not chal:
        return JSONResponse({"ok": False, "error": "Sesión vencida, intenta de nuevo"}, 400)
    try:
        v = verify_registration_response(credential=body["credencial"], expected_challenge=base64url_to_bytes(chal),
                                         expected_rp_id=rp_id, expected_origin=origin)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"No se pudo verificar ({type(e).__name__})"}, 400)
    c = db()
    c.execute("INSERT OR REPLACE INTO passkeys(user_id,credential_id,public_key,sign_count,nombre,creado) VALUES(?,?,?,?,?,?)",
              (u["id"], bytes_to_base64url(v.credential_id), bytes_to_base64url(v.credential_public_key), v.sign_count,
               (body.get("nombre") or "Mi dispositivo")[:40], avisos.ahora().isoformat()))
    c.commit(); c.close()
    return {"ok": True}


@app.post("/passkey/login/opciones")
def pk_login_opciones(request: Request):
    rp_id, _ = rp(request)
    opts = generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.PREFERRED)
    request.session["pk_chal"] = bytes_to_base64url(opts.challenge)
    return _json(opts)


@app.post("/passkey/login/verificar")
async def pk_login_verificar(request: Request):
    body = await request.json()
    chal = request.session.pop("pk_chal", None)
    rp_id, origin = rp(request)
    cred = body.get("credencial") or {}
    c = db()
    row = c.execute("SELECT * FROM passkeys WHERE credential_id=?", (cred.get("id", ""),)).fetchone()
    if not chal or not row:
        c.close()
        return JSONResponse({"ok": False, "error": "Esta passkey no está registrada"}, 400)
    try:
        v = verify_authentication_response(credential=cred, expected_challenge=base64url_to_bytes(chal),
                                           expected_rp_id=rp_id, expected_origin=origin,
                                           credential_public_key=base64url_to_bytes(row["public_key"]),
                                           credential_current_sign_count=row["sign_count"])
    except Exception as e:
        c.close()
        return JSONResponse({"ok": False, "error": f"No se pudo verificar ({type(e).__name__})"}, 400)
    c.execute("UPDATE passkeys SET sign_count=? WHERE id=?", (v.new_sign_count, row["id"]))
    c.commit(); c.close()
    request.session["uid"] = row["user_id"]
    return {"ok": True}


@app.post("/passkey/{pid}/borrar")
def pk_borrar(request: Request, pid: int):
    u = user(request)
    c = db(); c.execute("DELETE FROM passkeys WHERE id=? AND user_id=?", (pid, u["id"])); c.commit(); c.close()
    return volver(request, "/cuenta", msg="Passkey eliminada")
