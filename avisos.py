"""Avisos push (Web Push): vencimiento de tarjeta, extractos, recordatorios y presupuestos."""
import base64
import calendar
import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

import deudas
import lector

TZ = ZoneInfo(os.environ.get("TZ", "America/Bogota"))
CONTACTO = os.environ.get("VAPID_CONTACT", "mailto:admin@example.com")
EXC = "categoria NOT IN (" + ",".join(f"'{x}'" for x in lector.EXCLUIDAS) + ")"


def ahora():
    return datetime.now(TZ)


def cop(v):
    """Pesos con dos decimales, como en Nu: $63.634,80 · $12.000,00 · -$1.500,00"""
    v = v or 0
    centavos = round(abs(v) * 100)
    ent, cen = divmod(centavos, 100)
    return ("-" if v < 0 and centavos else "") + "$" + f"{ent:,}".replace(",", ".") + f",{cen:02d}"


def claves(data_dir):
    """Crea (una sola vez) las llaves VAPID y devuelve (ruta_pem, llave_publica_b64)."""
    pem = Path(data_dir) / "vapid_private.pem"
    if not pem.exists():
        v = Vapid()
        v.generate_keys()
        v.save_key(str(pem))
    v = Vapid.from_file(str(pem))
    pub = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return str(pem), base64.urlsafe_b64encode(pub).rstrip(b"=").decode()


def cfg(c, clave, defecto):
    r = c.execute("SELECT valor FROM config WHERE clave=?", (clave,)).fetchone()
    return r["valor"] if r else defecto


def enviar(db, pem, user_ids, titulo, cuerpo, url="/avisos"):
    c = db()
    if user_ids:
        q = ",".join("?" * len(user_ids))
        subs = c.execute(f"SELECT * FROM push_subs WHERE user_id IN ({q})", list(user_ids)).fetchall()
    else:
        subs = c.execute("SELECT * FROM push_subs").fetchall()
    n = 0
    for s in subs:
        try:
            webpush({"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                    data=json.dumps({"titulo": titulo, "cuerpo": cuerpo, "url": url}),
                    vapid_private_key=pem, vapid_claims={"sub": CONTACTO}, ttl=86400)
            n += 1
        except WebPushException as e:
            if e.response is not None and e.response.status_code in (404, 410):
                c.execute("DELETE FROM push_subs WHERE id=?", (s["id"],))   # suscripción vencida
            else:
                print("push error:", e, flush=True)
        except Exception as e:
            print("push error:", e, flush=True)
    c.commit()
    c.close()
    return n


def avisar(db, pem, clave, titulo, cuerpo, para=None, url="/avisos"):
    """Envía una sola vez por 'clave'. Si nadie lo recibió, no lo marca como enviado y reintenta luego."""
    c = db()
    try:
        c.execute("INSERT INTO avisos_log(clave,creado) VALUES(?,?)", (clave, ahora().isoformat()))
        c.commit()
    except sqlite3.IntegrityError:
        c.close()
        return False
    c.close()
    n = enviar(db, pem, para, titulo, cuerpo, url) if para else 0
    if not n:
        n = enviar(db, pem, None, titulo, cuerpo, url)     # si el dueño no tiene avisos, que le llegue a la familia
    if not n:
        c = db()
        c.execute("DELETE FROM avisos_log WHERE clave=?", (clave,))
        c.commit()
        c.close()
    return bool(n)


def revisar_presupuestos(db, pem):
    now = ahora()
    mes = now.strftime("%Y-%m")
    c = db()
    pres = c.execute("SELECT categoria, limite FROM presupuestos").fetchall()
    gastos = {r["categoria"]: r["v"] for r in c.execute(
        f"SELECT categoria, SUM(-monto) v FROM movs WHERE monto<0 AND {EXC} AND substr(fecha,1,7)=? GROUP BY categoria", (mes,))}
    c.close()
    for p in pres:
        g, lim = gastos.get(p["categoria"], 0), p["limite"]
        if lim <= 0:
            continue
        for umbral in (100, 80):
            if g / lim * 100 >= umbral:
                if umbral == 100:
                    t, b = f"Te pasaste en {p['categoria']}", f"Llevas {cop(g)} de {cop(lim)} este mes."
                else:
                    t, b = f"{p['categoria']} al 80%", f"Llevas {cop(g)} de {cop(lim)}. Quedan {cop(lim - g)}."
                avisar(db, pem, f"pres:{p['categoria']}:{mes}:{umbral}", t, b, url="/")
                break


def revisar(db, pem):
    """Se ejecuta cada ~15 min. Cada aviso se manda una sola vez (tabla avisos_log)."""
    now = ahora()
    hoy = now.date()
    c = db()
    hora_min = int(cfg(c, "hora_aviso", 9))
    dia_ext = int(cfg(c, "dia_extracto", 4))
    ultima = cfg(c, "ultima_subida", "")
    pagos = c.execute("SELECT * FROM pagos_tarjeta WHERE pagado=0").fetchall()
    recs = c.execute("SELECT * FROM recordatorios WHERE activo=1").fetchall()
    c.close()
    if now.hour < hora_min:
        return

    for p in pagos:                                           # 1) vencimiento de la tarjeta
        dias = (date.fromisoformat(p["fecha_limite"]) - hoy).days
        if dias in (3, 1, 0) or -3 <= dias < 0:
            quedan = ("vence HOY" if dias == 0 else f"vence en {dias} día{'s' if dias != 1 else ''}" if dias > 0
                      else f"venció hace {-dias} día{'s' if dias != -1 else ''}")
            cuerpo = (f"Pago mínimo {cop(p['pago_minimo'] or 0)}"
                      + (f" · para no pagar intereses {cop(p['a_pagar'])}" if p["a_pagar"] else "")
                      + f" · límite {date.fromisoformat(p['fecha_limite']):%d/%m}")
            avisar(db, pem, f"tarjeta:{p['id']}:{dias}", f"Tu tarjeta {p['banco']} {quedan}", cuerpo, [p["user_id"]])

    c = db()
    deus = c.execute("SELECT * FROM deudas WHERE activo=1").fetchall()
    for d in deus:                                            # 1b) cuotas de deudas
        pagos = c.execute("SELECT monto,tipo FROM deuda_pagos WHERE deuda_id=?", (d["id"],)).fetchall()
        e = deudas.estado(d, pagos, hoy)
        if e["pagada"] or e["proxima"] is None:
            continue
        dias = e["dias"]
        if dias in (3, 1, 0) or -3 <= dias < 0:
            quedan = ("vence HOY" if dias == 0 else f"vence en {dias} día{'s' if dias != 1 else ''}" if dias > 0
                      else f"venció hace {-dias} día{'s' if dias != -1 else ''}")
            if d["direccion"] == "debo":
                titulo = f"Cuota de {d['nombre']} {quedan}"
                cuerpo = f"Cuota {e['cuota'] and cop(e['proxima_monto'])} · saldo {cop(e['saldo'])} · día {e['proxima']:%d/%m}"
            else:
                titulo, cuerpo = f"Te toca cobrar: {d['nombre']} ({quedan})", f"Cuota {cop(e['proxima_monto'])} · te deben {cop(e['saldo'])}"
            avisar(db, pem, f"deuda:{d['id']}:{e['cubiertas']}:{dias}", titulo, cuerpo, [d["user_id"]], url="/deudas")
    c.close()
    for r in recs:                                            # 2) recordatorios de la familia
        for k in (0, 1):
            y, m = hoy.year + (hoy.month + k - 1) // 12, (hoy.month + k - 1) % 12 + 1
            obj = date(y, m, min(r["dia"], calendar.monthrange(y, m)[1]))
            if hoy == obj - timedelta(days=r["antes"]):
                cuerpo = "Es hoy" if r["antes"] == 0 else f"Falta{'n' if r['antes'] > 1 else ''} {r['antes']} día{'s' if r['antes'] > 1 else ''} ({obj:%d/%m})"
                avisar(db, pem, f"rec:{r['id']}:{hoy}", r['titulo'], cuerpo, [r["para"]] if r["para"] else None)

    if hoy.day in (dia_ext, dia_ext + 3):                     # 3) extractos del mes
        if not ultima or date.fromisoformat(ultima[:10]) < date(hoy.year, hoy.month, dia_ext):
            avisar(db, pem, f"extracto:{hoy}", "Ya llegaron los extractos",
                   "Descarga los PDF de tus bancos y súbelos a Cuadre para cuadrar el mes.", url="/subir")

    revisar_presupuestos(db, pem)                             # 4) presupuestos
