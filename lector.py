"""Lectura de extractos PDF (protegidos con cédula). Parser genérico: busca líneas
'fecha ... valor'. Los bancos con formato raro se ajustan aquí en parse_lineas()."""
import io
import re
import unicodedata
from datetime import date

import pdfplumber

MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7,
         "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12}

RE_VALOR = re.compile(r"[+-]?\$?\s?[+-]?\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|[+-]?\$?\s?[+-]?\d+,\d{1,2}|[+-]?\$\s?\d+")
RE_FECHA = re.compile(
    r"^(?P<f>\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|\d{1,2}\s+(?:de\s+)?[A-Za-zñÑ]{3,10}\.?(?:\s+(?:de\s+)?\d{2,4})?)\s+(?P<resto>.+)$"
)

INGRESO_CLAVES = ["abono", "consignaci", "rendimiento", "recib", "deposito", "depósito",
                  "nómina", "nomina", "pago recibido", "reembolso", "devoluci", "reversi"]

CATEGORIAS_BASE = {
    "Suscripciones": ["netflix", "spotify", "disney", "amazon", "youtube", "apple", "hbo", "openai", "anthropic", "paramount"],
    "Educación": ["platzi", "udemy", "coursera", "colegio", "universidad", "matricula", "matrícula"],
    "Compras en línea": ["merpago", "mercadolibre", "mercad olibre", "temu", "shein", "aliexpress"],
    "Supermercado": ["exito", "éxito", " d1 ", " ara ", "jumbo", "olimpica", "olímpica", "carulla", "makro", "canasto", "supertienda"],
    "Comida": ["rappi", "restaur", "mcdonald", "kfc", "domicilio", "ifood", "cafe", "café", "panader", "pizza"],
    "Transporte": ["uber", "didi", "cabify", "indriver", "terpel", "primax", "texaco", "peaje", "gasolina"],
    "Salud": ["farmacia", "drogueria", "droguería", " eps ", "clinica", "clínica", "cruz verde"],
    "Servicios": ["claro", "movistar", "tigo", "enel", "epm", "vanti", "acueducto", "etb", "surtigas", "gases"],
    "Hogar": ["dollarcity", "homecenter", "ikea"],
    "Intereses y cobros": ["interes", "interés", "cuota de manejo", "comision", "comisión"],
    "Ingresos": ["rendimiento", "nómina", "nomina", "salario"],
}
# Estas categorías no cuentan como ingreso/gasto en el resumen (mueven plata propia)
EXCLUIDAS = ("Traslado propio", "Pago de tarjeta", "Reversado")
import os
NOMBRES_PROPIOS = [n.strip().lower() for n in os.environ.get("OWN_NAMES", "").split(",") if n.strip()]
PREFIJOS = ("recibiste de ", "enviaste a ", "compra en ", "pago a ", "pagaste ")


def a_numero(txt):
    neg = "-" in txt
    limpio = re.sub(r"[^\d,]", "", txt).replace(",", ".")
    if not limpio:
        return None
    v = float(limpio)
    return -v if neg else v


def a_iso(txt, anio_defecto):
    t = txt.strip().lower().replace(" de ", " ").replace(".", "")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", t)
    if m:
        return f"{m[1]}-{m[2]}-{m[3]}"
    m = re.match(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?$", t)
    if m:
        a = int(m[3]) if m[3] else anio_defecto
        a += 2000 if a < 100 else 0
        return _fecha(a, int(m[2]), int(m[1]))
    m = re.match(r"(\d{1,2})\s+([a-zñ]{3})[a-zñ]*(?:\s+(\d{2,4}))?$", t)
    if m and m[2] in MESES:
        a = int(m[3]) if m[3] else anio_defecto
        a += 2000 if a < 100 else 0
        return _fecha(a, MESES[m[2]], int(m[1]))
    return None


def _fecha(a, m, d):
    try:
        return date(a, m, d).isoformat()
    except ValueError:
        return None


def sin_tildes(t):
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


def categoria(desc, reglas):
    d = " " + desc.lower() + " "
    if d.strip().startswith("pagaste tu tarjeta"):
        return "Pago de tarjeta"
    if any(sin_tildes(n) in sin_tildes(d) for n in NOMBRES_PROPIOS):
        return "Traslado propio"
    if re.search(r"\bretiro\b|retiraste|\bcajero\b", d):
        return "Traslado propio"       # sacar efectivo no es gastar: el gasto se anota cuando se paga con ese efectivo
    for kw, cat in reglas:               # reglas del usuario primero
        if kw.lower() in d:
            return cat
    for cat, claves in CATEGORIAS_BASE.items():
        if any(k in d for k in claves):
            return cat
    if d.strip().startswith("recibiste de"):
        return "Transferencias recibidas"
    if d.strip().startswith("enviaste a"):
        return "Transferencias enviadas"
    return "Otros"


def clave_regla(desc):
    """Palabras que identifican al comercio/persona, sin 'Recibiste de', 'Compra en'..."""
    d = desc.lower()
    for p in PREFIJOS:
        if d.startswith(p):
            d = d[len(p):]
    d = d.replace(" con tarjeta débito", "").replace(" con tarjeta debito", "")
    return " ".join(d.split()[:2])


def limpiar(desc):
    desc = re.sub(r"\s+con tarjeta d[ée]bito$", "", desc, flags=re.I)
    return re.sub(r"^compra en\s+", "", desc, flags=re.I)


def signo(valor, desc, tipo):
    """tipo: 'cuenta' o 'tarjeta'. Devuelve valor con signo (gasto<0, ingreso>0)."""
    d = desc.lower()
    es_ingreso = any(k in d for k in INGRESO_CLAVES)
    v = abs(valor)
    if tipo == "tarjeta":
        # en la tarjeta, compras suben la deuda; pagos/abonos la bajan
        return v if es_ingreso else -v
    if valor < 0:
        return -v
    return v if es_ingreso else -v


RE_DIA = re.compile(r"^(\d{1,2})\s+([A-Za-zñÑ]{3})\.?$")
RE_ANIO = re.compile(r"^(20\d{2})$")
RE_DINERO = re.compile(r"-?\$[\d.]+,\d{2}")


def leer_pdf(datos: bytes, clave: str, tipo: str, reglas):
    """Devuelve movimientos. Detecta solo si el PDF es cuenta o tarjeta (ignora la elección del usuario si el texto lo delata)."""
    with pdfplumber.open(io.BytesIO(datos), password=clave or None) as pdf:
        todo = "\n".join((p.extract_text() or "") for p in pdf.pages)
    t = todo.lower()
    if "cupo definido" in t or "deuda total" in t:
        tipo = "tarjeta"
    elif "resumen de tus movimientos" in t:
        tipo = "cuenta"
    movs = _leer_tarjeta(todo, reglas) if tipo == "tarjeta" else _leer_cuenta(todo, tipo, reglas)
    for m in movs:
        m["origen"] = tipo
    return movs, (info_tarjeta(todo) if tipo == "tarjeta" else info_cuenta(todo))


def _dinero(rx, texto):
    m = re.search(rx, texto, re.I)
    return a_numero(m[1]) if m else None


def info_cuenta(todo):
    """Totales y saldos que Nu imprime en el resumen del extracto."""
    entro = _dinero(r"Lo que entr[oó] a tu cuenta\s*\+?\$([\d.]+,\d{2})", todo)
    salio = _dinero(r"Lo que sali[oó] de tu cuenta\s*-?\$([\d.]+,\d{2})", todo)
    if entro is None or salio is None:
        return None
    m = re.search(r"Per[ií]odo\s*\d{1,2}\s*-\s*(\d{1,2})\s+([A-Za-zñÑ]{3})[A-Za-zñÑ]*\s+(20\d{2})", todo, re.I)
    cierre = _fecha(int(m[3]), MESES.get(m[2].lower(), 0), int(m[1])) if m else None
    return {"entro": entro, "salio": salio, "cierre": cierre,
            "inicio": _dinero(r"Tu dinero al inicio del mes\s*\$([\d.]+,\d{2})", todo),
            "final": _dinero(r"Tu dinero a final del mes\s*\$([\d.]+,\d{2})", todo)}


def info_tarjeta(todo):
    """Fecha límite, pago mínimo y deuda del extracto de la tarjeta (para los recordatorios)."""
    m = re.search(r"Fecha l[ií]mite de pago\s*(\d{1,2})\s+([A-Za-zñÑ]{3})[A-Za-zñÑ]*\s+(20\d{2})", todo, re.I)
    fecha = _fecha(int(m[3]), MESES.get(m[2].lower(), 0), int(m[1])) if m else None
    if not fecha:
        return None
    return {"fecha_limite": fecha,
            "pago_minimo": _dinero(r"PAGO M[IÍ]NIMO\s*\$([\d.]+,\d{2})", todo),
            "a_pagar": _dinero(r"Deuda a pagar este mes\s*\$([\d.]+,\d{2})", todo),
            "deuda_total": _dinero(r"DEUDA TOTAL[^$]*\$([\d.]+,\d{2})", todo)}


def _leer_tarjeta(todo, reglas):
    """Registros de varias líneas: 'DD MES' / 'AAAA' / descripción (puede partirse) / valores."""
    lineas = [l.strip() for l in todo.splitlines()]
    movs, i = [], 0
    while i < len(lineas):
        m = RE_DIA.match(lineas[i])
        if not (m and m[2].lower() in MESES and i + 1 < len(lineas) and RE_ANIO.match(lineas[i + 1])):
            i += 1
            continue
        fecha = _fecha(int(lineas[i + 1]), MESES[m[2].lower()], int(m[1]))
        j, partes = i + 2, []
        while j < len(lineas) and j - i < 8:
            partes.append(lineas[j])
            hay = RE_DINERO.search(lineas[j])
            j += 1
            if hay:
                break
        texto = " ".join(partes)
        dinero = RE_DINERO.findall(texto)
        if dinero and fecha:
            desc = re.sub(r"Mercad\s+olibre", "Mercadolibre", texto.split(dinero[0])[0]).strip()
            valor = abs(a_numero(dinero[0]) or 0)
            d = desc.lower()
            if d.startswith("pago"):
                monto, cat = valor, "Pago de tarjeta"
            elif "reversi" in d or "devoluci" in d:
                monto, cat = valor, "Devoluciones"
            else:
                monto, cat = -valor, categoria(desc, reglas)
            if valor:
                movs.append({"fecha": fecha, "descripcion": desc[:200], "monto": monto, "categoria": cat})
        i = j
    # una reversión que anula una compra igual del mismo extracto: ambas quedan en "Reversado"
    for r in [x for x in movs if x["categoria"] == "Devoluciones"]:
        for c in movs:
            if c["monto"] == -r["monto"] and c["categoria"] not in ("Reversado", "Pago de tarjeta"):
                r["categoria"] = c["categoria"] = "Reversado"
                break
    return movs


def _leer_cuenta(todo, tipo, reglas):
    movs = []
    m = re.search(r"\b(20\d{2})\b", todo)
    anio = int(m[1]) if m else date.today().year
    for linea in todo.splitlines():
        linea = linea.strip()
        mf = RE_FECHA.match(linea)
        if not mf:
            continue
        iso = a_iso(mf["f"], anio)
        valores = RE_VALOR.findall(mf["resto"])
        if not iso or not valores:
            continue
        crudo = valores[-1].strip()
        valor = a_numero(crudo)
        if not valor:
            continue
        explicito = crudo[0] in "+-"
        desc = mf["resto"]
        for v in valores:
            desc = desc.replace(v, "")
        desc = re.sub(r"\s+", " ", desc).strip(" $-")
        if len(desc) < 3:
            continue
        monto = valor if (explicito and tipo == "cuenta") else signo(valor, desc, tipo)
        movs.append({"fecha": iso, "descripcion": limpiar(desc)[:200], "monto": monto,
                     "categoria": categoria(desc, reglas)})
    return movs
