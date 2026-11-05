import asyncio
import calendar
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
import captura
import correo
import cuentas
import deudas
import agenda
import asesor
import icons
import lector
import metas
import saldos

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
    async def ciclo_correo():
        while True:
            try:
                if correo.configurado():
                    await asyncio.to_thread(correo.revisar_buzon, db)
            except Exception as e:
                print("correo:", e, flush=True)
            await asyncio.sleep(300)
    tareas = [asyncio.create_task(ciclo()), asyncio.create_task(ciclo_correo())]
    yield
    for t in tareas:
        t.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=_secreto(), same_site="lax",
                   https_only=os.environ.get("HTTPS_ONLY", "0") == "1", max_age=60 * 60 * 24 * 30)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
tpl = Jinja2Templates(directory=str(BASE / "templates"))
tpl.env.filters["cop"] = avisos.cop
tpl.env.filters["ico"] = icons.cat
tpl.env.filters["color"] = icons.color
tpl.env.filters["en_meses"] = lambda n: f"{MES_ES[deudas.add_months(date.today(), int(n)).month - 1][:3]} {deudas.add_months(date.today(), int(n)).year}"
tpl.env.filters["todate"] = lambda s: date.fromisoformat(s)
tpl.env.filters["dia_es"] = lambda d: f"{DIA_ES[d.weekday()][:3]} {d.day} {MES_ES[d.month - 1][:3]}"
def _md(t):
    """Markdown mínimo y seguro: **negrita**, listas con '-' y numeradas, párrafos."""
    import re as _re
    from markupsafe import Markup, escape
    out, lista = [], None
    def cierra():
        nonlocal lista
        if lista:
            out.append(f"</{lista}>")
            lista = None
    for linea in (t or "").splitlines():
        s = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", str(escape(linea.strip())))
        mm = _re.match(r"^(?:[-•*]\s+)(.*)$", s)
        mn = _re.match(r"^\d+[.)]\s+(.*)$", s)
        if mm or mn:
            tag = "ul" if mm else "ol"
            if lista != tag:
                cierra(); out.append(f"<{tag} class='md'>"); lista = tag
            out.append(f"<li>{(mm or mn).group(1)}</li>")
        elif s:
            cierra(); out.append(f"<p>{s}</p>")
        else:
            cierra()
    cierra()
    return Markup("".join(out))


def _md_inline(t):
    import re as _re
    from markupsafe import Markup, escape
    return Markup(_re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", str(escape(t or ""))))


tpl.env.filters["md"] = _md
tpl.env.filters["md_inline"] = _md_inline
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
    CREATE TABLE IF NOT EXISTS saldos(id INTEGER PRIMARY KEY, user_id INTEGER, banco TEXT, fecha TEXT, saldo REAL,
        corte_id INTEGER, fuente TEXT, UNIQUE(user_id,banco,fecha,fuente));
    CREATE TABLE IF NOT EXISTS deudas(id INTEGER PRIMARY KEY, user_id INTEGER, nombre TEXT, tipo TEXT, direccion TEXT DEFAULT 'debo',
        principal REAL, n_cuotas INTEGER, cuota REAL, tasa_ea REAL, primera_cuota TEXT, palabra_clave TEXT, notas TEXT, activo INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS deuda_pagos(id INTEGER PRIMARY KEY, deuda_id INTEGER, fecha TEXT, monto REAL, tipo TEXT DEFAULT 'cuota',
        mov_id INTEGER, nota TEXT);
    CREATE TABLE IF NOT EXISTS cuentas(id INTEGER PRIMARY KEY, user_id INTEGER, nombre TEXT, entidad TEXT, tipo TEXT DEFAULT 'ahorros',
        exenta INTEGER DEFAULT 0, ultimos4 TEXT, activa INTEGER DEFAULT 1, UNIQUE(user_id,nombre));
    CREATE TABLE IF NOT EXISTS cajitas(id INTEGER PRIMARY KEY, user_id INTEGER, banco TEXT, fecha TEXT, saldo_cajitas REAL, ea_cajitas REAL,
        rend_cajitas REAL, saldo_cuenta REAL, ea_cuenta REAL, rend_cuenta REAL, UNIQUE(user_id,banco,fecha));
    CREATE TABLE IF NOT EXISTS rend_cfg(user_id INTEGER, banco TEXT, ea_actual REAL, PRIMARY KEY(user_id,banco));
    CREATE TABLE IF NOT EXISTS metas(id INTEGER PRIMARY KEY, user_id INTEGER, nombre TEXT, objetivo REAL, periodo TEXT DEFAULT 'total',
        fecha_limite TEXT, bono_ea REAL DEFAULT 0, banco TEXT, avisar INTEGER DEFAULT 1, activa INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS meta_aportes(id INTEGER PRIMARY KEY, meta_id INTEGER, fecha TEXT, monto REAL, nota TEXT);
    CREATE TABLE IF NOT EXISTS asesor_hist(id INTEGER PRIMARY KEY, user_id INTEGER, modo TEXT, pregunta TEXT, respuesta TEXT, creado TEXT,
        firma TEXT, tok_in INTEGER DEFAULT 0, tok_out INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS asesor_acciones(id INTEGER PRIMARY KEY, user_id INTEGER, texto TEXT UNIQUE, estado TEXT DEFAULT 'pendiente',
        hist_id INTEGER, creado TEXT);
    CREATE TABLE IF NOT EXISTS ingresos_fijos(id INTEGER PRIMARY KEY, user_id INTEGER, nombre TEXT, monto REAL, dia INTEGER, banco TEXT, activo INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS passkeys(id INTEGER PRIMARY KEY, user_id INTEGER, credential_id TEXT UNIQUE, public_key TEXT,
        sign_count INTEGER DEFAULT 0, nombre TEXT, creado TEXT);
    """)
    c.execute("""CREATE TABLE IF NOT EXISTS capturas(id INTEGER PRIMARY KEY, user_id INTEGER, fuente TEXT, texto TEXT, estado TEXT,
        monto REAL, descripcion TEXT, fecha TEXT, banco TEXT, mov_id INTEGER, creado TEXT)""")
    for tabla, col, defin in [("movs", "estado", "TEXT DEFAULT 'confirmado'"), ("movs", "fuente", "TEXT DEFAULT 'extracto'"),
                              ("users", "token", "TEXT"), ("deudas", "origen", "TEXT DEFAULT 'manual'"),
                              ("deudas", "cuotas_fact", "INTEGER DEFAULT 0"), ("deudas", "restante", "REAL"),
                              ("deudas", "tasa_mv", "REAL"), ("deudas", "clave_ext", "TEXT"),
                              ("asesor_hist", "firma", "TEXT"), ("asesor_hist", "tok_in", "INTEGER DEFAULT 0"), ("asesor_hist", "tok_out", "INTEGER DEFAULT 0"),
                              ("cuentas", "cuota_manejo", "REAL DEFAULT 0"), ("cuentas", "manejo_cond", "TEXT DEFAULT 'siempre'"), ("pagos_tarjeta", "corte", "TEXT"), ("rend_cfg", "cajitas_base", "REAL"), ("rend_cfg", "base_fecha", "TEXT"), ("pagos_tarjeta", "cupo", "REAL"), ("pagos_tarjeta", "usado", "REAL"), ("pagos_tarjeta", "disponible", "REAL"), ("recordatorios", "fecha", "TEXT"), ("recordatorios", "monto", "REAL"), ("recordatorios", "repite", "TEXT DEFAULT 'mensual'")]:
        if col not in [r["name"] for r in c.execute(f"PRAGMA table_info({tabla})")]:
            c.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {defin}")
    cuentas.sembrar(c)
    # El plan de acción se llena al guardar un consejo; los consejos anteriores a esa función se importan una sola vez.
    if not c.execute("SELECT 1 FROM config WHERE clave='mig_plan_ia'").fetchone():
        if not c.execute("SELECT 1 FROM asesor_acciones").fetchone():
            for h in c.execute("""SELECT * FROM asesor_hist WHERE modo IN ('analisis','tarjeta') AND id IN
                    (SELECT MAX(id) FROM asesor_hist WHERE modo IN ('analisis','tarjeta') GROUP BY modo)""").fetchall():
                for t in asesor.extraer_acciones(h["respuesta"]):
                    c.execute("INSERT OR IGNORE INTO asesor_acciones(user_id,texto,estado,hist_id,creado) VALUES(?,?,?,?,?)",
                              (h["user_id"], t, "pendiente", h["id"], h["creado"]))
        c.execute("INSERT OR REPLACE INTO config(clave,valor) VALUES('mig_plan_ia','1')")
    if not c.execute("SELECT 1 FROM config WHERE clave='mig_plan_unico'").fetchone():
        asesor.unificar_acciones(c)
        c.execute("INSERT OR REPLACE INTO config(clave,valor) VALUES('mig_plan_unico','1')")
    # Corrección única: antes se suponía que Nu estaba exenta; Nu sí cobra 4x1000. La persona puede cambiarlo en Cuentas.
    # Corrección única (la persona confirmó): Nu está exenta de 4x1000 y Nequi sí lo cobra.
    if not c.execute("SELECT 1 FROM config WHERE clave='mig_gmf_v2'").fetchone():
        c.execute("UPDATE cuentas SET exenta=1 WHERE lower(nombre)='nu' AND tipo='ahorros'")
        c.execute("UPDATE cuentas SET exenta=0 WHERE lower(nombre)='nequi'")
        c.execute("INSERT OR REPLACE INTO config(clave,valor) VALUES('mig_gmf_v2','1')")
    c.commit()
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


def lista_cuentas(c):
    """Una opción por cuenta Y por dueño: 'Nequi · Edward', 'Nequi · Vanesa'. El valor es 'id_usuario|nombre'."""
    out = []
    for u_ in c.execute("SELECT id,nombre FROM users ORDER BY nombre").fetchall():
        out.append({"v": f"{u_['id']}|Efectivo", "label": f"Efectivo · {u_['nombre']}", "user_id": u_["id"], "nombre": "Efectivo",
                    "efectivo": True, "tarjeta": False})
        for q in c.execute("SELECT nombre,tipo FROM cuentas WHERE user_id=? AND activa=1 ORDER BY nombre", (u_["id"],)).fetchall():
            out.append({"v": f"{u_['id']}|{q['nombre']}", "label": f"{q['nombre']} · {u_['nombre']}", "user_id": u_["id"], "nombre": q["nombre"],
                        "efectivo": False, "tarjeta": q["tipo"] == "tarjeta"})
    return out


def parse_cuenta(v, user_defecto=0):
    """'3|Nequi' -> (3, 'Nequi')"""
    try:
        uid, nombre = (v or "").split("|", 1)
        return int(uid), nombre
    except ValueError:
        return user_defecto, ""


def render(request, name, **ctx):
    ctx.setdefault("u", None)
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("err", request.query_params.get("err"))
    if ctx["u"]:
        c = db()
        ctx["recientes"] = [r["descripcion"] for r in c.execute(
            "SELECT descripcion, COUNT(*) n FROM movs WHERE monto<0 GROUP BY descripcion ORDER BY n DESC, MAX(fecha) DESC LIMIT 30")]
        ctx["usuarios"] = c.execute("SELECT id,nombre FROM users ORDER BY nombre").fetchall()
        ctx["n_revisar"] = c.execute("SELECT COUNT(*) n FROM capturas WHERE estado='por_revisar'").fetchone()["n"]
        ctx.setdefault("pre", {})
        ctx["cuentas_n"] = cuentas.nombres(c, BANCOS)
        ctx["medios"] = ["Efectivo"] + ctx["cuentas_n"]
        todas_c = lista_cuentas(c)
        ctx["cuentas_op_t"] = todas_c                                         # con tarjetas (para anotar compras)
        ctx["cuentas_op"] = [x for x in todas_c if not x["tarjeta"]]          # cuentas y efectivo
        ctx["cuentas_ext"] = [x for x in todas_c if not x["tarjeta"] and not x["efectivo"]]   # para subir extractos
        c.close()
        ctx["cats_q"] = CATEGORIAS
        ctx["hoy_iso"] = avisos.ahora().date().isoformat()
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
    lista_saldos = saldos.actuales(c)
    resumen_d = resumen_deudas(c)
    libre = metas.disponible(c)
    prox7 = agenda.eventos(c, date.today(), 8)[:4]
    n_prov = c.execute("SELECT COUNT(*) n FROM movs WHERE estado='provisional' AND substr(fecha,1,7)=?", (mes,)).fetchone()["n"]
    n_pk = c.execute("SELECT COUNT(*) n FROM passkeys WHERE user_id=?", (u["id"],)).fetchone()["n"]
    c.close()
    pagos = [dict(p, dias=(date.fromisoformat(p["fecha_limite"]) - date.today()).days) for p in pagos]
    return render(request, "dashboard.html", u=u, mes=mes, tot=tot, cats=[dict(x) for x in cats],
                  pers=[dict(x) for x in pers], hist=[dict(x) for x in reversed(hist)], pres=pres, pagos=pagos, sin_passkey=(n_pk == 0), n_prov=n_prov, saldos=lista_saldos, res_deudas=resumen_d, libre=libre, prox7=prox7, saldo_total=sum(x['estimado'] for x in lista_saldos),
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
    mes = "" if mes == "todos" else (mes or date.today().strftime("%Y-%m"))
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
    c.execute("INSERT INTO movs(user_id,banco,fecha,descripcion,monto,categoria,origen,hash,estado,fuente) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (persona, "Manual", fecha, descripcion.strip()[:200], m, categoria, "manual", h, "confirmado", "manual"))
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


def m_tipo_tarjeta(movs, info, tipo):
    return tipo == "tarjeta" or any(x.get("origen") == "tarjeta" for x in movs)


# ---------- Subir PDFs ----------
@app.get("/subir")
def subir_form(request: Request):
    u = user(request)
    c = db(); users = c.execute("SELECT id,nombre FROM users ORDER BY nombre").fetchall()
    lista_c = cuentas.nombres(c, BANCOS)
    pares = [(r["user_id"], r["nombre"]) for r in c.execute("SELECT user_id,nombre FROM cuentas WHERE activa=1 ORDER BY nombre")]
    c.close()
    return render(request, "subir.html", u=u, users=users, bancos=lista_c, pares=pares, resultado=None)


@app.post("/subir")
async def subir(request: Request, bg: BackgroundTasks, archivos: list[UploadFile] = File(...), persona: int = Form(0),
                banco: str = Form("Nu"), banco_nueva: str = Form(""), tipo: str = Form("cuenta"), clave: str = Form(""),
                cuenta: str = Form("")):
    u = user(request)
    if cuenta and cuenta != "__nueva__":
        persona, banco = parse_cuenta(cuenta, u["id"])
    elif cuenta == "__nueva__" or banco == "__nueva__":
        banco = banco_nueva
    persona = persona or u["id"]
    c = db()
    reglas = [(r["kw"], r["categoria"]) for r in c.execute("SELECT kw,categoria FROM reglas")]
    banco = banco.strip() or "Nu"
    cuentas.asegurar(c, persona, banco)
    resultado = []
    for f in archivos:
        datos = await f.read()
        if len(datos) > MAX_MB * 1024 * 1024:
            resultado.append({"f": f.filename, "e": "Archivo demasiado grande", "l": 0, "n": 0, "notas": []}); continue
        try:
            movs, info = lector.leer_pdf(datos, clave, tipo, reglas)
        except Exception as e:
            nombre = type(e).__name__.lower()
            msg = ("Clave incorrecta (o PDF no legible)" if "password" in nombre or "pdfminer" in nombre
                   or "password" in str(e).lower() else f"No se pudo leer ({type(e).__name__})")
            resultado.append({"f": f.filename, "e": msg, "l": 0, "n": 0, "notas": []}); continue
        nuevos, vistos, concil = 0, {}, 0
        for m in movs:
            k = f"{persona}|{banco}|{m['origen']}|{m['fecha']}|{m['descripcion']}|{m['monto']}"
            vistos[k] = vistos.get(k, 0) + 1
            h = hashlib.sha256(f"{k}|{vistos[k]}".encode()).hexdigest()
            try:
                cur = c.execute("""INSERT INTO movs(user_id,banco,fecha,descripcion,monto,categoria,origen,hash,archivo,estado,fuente)
                    VALUES(?,?,?,?,?,?,?,?,?,'confirmado','extracto')""", (persona, banco, m["fecha"], m["descripcion"], m["monto"],
                                                   m["categoria"], m["origen"], h, f.filename))
                nuevos += 1
                viejo = captura.conciliar(c, persona, banco, m["fecha"], m["monto"])
                if viejo:
                    concil += 1
                    c.execute("UPDATE deuda_pagos SET mov_id=? WHERE mov_id=?", (cur.lastrowid, viejo))
                    if c.execute("SELECT 1 FROM deuda_pagos WHERE mov_id=?", (cur.lastrowid,)).fetchone():
                        c.execute("UPDATE movs SET categoria='Deudas' WHERE id=?", (cur.lastrowid,))
                elif m["origen"] != "tarjeta":
                    deudas.registrar_automatico(c, m["descripcion"], m["monto"], m["fecha"], cur.lastrowid)
            except sqlite3.IntegrityError:
                pass  # duplicado: ya estaba cargado
        notas = []
        if info and "fecha_limite" in info:
            c.execute("""INSERT INTO pagos_tarjeta(user_id,banco,fecha_limite,pago_minimo,a_pagar,deuda_total,cupo,usado,disponible,corte)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,banco,fecha_limite) DO UPDATE SET
                pago_minimo=excluded.pago_minimo, a_pagar=excluded.a_pagar, deuda_total=excluded.deuda_total,
                cupo=excluded.cupo, usado=excluded.usado, disponible=excluded.disponible, corte=excluded.corte""",
                      (persona, banco, info["fecha_limite"], info["pago_minimo"], info["a_pagar"], info["deuda_total"],
                       info.get("cupo"), info.get("usado"), info.get("disponible"), info.get("corte")))
            costos = {k: v for k, v in (info.get("costos") or {}).items() if v}
            if costos:
                notas.append(("warn", "Costos de la tarjeta este corte: " + ", ".join(f"{k.lower()} {avisos.cop(v)}" for k, v in costos.items()) + "."))
            elif info.get("costos"):
                notas.append(("ok", "Sin intereses ni comisiones este corte."))
            cuentas.asegurar_tarjeta(c, persona, banco)
            nv, ac = deudas.sincronizar_tarjeta(c, persona, banco, movs, info["fecha_limite"])
            if nv or ac:
                notas.append(("ok", f"Compras a cuotas: {nv} nueva(s) y {ac} actualizada(s) en Deudas."))
            notas.append(("warn", f"Pago mínimo {avisos.cop(info['pago_minimo'] or 0)}, vence el {info['fecha_limite']}. Te avisaremos antes."))
        if info and info.get("final") is not None and info.get("cierre"):
            saldos.guardar(c, persona, banco, info["cierre"], info["final"], saldos.CORTE_EXTRACTO, "extracto")
            saldos.guardar_cajitas(c, persona, banco, info["cierre"], info)
            if info.get("rend_total"):        # los rendimientos no vienen como líneas del extracto: se registran como ingreso del mes
                hr = hashlib.sha256(f"rend|{persona}|{banco}|{info['cierre']}|{info['rend_total']}".encode()).hexdigest()
                try:
                    c.execute("""INSERT INTO movs(user_id,banco,fecha,descripcion,monto,categoria,origen,hash,archivo,estado,fuente)
                        VALUES(?,?,?,?,?,?,?,?,?,'confirmado','extracto')""", (persona, banco, info["cierre"], f"Rendimientos {banco}",
                                                                         info["rend_total"], "Ingresos", "cuenta", hr, f.filename))
                    notas.append(("ok", f"Rendimientos del mes: {avisos.cop(info['rend_total'])}."))
                except sqlite3.IntegrityError:
                    pass
            if not info.get("cajitas") and banco.lower().startswith("nu"):
                notas.append(("warn", "No encontré el desglose de cajitas en este extracto. Puedes escribir la tasa de tus cajitas en Resumen → Ajustar."))
            if info.get("cajitas"):
                cj = info["cajitas"]
                notas.append(("ok", f"Cajitas: {avisos.cop(cj['saldo'])} al {info['cierre'][8:]}/{info['cierre'][5:7]}, rendimiento {('%.2f' % cj['ea']).replace('.', ',')}% E.A."))
            notas.append(("ok", f"Saldo al {info['cierre'][8:]}/{info['cierre'][5:7]}: {avisos.cop(info['final'])}."))
        if info and "entro" in info:
            ent = sum(x["monto"] for x in movs if x["monto"] > 0)
            sal = -sum(x["monto"] for x in movs if x["monto"] < 0)
            if abs(ent - info["entro"]) < 1 and abs(sal - info["salio"]) < 1:
                notas.append(("ok", f"Cuadra con el extracto: entró {avisos.cop(info['entro'])} y salió {avisos.cop(info['salio'])}."))
            else:
                notas.append(("bad", f"No cuadra: el extracto dice entró {avisos.cop(info['entro'])} y salió {avisos.cop(info['salio'])}; "
                                     f"Cuadre leyó {avisos.cop(ent)} y {avisos.cop(sal)}. Revisa si falta alguna línea."))
        if info and info.get("gmf") is not None:
            q = c.execute("SELECT * FROM cuentas WHERE user_id=? AND nombre=?", (persona, banco)).fetchone()
            if info["gmf"] > 0:
                notas.append(("warn", f"El banco cobró {avisos.cop(info['gmf'])} de 4x1000 este mes." + (" Esta cuenta está marcada como exenta: revísalo." if q and q["exenta"] else "")))
            elif q and not q["exenta"]:
                notas.append(("ok", "El extracto no cobró 4x1000. Si esta cuenta está exenta, márcala así en Cuentas."))
        if concil:
            notas.append(("ok", f"{concil} movimientos que ya habías capturado quedaron confirmados por el extracto."))
        diag = ""
        if m_tipo_tarjeta(movs, info, tipo) and not (info and "fecha_limite" in info):
            notas.append(("bad", "No pude leer el resumen de la tarjeta (fecha límite, pago mínimo, deuda total). Abre «Ver cómo leí este PDF» y pásame el texto."))
        if not movs or (m_tipo_tarjeta(movs, info, tipo) and not (info and "fecha_limite" in info)):
            try:
                diag = lector.texto_pdf(datos, clave)[:7000]
            except Exception:
                diag = ""
        resultado.append({"f": f.filename, "e": "OK" if movs else "Sin movimientos reconocidos", "l": len(movs), "n": nuevos,
                          "notas": notas, "diag": diag})
    c.execute("INSERT OR REPLACE INTO config(clave,valor) VALUES('ultima_subida',?)", (avisos.ahora().isoformat(),))
    c.commit()
    users = c.execute("SELECT id,nombre FROM users ORDER BY nombre").fetchall()
    lista_c = cuentas.nombres(c, BANCOS)
    pares = [(r["user_id"], r["nombre"]) for r in c.execute("SELECT user_id,nombre FROM cuentas WHERE activa=1 ORDER BY nombre")]
    c.close()
    bg.add_task(avisos.revisar_presupuestos, db, PEM)
    return render(request, "subir.html", u=u, users=users, bancos=lista_c, pares=pares, resultado=resultado)


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


@app.post("/agenda/evento")
def nuevo_evento(request: Request, titulo: str = Form(...), fecha: str = Form(...), monto: str = Form(""), repite: str = Form("no"),
                 antes: int = Form(1)):
    u = user(request)
    try:
        f = date.fromisoformat(fecha)
    except ValueError:
        return volver(request, "/agenda", err="Elige la fecha")
    v = None
    if monto.strip():
        try:
            v = abs(captura.numero_es(monto))
        except ValueError:
            return volver(request, "/agenda", err="Escribe el valor en números")
    repite = repite if repite in ("no", "mensual", "anual") else "no"
    c = db()
    c.execute("INSERT INTO recordatorios(titulo,dia,antes,para,fecha,monto,repite) VALUES(?,?,?,?,?,?,?)",
              (titulo.strip()[:80], f.day, max(0, min(15, antes)), u["id"], f.isoformat(), v, repite))
    c.commit(); c.close()
    return volver(request, "/agenda", msg="Agregado a la agenda")


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


# ---------- Captura rápida ----------
@app.get("/rapido")
def rapido(request: Request, monto: str = "", desc: str = "", tipo: str = "gasto", medio: str = ""):
    u = user(request)
    return render(request, "rapido.html", u=u, pre={"monto": monto, "desc": desc, "tipo": tipo, "medio": medio}, abrir=True)


@app.post("/movimientos/rapido")
def guardar_rapido(request: Request, bg: BackgroundTasks, tipo: str = Form("gasto"), monto: str = Form(...),
                   descripcion: str = Form(""), categoria: str = Form("Auto"), medio: str = Form("Efectivo"),
                   fecha: str = Form(""), persona: int = Form(0), cuenta: str = Form("")):
    u = user(request)
    if cuenta:
        persona, medio = parse_cuenta(cuenta, u["id"])
    try:
        valor = abs(captura.numero_es(monto))
    except ValueError:
        return volver(request, "/", err="Escribe el valor en números")
    c = db()
    reglas = [(r["kw"], r["categoria"]) for r in c.execute("SELECT kw,categoria FROM reglas")]
    desc = descripcion.strip() or ("Ingreso" if tipo == "ingreso" else "Gasto")
    cat = lector.categoria(desc, reglas) if categoria == "Auto" else categoria
    efectivo = medio == "Efectivo"
    cuentas.asegurar(c, persona or u["id"], medio)
    if tipo == "retiro":
        if efectivo:
            c.close()
            return volver(request, "/", err="Para un retiro elige de qué banco sacaste el efectivo")
        dia = fecha or date.today().isoformat()
        for banco_, monto_, est in ((medio, -valor, "provisional"), ("Efectivo", valor, "confirmado")):
            hh = hashlib.sha256(f"retiro{secrets.token_hex(8)}".encode()).hexdigest()
            c.execute("""INSERT INTO movs(user_id,banco,fecha,descripcion,monto,categoria,origen,hash,estado,fuente)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (persona or u["id"], banco_, dia, "Retiro de efectivo", monto_, "Traslado propio",
                                                 "manual", hh, est, "manual"))
        c.commit(); c.close()
        from urllib.parse import urlparse
        destino = urlparse(request.headers.get("referer", "")).path or "/"
        return volver(request, "/" if destino in ("/rapido", "/login") else destino,
                      msg=f"Retiro anotado: sale de {medio} y entra a tu efectivo")
    h = hashlib.sha256(f"rapido{secrets.token_hex(8)}".encode()).hexdigest()
    c.execute("""INSERT INTO movs(user_id,banco,fecha,descripcion,monto,categoria,origen,hash,estado,fuente)
        VALUES(?,?,?,?,?,?,?,?,?,?)""", (persona or u["id"], medio, fecha or date.today().isoformat(), desc[:200],
                                         valor if tipo == "ingreso" else -valor, cat, "manual", h,
                                         "confirmado" if efectivo else "provisional", "manual"))
    mid = c.execute("SELECT last_insert_rowid() i").fetchone()["i"]
    pago_de = deudas.registrar_automatico(c, desc, valor if tipo == "ingreso" else -valor, fecha or date.today().isoformat(), mid)
    c.commit(); c.close()
    bg.add_task(avisos.revisar_presupuestos, db, PEM)
    msg = "Guardado" + (f" y registrado como pago de {pago_de}" if pago_de else "") + ("" if efectivo else ". Quedará confirmado cuando llegue el extracto")
    from urllib.parse import urlparse
    destino = urlparse(request.headers.get("referer", "")).path or "/"
    return volver(request, "/" if destino in ("/rapido", "/login") else destino, msg=msg)


# ---------- Captura automática ----------
def usuario_por_token(token):
    if not token:
        return None
    c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
    return u


@app.post("/api/captura")
async def api_captura(request: Request):
    """Recibe el texto de una notificación o correo. Autenticación: token personal (Authorization: Bearer ... o ?token=)."""
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("token", "")
    datos = {}
    try:
        tipo = request.headers.get("content-type", "")
        if "json" in tipo:
            datos = await request.json()
        elif "form" in tipo:
            datos = dict(await request.form())
        else:
            datos = {"texto": (await request.body()).decode("utf-8", "replace")}
    except Exception:
        return JSONResponse({"ok": False, "error": "No se pudo leer el cuerpo"}, 400)
    token = token or datos.get("token", "")
    u = usuario_por_token(token)
    if not u:
        return JSONResponse({"ok": False, "error": "Token inválido"}, 401)
    texto = str(datos.get("texto") or datos.get("text") or datos.get("body") or "")
    titulo = str(datos.get("titulo") or datos.get("title") or "")
    banco = str(datos.get("banco") or "")
    app_ = str(datos.get("app") or "")
    try:
        fecha_cap = date.fromisoformat(str(datos.get("fecha"))[:10]) if datos.get("fecha") else None
    except ValueError:
        fecha_cap = None
    if not banco and app_:
        banco = captura.banco_de(app_) if captura.banco_de(app_) != "Otro" else ""
    c = db()
    r = captura.procesar(c, u["id"], texto, titulo, banco, "notificacion", fecha_defecto=fecha_cap)
    c.close()
    return {"ok": True, **{k: v for k, v in r.items() if k in ("estado", "tipo", "monto", "descripcion", "fecha", "banco")}}


@app.get("/conectar")
def conectar(request: Request):
    u = user(request)
    c = db()
    d = dict(token=captura.token_de(c, u["id"]),
             revisar=c.execute("SELECT * FROM capturas WHERE estado='por_revisar' ORDER BY id DESC LIMIT 20").fetchall(),
             recientes_c=c.execute("""SELECT k.*, u.nombre FROM capturas k JOIN users u ON u.id=k.user_id
                WHERE k.estado IN ('creado','por_revisar','ignorado') ORDER BY k.id DESC LIMIT 10""").fetchall(),
             correo_estado=avisos.cfg(c, "correo_estado", ""), usuario=u["username"])
    c.close()
    base = f"{request.url.scheme}://{request.headers.get('host', '')}"
    return render(request, "conectar.html", u=u, url=f"{base}/api/captura", buzon=os.environ.get("IMAP_USER", ""),
                  imap=correo.configurado(), **d)


@app.post("/conectar/probar")
def probar_captura(request: Request, texto: str = Form(...), banco: str = Form("")):
    u = user(request)
    c = db()
    r = captura.procesar(c, u["id"], texto, "", banco, "pegado")
    c.close()
    est = r.get("estado")
    if est == "creado":
        return volver(request, "/conectar", msg=f"Entendido: {r['tipo']} de {avisos.cop(r['monto'])} en {r['descripcion']}. Quedó como pendiente")
    if est == "por_revisar":
        return volver(request, "/conectar#revisar", msg="Vi un valor pero no supe si es gasto o ingreso. Dime cuál es abajo")
    return volver(request, "/conectar", err="No encontré un valor ni un movimiento en ese texto")


@app.post("/conectar/token")
def nuevo_token(request: Request):
    u = user(request)
    c = db(); c.execute("UPDATE users SET token=? WHERE id=?", (secrets.token_urlsafe(24), u["id"])); c.commit(); c.close()
    return volver(request, "/conectar", msg="Token nuevo creado. Actualiza tus atajos")


@app.post("/capturas/{cid}/resolver")
def resolver_captura(request: Request, cid: int, tipo: str = Form(...)):
    user(request)
    c = db()
    k = c.execute("SELECT * FROM capturas WHERE id=? AND estado='por_revisar'", (cid,)).fetchone()
    if k and tipo in ("gasto", "ingreso"):
        reglas = [(r["kw"], r["categoria"]) for r in c.execute("SELECT kw,categoria FROM reglas")]
        mid = captura.insertar(c, k["user_id"], tipo, k["monto"], k["descripcion"], k["fecha"], k["banco"], k["fuente"], reglas)
        c.execute("UPDATE capturas SET estado='resuelto', mov_id=? WHERE id=?", (mid, cid))
        c.commit()
    c.close()
    return RedirectResponse("/conectar#revisar", 303)


@app.post("/capturas/{cid}/descartar")
def descartar_captura(request: Request, cid: int):
    user(request)
    c = db(); c.execute("UPDATE capturas SET estado='descartado' WHERE id=?", (cid,)); c.commit(); c.close()
    return RedirectResponse("/conectar#revisar", 303)


# ---------- Saldos ----------
@app.post("/saldos/ajustar")
def ajustar_saldo(request: Request, banco: str = Form(""), persona: int = Form(0), valor: str = Form(""), ea_cajitas: str = Form(""),
                  en_cajitas: str = Form(""), cuenta: str = Form("")):
    u_ = user(request)
    if cuenta:
        persona, banco = parse_cuenta(cuenta, u_["id"])
    persona = persona or u_["id"]
    if not banco:
        return volver(request, "/", err="Elige la cuenta")
    c = db()
    if en_cajitas.strip():
        try:
            caj = captura.numero_es(en_cajitas)
        except ValueError:
            c.close()
            return volver(request, "/", err="Escribe lo que tienes en cajitas en números")
        if not valor.strip():
            c.close()
            return volver(request, "/", err="Para indicar lo que tienes en cajitas escribe también el saldo total")
        c.execute("""INSERT INTO rend_cfg(user_id,banco,cajitas_base,base_fecha) VALUES(?,?,?,?)
            ON CONFLICT(user_id,banco) DO UPDATE SET cajitas_base=excluded.cajitas_base, base_fecha=excluded.base_fecha""",
                  (persona, banco, caj, avisos.ahora().date().isoformat()))
    if ea_cajitas.strip():
        try:
            ea = captura.numero_es(ea_cajitas.replace("%", ""))
        except ValueError:
            c.close()
            return volver(request, "/", err="Escribe la tasa en números, por ejemplo 9,85")
        c.execute("""INSERT INTO rend_cfg(user_id,banco,ea_actual) VALUES(?,?,?)
            ON CONFLICT(user_id,banco) DO UPDATE SET ea_actual=excluded.ea_actual""", (persona, banco, ea))
        if not valor.strip():
            c.commit(); c.close()
            return volver(request, "/", msg=f"Tasa de tus cajitas en {banco}: {('%.2f' % ea).replace('.', ',')}% E.A.")
    try:
        v = captura.numero_es(valor)
    except ValueError:
        c.close()
        return volver(request, "/", err="Escribe el saldo en números")
    corte = c.execute("SELECT COALESCE(MAX(id),0) m FROM movs").fetchone()["m"]
    saldos.guardar(c, persona, banco, avisos.ahora().date().isoformat(), v, corte, "manual")
    c.commit(); c.close()
    return volver(request, "/", msg=f"Saldo de {banco} ajustado a {avisos.cop(v)}")


@app.post("/saldos/{sid}/borrar")
def borrar_saldo(request: Request, sid: int):
    user(request)
    c = db(); c.execute("DELETE FROM saldos WHERE id=?", (sid,)); c.commit(); c.close()
    return RedirectResponse("/", 303)


# ---------- Deudas ----------
def cargar_deudas(c):
    out = []
    for d in c.execute("""SELECT d.*, u.nombre AS responsable FROM deudas d JOIN users u ON u.id=d.user_id
                          WHERE d.activo=1 ORDER BY d.direccion DESC, d.id""").fetchall():
        pagos = c.execute("SELECT * FROM deuda_pagos WHERE deuda_id=? ORDER BY fecha, id", (d["id"],)).fetchall()
        out.append({"d": d, "e": deudas.estado(d, pagos), "pagos": pagos})
    return out


def cargar_tarjetas(c, todas):
    """Una línea por tarjeta (el último extracto) con sus compras a cuotas."""
    out = []
    for t in c.execute("""SELECT p.*, u.nombre AS responsable FROM pagos_tarjeta p JOIN users u ON u.id=p.user_id
        WHERE p.id=(SELECT id FROM pagos_tarjeta x WHERE x.user_id=p.user_id AND x.banco=p.banco ORDER BY x.fecha_limite DESC, x.id DESC LIMIT 1)
        ORDER BY u.nombre, p.banco""").fetchall():
        cuotas = [x for x in todas if x["d"]["origen"] == "tarjeta" and x["d"]["user_id"] == t["user_id"]
                  and not x["e"]["pagada"]]
        dias = (date.fromisoformat(t["fecha_limite"]) - date.today()).days
        uso = (t["usado"] / t["cupo"]) if t["cupo"] and t["usado"] is not None else None
        out.append({"t": t, "cuotas": cuotas, "dias": dias, "uso": uso, "manejo": cuentas.manejo_tarjeta(c, t["user_id"], t["banco"]),
                    "ficha": cuentas.ficha_tarjeta(c, t["user_id"], t["banco"]), "ciclo": cuentas.ciclo_tarjeta(c, t["user_id"], t["banco"])})
    return out


def resumen_deudas(c):
    todas = cargar_deudas(c)
    tarjetas = cargar_tarjetas(c, todas)
    if not todas and not tarjetas:
        return None
    manual = [x for x in todas if x["d"]["origen"] != "tarjeta"]
    debo = [x for x in manual if x["d"]["direccion"] == "debo" and not x["e"]["pagada"]]
    tarj = [x for x in tarjetas if not x["t"]["pagado"]]
    prox = sorted([x for x in debo if x["e"]["proxima"]], key=lambda x: x["e"]["proxima"])
    return {"n": len(debo) + len(tarj),
            "saldo": sum(x["e"]["saldo"] for x in debo) + sum((x["t"]["deuda_total"] or 0) for x in tarj),
            "cuotas": sum(x["e"]["cuota"] for x in debo) + sum((x["t"]["a_pagar"] or x["t"]["pago_minimo"] or 0) for x in tarj),
            "tarjeta": min(tarj, key=lambda x: x["t"]["fecha_limite"]) if tarj else None,
            "proxima": prox[0] if prox else None,
            "me_deben": sum(x["e"]["saldo"] for x in manual if x["d"]["direccion"] == "me_deben" and not x["e"]["pagada"])}


@app.get("/deudas")
def pagina_deudas(request: Request, extra: str = ""):
    u = user(request)
    c = db()
    todas = cargar_deudas(c)
    tarjetas = cargar_tarjetas(c, todas)
    users = c.execute("SELECT id,nombre FROM users ORDER BY nombre").fetchall()
    res = resumen_deudas(c)
    c.close()
    todas = [x for x in todas if x["d"]["origen"] != "tarjeta"]
    debo = [x for x in todas if x["d"]["direccion"] == "debo"]
    activas = [x for x in debo if not x["e"]["pagada"]]
    try:
        extra_v = captura.numero_es(extra) if extra else 0.0
    except ValueError:
        extra_v = 0.0
    est = None
    if activas:
        est = deudas.estrategia([{"nombre": x["d"]["nombre"], "saldo": x["e"]["saldo"], "i": x["e"]["i"], "ea": x["e"]["ea"],
                                  "cuota": x["e"]["cuota"]} for x in activas], extra_v)
    return render(request, "deudas.html", u=u, debo=debo, me_deben=[x for x in todas if x["d"]["direccion"] == "me_deben"],
                  res=res, est=est, extra=extra_v, tarjetas=tarjetas, usuarios_d=users, hoy=date.today().isoformat(),
                  interes_total=sum(x["e"]["int_restante"] for x in activas))


@app.post("/deudas")
def crear_deuda(request: Request, nombre: str = Form(...), tipo: str = Form("Préstamo"), direccion: str = Form("debo"),
                principal: str = Form(...), n_cuotas: int = Form(...), cuota: str = Form(""), tasa_ea: str = Form(""),
                primera_cuota: str = Form(...), palabra_clave: str = Form(""), persona: int = Form(0)):
    u = user(request)
    try:
        P = captura.numero_es(principal)
        q = captura.numero_es(cuota) if cuota.strip() else None
        ea = captura.numero_es(tasa_ea.replace("%", "")) if tasa_ea.strip() else None
    except ValueError:
        return volver(request, "/deudas", err="Revisa los números: valor prestado, cuota o E.A.")
    if P <= 0 or n_cuotas < 1:
        return volver(request, "/deudas", err="El valor prestado y el número de cuotas deben ser mayores que cero")
    c = db()
    c.execute("""INSERT INTO deudas(user_id,nombre,tipo,direccion,principal,n_cuotas,cuota,tasa_ea,primera_cuota,palabra_clave)
        VALUES(?,?,?,?,?,?,?,?,?,?)""", (persona or u["id"], nombre.strip()[:80], tipo, direccion, P, n_cuotas, q, ea,
                                         primera_cuota, palabra_clave.strip().lower()[:60]))
    did = c.execute("SELECT last_insert_rowid() i").fetchone()["i"]
    c.commit(); c.close()
    return volver(request, f"/deudas/{did}", msg="Deuda guardada")


@app.get("/deudas/{did}")
def detalle_deuda(request: Request, did: int):
    u = user(request)
    c = db()
    d = c.execute("SELECT d.*, u.nombre AS responsable FROM deudas d JOIN users u ON u.id=d.user_id WHERE d.id=?", (did,)).fetchone()
    if not d:
        c.close()
        return RedirectResponse("/deudas", 303)
    pagos = c.execute("SELECT * FROM deuda_pagos WHERE deuda_id=? ORDER BY fecha DESC, id DESC", (did,)).fetchall()
    c.close()
    e = deudas.estado(d, pagos)
    return render(request, "deuda.html", u=u, d=d, e=e, pagos=pagos, hoy=date.today().isoformat())


@app.post("/deudas/{did}/pago")
def pago_deuda(request: Request, did: int, monto: str = Form(...), fecha: str = Form(""), tipo: str = Form("cuota")):
    user(request)
    try:
        v = abs(captura.numero_es(monto))
    except ValueError:
        return volver(request, f"/deudas/{did}", err="Escribe el valor en números")
    c = db()
    c.execute("INSERT INTO deuda_pagos(deuda_id,fecha,monto,tipo,nota) VALUES(?,?,?,?,?)",
              (did, fecha or date.today().isoformat(), v, "abono" if tipo == "abono" else "cuota", "Registrado a mano"))
    c.commit(); c.close()
    return volver(request, f"/deudas/{did}", msg="Pago registrado")


@app.post("/deudas/{did}/pago/{pid}/borrar")
def borrar_pago_deuda(request: Request, did: int, pid: int):
    user(request)
    c = db(); c.execute("DELETE FROM deuda_pagos WHERE id=? AND deuda_id=?", (pid, did)); c.commit(); c.close()
    return RedirectResponse(f"/deudas/{did}", 303)


@app.post("/deudas/{did}/borrar")
def borrar_deuda(request: Request, did: int):
    user(request)
    c = db()
    c.execute("DELETE FROM deuda_pagos WHERE deuda_id=?", (did,)); c.execute("DELETE FROM deudas WHERE id=?", (did,))
    c.commit(); c.close()
    return volver(request, "/deudas", msg="Deuda eliminada")


# ---------- Cuentas ----------
@app.get("/cuentas")
def pagina_cuentas(request: Request):
    u = user(request)
    c = db()
    lista = cuentas.listado(c, date.today().strftime("%Y-%m"), saldos.actuales(c))
    users = c.execute("SELECT id,nombre FROM users ORDER BY nombre").fetchall()
    c.close()
    return render(request, "cuentas.html", u=u, lista=lista, usuarios_c=users, tipos=cuentas.TIPOS, aviso=cuentas.avisos_exencion(lista),
                  gmf_total=sum(x["gmf"] for x in lista), costo_total=sum(x["costo"] for x in lista))


def _cuenta_form(nombre, entidad, tipo, exenta, ultimos4, manejo="", cond="siempre"):
    try:
        mj = abs(captura.numero_es(manejo)) if manejo.strip() else 0.0
    except ValueError:
        mj = 0.0
    return (nombre.strip()[:60], (entidad.strip() or nombre.strip())[:60], tipo if tipo in cuentas.TIPOS else "ahorros",
            1 if exenta else 0, "".join(ch for ch in ultimos4 if ch.isdigit())[:4], mj, cond if cond in ("siempre", "sin_uso") else "siempre")


@app.post("/cuentas")
def crear_cuenta(request: Request, nombre: str = Form(...), entidad: str = Form(""), tipo: str = Form("ahorros"),
                 exenta: str = Form(""), ultimos4: str = Form(""), cuota_manejo: str = Form(""), manejo_cond: str = Form("siempre"),
                 persona: int = Form(0)):
    u = user(request)
    n, e, t, ex, l4, mj, mc = _cuenta_form(nombre, entidad, tipo, exenta, ultimos4, cuota_manejo, manejo_cond)
    if not n or n in cuentas.RESERVADOS:
        return volver(request, "/cuentas", err="Ponle un nombre a la cuenta")
    c = db()
    try:
        c.execute("INSERT INTO cuentas(user_id,nombre,entidad,tipo,exenta,ultimos4,cuota_manejo,manejo_cond) VALUES(?,?,?,?,?,?,?,?)",
                  (persona or u["id"], n, e, t, ex, l4, mj, mc))
        c.commit()
    except sqlite3.IntegrityError:
        c.close()
        return volver(request, "/cuentas", err="Esa persona ya tiene una cuenta con ese nombre")
    c.close()
    return volver(request, "/cuentas", msg="Cuenta creada")


@app.post("/cuentas/{cid}")
def editar_cuenta(request: Request, cid: int, nombre: str = Form(...), entidad: str = Form(""), tipo: str = Form("ahorros"),
                  exenta: str = Form(""), ultimos4: str = Form(""), cuota_manejo: str = Form(""), manejo_cond: str = Form("siempre")):
    user(request)
    n, e, t, ex, l4, mj, mc = _cuenta_form(nombre, entidad, tipo, exenta, ultimos4, cuota_manejo, manejo_cond)
    c = db()
    viejo = c.execute("SELECT * FROM cuentas WHERE id=?", (cid,)).fetchone()
    if not viejo or not n:
        c.close()
        return volver(request, "/cuentas", err="No se encontró la cuenta")
    try:
        c.execute("UPDATE cuentas SET nombre=?, entidad=?, tipo=?, exenta=?, ultimos4=?, cuota_manejo=?, manejo_cond=? WHERE id=?", (n, e, t, ex, l4, mj, mc, cid))
        if n != viejo["nombre"]:     # el nombre es la llave de los movimientos y saldos de esa cuenta
            for tabla in ("movs", "saldos", "pagos_tarjeta"):
                c.execute(f"UPDATE {tabla} SET banco=? WHERE user_id=? AND banco=?", (n, viejo["user_id"], viejo["nombre"]))
        c.commit()
    except sqlite3.IntegrityError:
        c.close()
        return volver(request, "/cuentas", err="Ya existe una cuenta con ese nombre")
    c.close()
    return volver(request, "/cuentas", msg="Cuenta actualizada")


@app.post("/cuentas/{cid}/borrar")
def borrar_cuenta(request: Request, cid: int):
    user(request)
    c = db()
    q = c.execute("SELECT * FROM cuentas WHERE id=?", (cid,)).fetchone()
    if q:
        hay = c.execute("SELECT 1 FROM movs WHERE user_id=? AND banco=? LIMIT 1", (q["user_id"], q["nombre"])).fetchone()
        if hay:
            c.execute("UPDATE cuentas SET activa=0 WHERE id=?", (cid,))      # tiene historial: se archiva
        else:
            c.execute("DELETE FROM cuentas WHERE id=?", (cid,))
        c.commit()
    c.close()
    return volver(request, "/cuentas", msg="Cuenta quitada")


# ---------- Metas ----------
def cargar_metas(c):
    out = []
    for m in c.execute("""SELECT m.*, u.nombre AS responsable FROM metas m JOIN users u ON u.id=m.user_id
                          WHERE m.activa=1 ORDER BY m.periodo, m.id""").fetchall():
        aps = c.execute("SELECT * FROM meta_aportes WHERE meta_id=? ORDER BY fecha DESC, id DESC", (m["id"],)).fetchall()
        out.append({"m": m, "e": metas.estado_meta(m, aps), "aportes": aps})
    return out


@app.get("/metas")
def pagina_metas(request: Request):
    u = user(request)
    c = db()
    lista = cargar_metas(c)
    users = c.execute("SELECT id,nombre FROM users ORDER BY nombre").fetchall()
    c.close()
    return render(request, "metas.html", u=u, lista=lista, usuarios_m=users, hoy=date.today().isoformat())


@app.post("/metas")
def crear_meta(request: Request, nombre: str = Form(...), objetivo: str = Form(...), periodo: str = Form("total"),
               fecha_limite: str = Form(""), bono_ea: str = Form(""), banco: str = Form(""), persona: int = Form(0)):
    u = user(request)
    try:
        obj = abs(captura.numero_es(objetivo))
        bono = abs(captura.numero_es(bono_ea.replace("%", ""))) if bono_ea.strip() else 0.0
    except ValueError:
        return volver(request, "/metas", err="Revisa los números de la meta")
    if obj <= 0:
        return volver(request, "/metas", err="La meta debe ser mayor que cero")
    c = db()
    c.execute("""INSERT INTO metas(user_id,nombre,objetivo,periodo,fecha_limite,bono_ea,banco) VALUES(?,?,?,?,?,?,?)""",
              (persona or u["id"], nombre.strip()[:80], obj, "mensual" if periodo == "mensual" else "total",
               fecha_limite or None, bono, banco.strip()[:40]))
    c.commit(); c.close()
    return volver(request, "/metas", msg="Meta creada")


@app.post("/metas/{mid}/aporte")
def aportar_meta(request: Request, mid: int, monto: str = Form(...), fecha: str = Form("")):
    user(request)
    try:
        v = captura.numero_es(monto)
    except ValueError:
        return volver(request, "/metas", err="Escribe el valor en números")
    c = db()
    c.execute("INSERT INTO meta_aportes(meta_id,fecha,monto,nota) VALUES(?,?,?,?)",
              (mid, fecha or date.today().isoformat(), v, "Aporte" if v >= 0 else "Retiro"))
    c.commit(); c.close()
    return volver(request, "/metas", msg="Aporte registrado")


@app.post("/metas/{mid}/aporte/{aid}/borrar")
def borrar_aporte(request: Request, mid: int, aid: int):
    user(request)
    c = db(); c.execute("DELETE FROM meta_aportes WHERE id=? AND meta_id=?", (aid, mid)); c.commit(); c.close()
    return RedirectResponse("/metas", 303)


@app.post("/metas/{mid}/borrar")
def borrar_meta(request: Request, mid: int):
    user(request)
    c = db()
    c.execute("DELETE FROM meta_aportes WHERE meta_id=?", (mid,)); c.execute("DELETE FROM metas WHERE id=?", (mid,))
    c.commit(); c.close()
    return volver(request, "/metas", msg="Meta eliminada")


@app.post("/disponible/horizonte")
def fijar_horizonte(request: Request, dia: str = Form("")):
    user(request)
    c = db()
    try:
        d = max(1, min(31, int(dia))) if dia.strip() else 0
    except ValueError:
        d = 0
    c.execute("INSERT OR REPLACE INTO config(clave,valor) VALUES('horizonte_dia',?)", (str(d or ""),))
    c.commit(); c.close()
    return RedirectResponse("/", 303)


# ---------- Agenda ----------
@app.get("/agenda")
def pagina_agenda(request: Request, dias: int = 60):
    u = user(request)
    c = db()
    ev = agenda.eventos(c, date.today(), max(7, min(dias, 180)))
    c.close()
    return render(request, "agenda.html", u=u, grupos=agenda.agrupar(ev), semana=agenda.por_pagar(ev, 7), mes=agenda.por_pagar(ev, 30),
                  total=len(ev), dias=max(7, min(dias, 180)))


# ---------- Asesor con IA ----------
def _guardar_hist(c, user_id, modo, pregunta, respuesta, firma="", tok=(0, 0)):
    cur = c.execute("INSERT INTO asesor_hist(user_id,modo,pregunta,respuesta,creado,firma,tok_in,tok_out) VALUES(?,?,?,?,?,?,?,?)",
                    (user_id, modo, pregunta, respuesta, avisos.ahora().isoformat(), firma, tok[0], tok[1]))
    if modo in asesor.MODOS:
        asesor.guardar_acciones(c, user_id, asesor.extraer_acciones(respuesta), cur.lastrowid, avisos.ahora().isoformat())
    c.commit()


@app.get("/asesor")
def pagina_asesor(request: Request):
    u = user(request)
    c = db()
    ultimos = {}
    for md in asesor.MODOS:
        h = c.execute("SELECT * FROM asesor_hist WHERE modo=? ORDER BY id DESC LIMIT 1", (md,)).fetchone()
        ultimos[md] = {"h": h, "p": asesor.partes(h["respuesta"]) if h else None}
    ctx_actual = asesor.firma(asesor.contexto(c)) if asesor.hay_clave() else ""
    for md, x in ultimos.items():
        x["vigente"] = bool(x["h"] and x["h"]["firma"] and x["h"]["firma"] == ctx_actual)
    chat = list(reversed(c.execute("SELECT * FROM asesor_hist WHERE user_id=? AND modo='chat' ORDER BY id DESC LIMIT 8", (u["id"],)).fetchall()))
    acciones = c.execute("SELECT * FROM asesor_acciones ORDER BY estado DESC, id DESC LIMIT 14").fetchall()
    usos = asesor.usos_hoy(c, u["id"])
    gasto = asesor.gasto_mes(c)
    c.close()
    return render(request, "asesor.html", u=u, clave=asesor.hay_clave(), ultimos=ultimos, chat=chat, acciones=acciones, usos=usos,
                  max_dia=asesor.MAX_DIA, modelo=asesor.MODELO, gasto=gasto)


def _llamar_asesor(request, u, modo, mensajes, effort, max_tokens, pregunta, firma=""):
    c = db()
    try:
        if not asesor.hay_clave():
            return volver(request, "/asesor", err="Falta la clave de Anthropic (ANTHROPIC_API_KEY) en la configuración del servidor")
        if asesor.usos_hoy(c, u["id"]) >= asesor.MAX_DIA:
            return volver(request, "/asesor", err=f"Llegaste al límite de {asesor.MAX_DIA} consultas de hoy. Mañana sigues")
        try:
            texto, aviso, tok = asesor.consultar(mensajes, effort, max_tokens)
        except Exception as e:
            print("asesor:", type(e).__name__, str(e)[:200], flush=True)
            return volver(request, "/asesor", err=asesor.mensaje_de_error(e))
        if not texto:
            return volver(request, "/asesor", err=aviso or "El asistente no devolvió texto. Intenta de nuevo")
        _guardar_hist(c, u["id"], modo, pregunta, texto, firma, tok)
        return volver(request, "/asesor", msg=aviso or "Listo")
    finally:
        c.close()


@app.post("/asesor/analizar")
def asesor_analizar(request: Request, modo: str = Form("analisis"), forzar: str = Form("")):
    u = user(request)
    if modo not in asesor.MODOS:
        modo = "analisis"
    instruccion, effort, max_tokens = asesor.MODOS[modo]
    c = db()
    ctx = asesor.contexto(c)
    fx = asesor.firma(ctx)
    if not forzar:                 # si nada cambió desde la última vez, se reutiliza la respuesta guardada: sin costo
        ya = c.execute("SELECT id FROM asesor_hist WHERE modo=? AND firma=? ORDER BY id DESC LIMIT 1", (modo, fx)).fetchone()
        if ya:
            c.close()
            return volver(request, "/asesor", msg="Tus cifras no han cambiado: te muestro el consejo guardado (sin gastar IA)")
    mem = asesor.memoria(c, u["id"])
    c.close()
    contenido = f"{instruccion}\n\nContexto de nuestras finanzas:\n{ctx}" + (f"\n\n{mem}" if mem else "")
    return _llamar_asesor(request, u, modo, [{"role": "user", "content": contenido}], effort, max_tokens, instruccion, fx)


@app.post("/asesor/preguntar")
def asesor_preguntar(request: Request, pregunta: str = Form(...)):
    u = user(request)
    pregunta = pregunta.strip()[:600]
    if not pregunta:
        return volver(request, "/asesor", err="Escribe tu pregunta")
    c = db()
    ctx = asesor.contexto(c)
    fx = asesor.firma(ctx)
    ya = c.execute("SELECT id FROM asesor_hist WHERE modo='chat' AND firma=? AND lower(pregunta)=lower(?) ORDER BY id DESC LIMIT 1", (fx, pregunta)).fetchone()
    if ya:                         # misma pregunta con las mismas cifras: se copia la respuesta guardada
        r = c.execute("SELECT respuesta FROM asesor_hist WHERE id=?", (ya["id"],)).fetchone()
        c.execute("INSERT INTO asesor_hist(user_id,modo,pregunta,respuesta,creado,firma) VALUES(?,?,?,?,?,?)",
                  (u["id"], "chat", pregunta, r["respuesta"], avisos.ahora().isoformat(), fx))
        c.commit(); c.close()
        return volver(request, "/asesor", msg="Ya habías hecho esa pregunta con estas mismas cifras: te muestro la respuesta guardada (sin gastar IA)")
    mem = asesor.memoria(c, u["id"])
    previos = list(reversed(c.execute("SELECT pregunta,respuesta FROM asesor_hist WHERE user_id=? AND modo='chat' AND tok_in>0 ORDER BY id DESC LIMIT 2",
                                      (u["id"],)).fetchall()))
    c.close()
    msgs = []
    for p in previos:
        msgs += [{"role": "user", "content": p["pregunta"]}, {"role": "assistant", "content": p["respuesta"][:900]}]
    msgs.append({"role": "user", "content": f"Contexto de nuestras finanzas:\n{ctx}" + (f"\n\n{mem}" if mem else "") + f"\n\nMi pregunta: {pregunta}"})
    return _llamar_asesor(request, u, "chat", msgs, "low", 3000, pregunta, fx)


@app.post("/asesor/accion/{aid}")
def asesor_accion(request: Request, aid: int):
    user(request)
    c = db()
    c.execute("UPDATE asesor_acciones SET estado=CASE estado WHEN 'pendiente' THEN 'hecha' ELSE 'pendiente' END WHERE id=?", (aid,))
    c.commit(); c.close()
    return RedirectResponse("/asesor#plan", 303)


@app.post("/asesor/acciones/limpiar")
def asesor_limpiar(request: Request):
    user(request)
    c = db(); c.execute("DELETE FROM asesor_acciones WHERE estado='hecha'"); c.commit(); c.close()
    return RedirectResponse("/asesor#plan", 303)


@app.post("/asesor/borrar")
def asesor_borrar(request: Request):
    u = user(request)
    c = db(); c.execute("DELETE FROM asesor_hist WHERE user_id=? AND modo='chat'", (u["id"],)); c.commit(); c.close()
    return RedirectResponse("/asesor", 303)


# ---------- Ingresos fijos ----------
@app.get("/ingresos")
def pagina_ingresos(request: Request):
    u = user(request)
    c = db()
    filas = c.execute("""SELECT i.*, u.nombre AS responsable FROM ingresos_fijos i JOIN users u ON u.id=i.user_id
        WHERE i.activo=1 ORDER BY i.dia""").fetchall()
    hoy = date.today()
    lista = []
    for r in filas:
        y, mm = (hoy.year, hoy.month)
        f = date(y, mm, min(r["dia"], calendar.monthrange(y, mm)[1]))
        if f < hoy:
            y, mm = (hoy.year + hoy.month // 12, hoy.month % 12 + 1)
            f = date(y, mm, min(r["dia"], calendar.monthrange(y, mm)[1]))
        lista.append({"r": r, "prox": f, "dias": (f - hoy).days})
    users = c.execute("SELECT id,nombre FROM users ORDER BY nombre").fetchall()
    total = sum(x["r"]["monto"] for x in lista)
    c.close()
    return render(request, "ingresos.html", u=u, lista=lista, total=total, usuarios_i=users, hoy=hoy.isoformat())


@app.post("/ingresos")
def crear_ingreso(request: Request, nombre: str = Form(...), monto: str = Form(...), dia: int = Form(...), banco: str = Form("Nu"),
                  persona: int = Form(0), cuenta: str = Form("")):
    u = user(request)
    if cuenta:
        persona, banco = parse_cuenta(cuenta, u["id"])
    try:
        v = abs(captura.numero_es(monto))
    except ValueError:
        return volver(request, "/ingresos", err="Escribe el valor en números")
    c = db()
    c.execute("INSERT INTO ingresos_fijos(user_id,nombre,monto,dia,banco) VALUES(?,?,?,?,?)",
              (persona or u["id"], nombre.strip()[:60], v, max(1, min(31, dia)), banco.strip()[:40]))
    c.commit(); c.close()
    return volver(request, "/ingresos", msg="Ingreso guardado")


@app.post("/ingresos/{iid}/llego")
def ingreso_llego(request: Request, iid: int):
    """Registra que el ingreso ya llegó: queda como movimiento pendiente que el extracto confirma."""
    user(request)
    c = db()
    r = c.execute("SELECT * FROM ingresos_fijos WHERE id=?", (iid,)).fetchone()
    if r:
        cuentas.asegurar(c, r["user_id"], r["banco"] or "Nu")
        captura.insertar(c, r["user_id"], "ingreso", r["monto"], r["nombre"], date.today().isoformat(), r["banco"] or "Nu", "manual",
                         [], "provisional")
        c.commit()
    c.close()
    return volver(request, "/ingresos", msg="Registrado: quedó como ingreso pendiente de confirmar con el extracto")


@app.post("/ingresos/{iid}/borrar")
def borrar_ingreso(request: Request, iid: int):
    user(request)
    c = db(); c.execute("DELETE FROM ingresos_fijos WHERE id=?", (iid,)); c.commit(); c.close()
    return volver(request, "/ingresos", msg="Ingreso quitado")
