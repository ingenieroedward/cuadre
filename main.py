import csv
import hashlib
import io
import os
import secrets
import sqlite3
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import lector

DATA = Path(os.environ.get("DATA_DIR", "./data"))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "finanzas.db"
SECRET = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
MAX_MB = 25

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET, same_site="lax",
                   https_only=os.environ.get("HTTPS_ONLY", "0") == "1", max_age=60 * 60 * 24 * 14)
tpl = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
CATEGORIAS = list(lector.CATEGORIAS_BASE) + ["Transferencias recibidas", "Transferencias enviadas", "Traslado propio", "Pago de tarjeta", "Arriendo", "Ocio", "Otros"]
EXC = "categoria NOT IN (" + ",".join(f"'{x}'" for x in lector.EXCLUIDAS) + ")"
BANCOS = ["Nu", "Bancolombia", "Davivienda", "BBVA", "Nequi", "Otro"]


# ---------- DB ----------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${h}"


def check_pw(pw, stored):
    salt, _ = stored.split("$")
    return secrets.compare_digest(hash_pw(pw, salt), stored)


def init():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, nombre TEXT, pw TEXT, admin INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS movs(id INTEGER PRIMARY KEY, user_id INTEGER, banco TEXT, fecha TEXT, descripcion TEXT,
        monto REAL, categoria TEXT, origen TEXT, hash TEXT UNIQUE, archivo TEXT);
    CREATE TABLE IF NOT EXISTS reglas(id INTEGER PRIMARY KEY, kw TEXT UNIQUE, categoria TEXT);
    CREATE INDEX IF NOT EXISTS ix_f ON movs(fecha);
    """)
    if not c.execute("SELECT 1 FROM users").fetchone():
        u, p = os.environ.get("ADMIN_USER", "admin"), os.environ.get("ADMIN_PASSWORD")
        if not p:
            p = secrets.token_urlsafe(12)
            print(f"*** Usuario admin creado: {u} / {p}  (define ADMIN_PASSWORD para fijarla)", flush=True)
        c.execute("INSERT INTO users(username,nombre,pw,admin) VALUES(?,?,?,1)", (u, u.capitalize(), hash_pw(p)))
        c.commit()
    c.close()


init()


# ---------- Auth ----------
class NoAuth(Exception):
    pass


@app.exception_handler(NoAuth)
async def _noauth(request, exc):
    return RedirectResponse("/login", 303)


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
    return tpl.TemplateResponse(request, name, ctx)


@app.get("/login")
def login_form(request: Request):
    return render(request, "login.html", error=None)


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    c = db()
    u = c.execute("SELECT * FROM users WHERE username=?", (username.strip().lower(),)).fetchone()
    c.close()
    if u and check_pw(password, u["pw"]):
        request.session["uid"] = u["id"]
        return RedirectResponse("/", 303)
    return render(request, "login.html", error="Usuario o clave incorrectos")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


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
        WHERE substr(m.fecha,1,7)=? AND m.categoria NOT IN ('Traslado propio','Pago de tarjeta') GROUP BY u.id""", (mes,)).fetchall()
    hist = c.execute(f"""SELECT substr(fecha,1,7) mes, COALESCE(SUM(CASE WHEN monto>0 THEN monto END),0) ing,
        COALESCE(SUM(CASE WHEN monto<0 THEN -monto END),0) gas FROM movs WHERE {EXC} GROUP BY 1 ORDER BY 1 DESC LIMIT 12""").fetchall()
    c.close()
    return render(request, "dashboard.html", u=u, mes=mes, tot=tot, cats=[dict(x) for x in cats],
                  pers=[dict(x) for x in pers], hist=[dict(x) for x in reversed(hist)])


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
    return render(request, "movimientos.html", u=u, rows=rows, users=users, cats=CATEGORIAS, bancos=BANCOS,
                  f={"mes": mes, "persona": persona, "cat": cat, "q": q}, hoy=date.today().isoformat())


@app.post("/movimientos/nuevo")
def nuevo(request: Request, fecha: str = Form(...), descripcion: str = Form(...), monto: float = Form(...),
          tipo: str = Form(...), categoria: str = Form("Otros"), persona: int = Form(...)):
    user(request)
    m = -abs(monto) if tipo == "gasto" else abs(monto)
    h = hashlib.sha256(f"manual{secrets.token_hex(8)}".encode()).hexdigest()
    c = db()
    c.execute("INSERT INTO movs(user_id,banco,fecha,descripcion,monto,categoria,origen,hash) VALUES(?,?,?,?,?,?,?,?)",
              (persona, "Manual", fecha, descripcion.strip()[:200], m, categoria, "manual", h))
    c.commit(); c.close()
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
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=movimientos.csv"})


# ---------- Subir PDFs ----------
@app.get("/subir")
def subir_form(request: Request):
    u = user(request)
    c = db(); users = c.execute("SELECT id,nombre FROM users ORDER BY nombre").fetchall(); c.close()
    return render(request, "subir.html", u=u, users=users, bancos=BANCOS, resultado=None)


@app.post("/subir")
async def subir(request: Request, archivos: list[UploadFile] = File(...), persona: int = Form(...),
                banco: str = Form("Nu"), tipo: str = Form("cuenta"), clave: str = Form("")):
    u = user(request)
    c = db()
    reglas = [(r["kw"], r["categoria"]) for r in c.execute("SELECT kw,categoria FROM reglas")]
    resultado = []
    for f in archivos:
        datos = await f.read()
        if len(datos) > MAX_MB * 1024 * 1024:
            resultado.append((f.filename, "Archivo demasiado grande", 0, 0)); continue
        try:
            movs = lector.leer_pdf(datos, clave, tipo, reglas)
        except Exception as e:
            nombre = type(e).__name__.lower()
            msg = ("Clave incorrecta (o PDF no legible)" if "password" in nombre or "pdfminer" in nombre or "password" in str(e).lower()
                   else f"No se pudo leer ({type(e).__name__})")
            resultado.append((f.filename, msg, 0, 0)); continue
        nuevos, vistos = 0, {}
        for m in movs:
            k = f"{persona}|{banco}|{tipo}|{m['fecha']}|{m['descripcion']}|{m['monto']}"
            vistos[k] = vistos.get(k, 0) + 1
            h = hashlib.sha256(f"{k}|{vistos[k]}".encode()).hexdigest()
            try:
                c.execute("""INSERT INTO movs(user_id,banco,fecha,descripcion,monto,categoria,origen,hash,archivo)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (persona, banco, m["fecha"], m["descripcion"], m["monto"],
                                                   m["categoria"], tipo, h, f.filename))
                nuevos += 1
            except sqlite3.IntegrityError:
                pass  # duplicado: ya estaba cargado
        resultado.append((f.filename, "OK" if movs else "Sin movimientos reconocidos", len(movs), nuevos))
    c.commit()
    users = c.execute("SELECT id,nombre FROM users ORDER BY nombre").fetchall(); c.close()
    return render(request, "subir.html", u=u, users=users, bancos=BANCOS, resultado=resultado)


# ---------- Miembros ----------
@app.get("/miembros")
def miembros(request: Request, error: str = ""):
    u = user(request)
    c = db(); users = c.execute("SELECT id,username,nombre,admin FROM users ORDER BY nombre").fetchall(); c.close()
    return render(request, "miembros.html", u=u, users=users, error=error)


@app.post("/miembros")
def crear_miembro(request: Request, username: str = Form(...), nombre: str = Form(...), password: str = Form(...)):
    u = user(request)
    if not u["admin"]:
        return RedirectResponse("/miembros?error=Solo el administrador puede agregar miembros", 303)
    if len(password) < 8:
        return RedirectResponse("/miembros?error=La clave debe tener m%C3%A1s de 8 caracteres", 303)
    c = db()
    try:
        c.execute("INSERT INTO users(username,nombre,pw) VALUES(?,?,?)",
                  (username.strip().lower(), nombre.strip(), hash_pw(password)))
        c.commit()
    except sqlite3.IntegrityError:
        c.close(); return RedirectResponse("/miembros?error=Ese usuario ya existe", 303)
    c.close()
    return RedirectResponse("/miembros", 303)


@app.get("/health")
def health():
    return {"ok": True}
