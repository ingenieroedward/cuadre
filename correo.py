"""Lee un buzón (IMAP) donde la familia reenvía las alertas por correo de sus bancos."""
import email
import html
import imaplib
import os
import re
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime

import avisos
import captura

DOMINIOS = {"nequi.com.co": "Nequi", "nequi.com": "Nequi", "bancolombia.com.co": "Bancolombia", "bancolombia.com": "Bancolombia",
            "documentosbancolombia.com": "Bancolombia", "davivienda.com": "Davivienda", "nu.com.co": "Nu", "bbva.com.co": "BBVA",
            "daviplata.com": "Daviplata", "bancodebogota.com.co": "Banco de Bogotá"}


def configurado():
    return all(os.environ.get(k) for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD"))


def _texto(msg):
    partes = {"text/plain": [], "text/html": []}
    for p in msg.walk():
        t = p.get_content_type()
        if t in partes and "attachment" not in str(p.get("Content-Disposition", "")):
            try:
                partes[t].append(p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", "replace"))
            except Exception:
                pass
    if partes["text/plain"]:
        return "\n".join(partes["text/plain"])
    h = "\n".join(partes["text/html"])
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
    return html.unescape(re.sub(r"<[^>]+>", " ", h))


def _banco(*textos):
    for t in textos:
        for dom in re.findall(r"@([\w.-]+)", t or ""):
            for suf, nombre in DOMINIOS.items():
                if dom == suf or dom.endswith("." + suf):
                    return nombre
    return ""


def procesar_mensaje(c, raw: bytes):
    """Devuelve el resultado de captura.procesar, o None si el correo no es una alerta que nos interese."""
    msg = email.message_from_bytes(raw)
    asunto = str(make_header(decode_header(msg.get("Subject", ""))))
    cuerpo = re.sub(r"\s+", " ", _texto(msg)).strip()
    destinos = " ".join(msg.get_all("To", []) + msg.get_all("Delivered-To", []) + msg.get_all("X-Original-To", []))
    usuario = None
    m = re.search(r"\+([a-z0-9._-]+)@", destinos.lower())
    if m:
        usuario = c.execute("SELECT id FROM users WHERE username=?", (m[1],)).fetchone()
    banco = _banco(msg.get("From", ""), cuerpo[:800])
    if not usuario and not banco:
        return None
    if not usuario:
        usuario = c.execute("SELECT id FROM users WHERE admin=1 ORDER BY id LIMIT 1").fetchone()
    if not usuario:
        return None
    try:
        fecha = parsedate_to_datetime(msg.get("Date")).astimezone(avisos.TZ).date()
    except Exception:
        fecha = None
    return captura.procesar(c, usuario["id"], cuerpo[:1800], asunto, banco, "correo", fecha_defecto=fecha)


def revisar_buzon(db):
    """Una pasada: lee los correos sin leer, crea los movimientos y los marca como leídos."""
    if not configurado():
        return
    c = db()
    nuevos = 0
    try:
        imap = imaplib.IMAP4_SSL(os.environ["IMAP_HOST"], int(os.environ.get("IMAP_PORT", 993)), timeout=30)
        imap.login(os.environ["IMAP_USER"], os.environ["IMAP_PASSWORD"])
        imap.select(os.environ.get("IMAP_FOLDER", "INBOX"))
        _, ids = imap.search(None, "UNSEEN")
        for i in ids[0].split()[:50]:
            _, datos = imap.fetch(i, "(RFC822)")
            try:
                r = procesar_mensaje(c, datos[0][1])
                nuevos += 1 if r and r.get("estado") in ("creado", "por_revisar") else 0
            except Exception as e:
                print("correo:", type(e).__name__, e, flush=True)
            imap.store(i, "+FLAGS", "\\Seen")
        imap.logout()
        estado = f"OK · {avisos.ahora():%d/%m %H:%M} · {nuevos} nuevos"
    except Exception as e:
        estado = f"Error · {avisos.ahora():%d/%m %H:%M} · {type(e).__name__}"
    c.execute("INSERT OR REPLACE INTO config(clave,valor) VALUES('correo_estado',?)", (estado,))
    c.commit()
    c.close()
