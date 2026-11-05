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


# ---------- Registro por voz ----------
_UNI = {"cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9,
        "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15, "dieciseis": 16, "diecisiete": 17,
        "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiun": 21, "veintiuno": 21, "veintidos": 22, "veintitres": 23,
        "veinticuatro": 24, "veinticinco": 25, "veintiseis": 26, "veintisiete": 27, "veintiocho": 28, "veintinueve": 29,
        "treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90,
        "cien": 100, "ciento": 100, "doscientos": 200, "trescientos": 300, "cuatrocientos": 400, "quinientos": 500,
        "seiscientos": 600, "setecientos": 700, "ochocientos": 800, "novecientos": 900}


def _sin(t):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")


def monto_hablado(texto):
    """'cuarenta y cinco mil', '2 millones quinientos mil', '35 mil', 'veinte lucas', '$50.000' -> (monto, (ini, fin) en palabras) o None."""
    t = _sin(texto).replace("$", " $ ")
    palabras = t.split()
    # número con dígitos primero (con o sin 'mil'/'millones'/'lucas')
    for i, w in enumerate(palabras):
        if re.fullmatch(r"\d[\d.,]*", w):
            try:
                v = numero_es(w)
            except ValueError:
                continue
            sig = palabras[i + 1] if i + 1 < len(palabras) else ""
            if sig in ("mil", "lucas", "luca", "k"):
                v *= 1000
            elif sig in ("millon", "millones"):
                v *= 1_000_000
            elif sig.startswith("pes") or True:
                pass
            return v, (i, i + 1 + (1 if sig in ("mil", "lucas", "luca", "k", "millon", "millones") else 0))
    total, actual, ini, fin, visto, salto = 0, 0, None, None, False, -1
    for i, w in enumerate(palabras):
        if i == salto:
            continue
        if w in _UNI:
            actual += _UNI[w]; visto = True; ini = i if ini is None else ini; fin = i + 1
        elif w == "y" and visto:
            continue
        elif w in ("mil", "lucas", "luca") and visto:
            total += (actual or 1) * 1000; actual = 0; fin = i + 1
        elif w == "mil" and not visto:
            actual = 0; total += 1000; visto = True; ini = i if ini is None else ini; fin = i + 1
        elif w in ("millon", "millones") and visto:
            total += (actual or 1) * 1_000_000; actual = 0; fin = i + 1
        elif w == "medio" and i + 1 < len(palabras) and palabras[i + 1] in ("millon",):
            total += 500_000; visto = True; ini = i if ini is None else ini; fin = i + 2; salto = i + 1
        elif visto:
            break
    if visto and total + actual > 0:
        return float(total + actual), (ini, fin)
    return None


def voz(texto, hoy):
    """Convierte 'gasté cuarenta mil en taxi con nequi' en un movimiento para confirmar. Devuelve dict o None si no hay valor."""
    r = monto_hablado(texto)
    if not r:
        return None
    monto, (a, b) = r
    t = _sin(texto)
    pal = t.replace("$", " ").split()
    tipo = "gasto"
    if re.search(r"\b(pase|pasar|transferi|traslade|movi|mande)\b", t):
        hallados = sorted((f" {t} ".find(k), n) for k, n in ((" nequi", "Nequi"), (" daviplata", "Daviplata"), (" efectivo", "Efectivo"), (" nu ", "Nu"))
                          if f" {t} ".find(k) >= 0)
        if len(hallados) >= 2:
            return {"tipo": "traslado", "monto": monto, "descripcion": "", "cuenta": hallados[0][1], "destino": hallados[1][1],
                    "fecha": hoy.isoformat()}
    if re.search(r"\b(retire|retiro|saque|saqu[eé] del cajero)\b", t):
        tipo = "retiro"
    elif re.search(r"\b(recibi|me pagaron|me llego|me consignaron|me transfirieron|cobre|ingreso|gane|me depositaron|me enviaron)\b", t):
        tipo = "ingreso"
    cuenta = ""
    for k, nom in (("nequi", "Nequi"), ("daviplata", "Daviplata"), ("efectivo", "Efectivo"), ("tarjeta", "Tarjeta"), ("credito", "Tarjeta"), (" nu ", "Nu")):
        if k in f" {t} ":
            cuenta = nom; break
    fecha = hoy
    from datetime import timedelta
    if re.search(r"\banteayer\b|\bantier\b", t):
        fecha = hoy - timedelta(days=2)
    elif re.search(r"\bayer\b", t):
        fecha = hoy - timedelta(days=1)
    resto = pal[:a] + pal[b:]
    basura = {"gaste", "pague", "compre", "recibi", "me", "pagaron", "llego", "pesos", "peso", "de", "en", "por", "con", "un", "una", "el", "la", "los", "las",
              "hoy", "ayer", "antier", "anteayer", "retire", "retiro", "saque", "cobre", "gane", "ingreso", "a", "y", "mi", "del", "al", "para", "$",
              "nequi", "daviplata", "efectivo", "tarjeta", "credito", "nu", "debito", "cuenta", "lucas", "luca", "mil", "k", "consignaron", "transfirieron",
              "depositaron", "enviaron"}
    desc = [w for w in resto if w not in basura]
    return {"tipo": tipo, "monto": monto, "descripcion": " ".join(desc).strip().capitalize(), "cuenta": cuenta, "fecha": fecha.isoformat()}
