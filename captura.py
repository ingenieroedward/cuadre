"""Captura de movimientos en el momento: notificaciones del celular y correos de alerta.
Todo lo capturado queda como 'provisional' hasta que llegue el extracto y lo concilie."""
import hashlib
import re
import secrets
import sqlite3
import unicodedata
from datetime import date, timedelta

import avisos
import cuentas
import deudas
import lector

MESES_N = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12}
RE_MONTO = re.compile(r"\$\s*(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:,\d{1,2})?)")
GASTO = ["compraste", "pagaste", "enviaste", "transferiste", "retiraste", "hiciste un pago", "pago exitoso", "listo tu pago",
         "abonaste", "compra aprobada", "compra exitosa", "se realizo una compra", "giraste", "retiro de", "compra por",
         "pago a ", "pago en "]
INGRESO = ["recibiste", "te enviaron", "te transfirieron", "venta exitosa", "deposito", "consignacion", "te abonaron",
           "te consignaron", "nomina", "reembolso", "devolucion", "abono recibido"]
IGNORAR = ["intento de compra", "confirma", "codigo", "clave dinamica", "te ofrecemos", "promocion", "oferta"]
BANCOS_TXT = [("nequi", "Nequi"), ("bancolombia", "Bancolombia"), ("davivienda", "Davivienda"), ("daviplata", "Daviplata"),
              ("bbva", "BBVA"), ("nubank", "Nu"), ("nu colombia", "Nu")]


def sin(t):
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn").lower()


def numero_es(txt):
    """'29.900' -> 29900, '29.900,50' -> 29900.5, '$ 12000' -> 12000, '12,5' -> 12.5"""
    t = re.sub(r"[^\d.,-]", "", txt)
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    return float(t)


def a_monto(txt):
    return float(txt.replace(".", "").replace(",", "."))


def parse_fecha(t, hoy):
    t = re.sub(r"cuota del [^.]*?(20\d{2})", "", t, flags=re.I)
    m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", t)
    if m:
        return lector._fecha(int(m[1]), int(m[2]), int(m[3]))
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", t)
    if m:
        return lector._fecha(int(m[3]), int(m[2]), int(m[1]))
    m = re.search(r"\b(\d{1,2})\s*(?:de\s+|/|-)\s*([A-Za-zñÑáéíóúÁÉÍÓÚ]{3,10})\.?\s*(?:de\s+|/|-)\s*(20\d{2})\b", t)
    if m and sin(m[2])[:3] in MESES_N:
        return lector._fecha(int(m[3]), MESES_N[sin(m[2])[:3]], int(m[1]))
    return hoy.isoformat()


def banco_de(texto, banco=""):
    if banco:
        return banco
    n = re.sub(r"banco:\s*\S+", "", sin(texto))
    for k, v in BANCOS_TXT:
        if k in n or (k == "nubank" and re.search(r"\bnu\b", n)):
            return v
    return "Otro"


def descripcion(texto, titulo=""):
    t = re.sub(r"\s+", " ", texto).strip()
    pats = [
        (r"Pagador:\s*(.+?)\s+Banco:", "Recibiste de {}"),
        (r"(?:hiciste un pago|listo tu pago|pago exitoso)\s+en\s+(.+?)(?:\s+por\s+\$|\s+Pagaste|\s+con\s+Nequi|\s*\$|[.,]|$)", "{}"),
        (r"compra[^$]{0,40}?\$\s*[\d.,]+\s+en\s+(.+?)(?:\s+con\b|\s+el\s+\d|\s+a\s+las|\.|,|$)", "{}"),
        (r"compra[^$]{0,60}?\ben\s+(.+?)\s+por\s+\$", "{}"),
        (r"(?:enviaste|transferiste)\s+(?:\$\s*[\d.,]+\s+)?a\s+(.+?)(?:\s+\$|\.|,|$)", "Enviaste a {}"),
        (r"recibiste\s+(?:\$\s*[\d.,]+\s+)?de\s+(.+?)(?:\s+\$|\.|,|$)", "Recibiste de {}"),
        (r"pagaste\s+(?:\$\s*[\d.,]+\s+)?a\s+(.+?)(?:\s+\$|\.|,|$)", "Pago a {}"),
        (r"pago\s+a\s+(.+?)(?:\s+por\s+\$|\s+\$|\.|,|$)", "Pago a {}"),
    ]
    for rx, fmt in pats:
        m = re.search(rx, t, re.I)
        if m and m[1].strip():
            x = re.split(r"\s+(?:\d{1,2}/\d{1,2}/\d{2,4}|el\s+\d|a\s+las\s+\d)", m[1].strip())[0].strip()
            return fmt.format(x)[:120]
    if re.search(r"cr[eé]dito nequi", t, re.I):
        return "Cuota crédito Nequi"
    return (titulo.strip() or "Movimiento")[:80]


def interpretar(texto, titulo="", banco="", hoy=None):
    """Devuelve {'estado','tipo','monto','descripcion','fecha','banco'}; estado: creado / por_revisar / ignorado."""
    hoy = hoy or avisos.ahora().date()
    completo = f"{titulo} {texto}".strip()
    n = sin(completo)
    montos = RE_MONTO.findall(completo)
    if not montos or any(k in n for k in IGNORAR):
        return {"estado": "ignorado"}
    monto = a_monto(montos[0])
    g = [n.find(k) for k in GASTO if k in n]
    i = [n.find(k) for k in INGRESO if k in n]
    base = {"monto": monto, "descripcion": descripcion(texto, titulo), "fecha": parse_fecha(completo, hoy), "banco": banco_de(completo, banco)}
    if re.search(r"tarjeta\s+(de\s+)?cr[eé]dito|nucontrol|tarjeta morada", n) and base["banco"] not in ("Otro",):
        base["banco"] = f"Tarjeta {base['banco']}"       # compra con la tarjeta de crédito, no con la cuenta
    if g and (not i or min(g) < min(i)):
        return {"estado": "creado", "tipo": "gasto", **base}
    if i:
        return {"estado": "creado", "tipo": "ingreso", **base}
    return {"estado": "por_revisar", "tipo": "", **base}


def token_de(c, user_id):
    r = c.execute("SELECT token FROM users WHERE id=?", (user_id,)).fetchone()
    if r and r["token"]:
        return r["token"]
    t = secrets.token_urlsafe(24)
    c.execute("UPDATE users SET token=? WHERE id=?", (t, user_id))
    c.commit()
    return t


def insertar(c, user_id, tipo, monto, desc, fecha, banco, fuente, reglas, estado="provisional"):
    cat = lector.categoria(desc, reglas)
    monto = -abs(monto) if tipo == "gasto" else abs(monto)
    h = hashlib.sha256(f"cap|{user_id}|{secrets.token_hex(8)}".encode()).hexdigest()
    cur = c.execute("""INSERT INTO movs(user_id,banco,fecha,descripcion,monto,categoria,origen,hash,estado,fuente)
        VALUES(?,?,?,?,?,?,?,?,?,?)""", (user_id, banco, fecha, desc[:200], monto, cat, "captura", h, estado, fuente))
    return cur.lastrowid


def procesar(c, user_id, texto, titulo="", banco="", fuente="notificacion", fecha_defecto=None):
    """Procesa un texto capturado. Devuelve dict con estado y el movimiento creado (si lo hubo)."""
    texto = (texto or "").strip()
    if not texto and not titulo:
        return {"estado": "ignorado", "motivo": "vacío"}
    ya = c.execute("""SELECT id FROM capturas WHERE user_id=? AND texto=? AND creado > ?""",
                   (user_id, texto, (avisos.ahora() - timedelta(minutes=10)).isoformat())).fetchone()
    if ya:
        return {"estado": "duplicado"}
    reglas = [(r["kw"], r["categoria"]) for r in c.execute("SELECT kw,categoria FROM reglas")]
    r = interpretar(texto, titulo, banco, hoy=fecha_defecto)
    mov_id = None
    if r.get("banco"):
        r["banco"] = cuentas.resolver(c, user_id, r["banco"])
    if r["estado"] == "creado":
        mov_id = insertar(c, user_id, r["tipo"], r["monto"], r["descripcion"], r["fecha"], r["banco"], fuente, reglas)
        firmado = -abs(r["monto"]) if r["tipo"] == "gasto" else abs(r["monto"])
        r["deuda"] = deudas.registrar_automatico(c, r["descripcion"], firmado, r["fecha"], mov_id)
    c.execute("""INSERT INTO capturas(user_id,fuente,texto,estado,monto,descripcion,fecha,banco,mov_id,creado)
        VALUES(?,?,?,?,?,?,?,?,?,?)""", (user_id, fuente, texto[:2000], r["estado"], r.get("monto"), r.get("descripcion"),
                                         r.get("fecha"), r.get("banco"), mov_id, avisos.ahora().isoformat()))
    c.commit()
    return {**r, "movimiento_id": mov_id}


def conciliar(c, user_id, banco, fecha, monto):
    """Si hay un movimiento provisional igual (mismo banco, mismo valor, fecha cercana), el extracto lo reemplaza."""
    r = c.execute("""SELECT id FROM movs WHERE estado='provisional' AND user_id=? AND (banco=? OR banco='Otro') AND ABS(monto-?)<0.5
        AND ABS(julianday(fecha)-julianday(?))<=5 ORDER BY ABS(julianday(fecha)-julianday(?)) LIMIT 1""",
                  (user_id, banco, monto, fecha, fecha)).fetchone()
    if r:
        c.execute("DELETE FROM movs WHERE id=?", (r["id"],))
        return r["id"]
    return None
