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
                  "nómina", "nomina", "pago recibido", "reembolso", "devoluci"]

CATEGORIAS_BASE = {
    "Suscripciones": ["netflix", "spotify", "disney", "amazon", "youtube", "apple", "hbo", "openai", "anthropic", "paramount"],
    "Educación": ["platzi", "udemy", "coursera", "colegio", "universidad", "matricula", "matrícula"],
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
EXCLUIDAS = ("Traslado propio", "Pago de tarjeta")
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


def leer_pdf(datos: bytes, clave: str, tipo: str, reglas):
    movs = []
    with pdfplumber.open(io.BytesIO(datos), password=clave or None) as pdf:
        textos = [(p.extract_text() or "") for p in pdf.pages]
    todo = "\n".join(textos)
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
