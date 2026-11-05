"""Agenda: todo lo que viene (cuotas, tarjeta, cobros, recordatorios, metas) en una sola línea de tiempo."""
import calendar
from datetime import date, timedelta

import cuentas
import deudas
import metas


def _mes_mas(hoy, k):
    m = hoy.month - 1 + k
    return hoy.year + m // 12, m % 12 + 1


def eventos(c, hoy=None, dias=60):
    hoy = hoy or date.today()
    fin = hoy + timedelta(days=dias)
    ev = []

    def add(fecha, titulo, detalle="", monto=None, tipo="otro", url="/", icono="calendar", color="#7a1fd6"):
        if fecha is not None and fecha <= fin:
            ev.append({"fecha": fecha, "titulo": titulo, "detalle": detalle, "monto": monto, "tipo": tipo, "url": url,
                       "icono": icono, "color": color, "dias": (fecha - hoy).days})

    for d in c.execute("SELECT * FROM deudas WHERE activo=1 AND COALESCE(origen,'manual')!='tarjeta'").fetchall():
        pagos = c.execute("SELECT monto,tipo FROM deuda_pagos WHERE deuda_id=?", (d["id"],)).fetchall()
        e = deudas.estado(d, pagos, hoy)
        if e["pagada"]:
            continue
        for r in e["rows"]:
            if not r["pagada"]:
                if d["direccion"] == "debo":
                    add(r["fecha"], f"Cuota de {d['nombre']}", f"Cuota {r['n']} de {e['n']} · saldo {r['saldo']:,.0f}".replace(",", "."),
                        r["cuota"], "deuda", f"/deudas/{d['id']}", "bank", "#a61e4d")
                else:
                    add(r["fecha"], f"Te toca cobrar: {d['nombre']}", f"Cuota {r['n']} de {e['n']}", r["cuota"], "cobro",
                        f"/deudas/{d['id']}", "users", "#0d9b6a")
    for t in c.execute("""SELECT p.* FROM pagos_tarjeta p WHERE p.pagado=0 AND p.id=(SELECT id FROM pagos_tarjeta x
            WHERE x.user_id=p.user_id AND x.banco=p.banco ORDER BY x.fecha_limite DESC, x.id DESC LIMIT 1)""").fetchall():
        f = date.fromisoformat(t["fecha_limite"])
        add(f, f"Pagar la tarjeta {t['banco']}", f"Mínimo {(t['pago_minimo'] or 0):,.0f} · para no pagar intereses {(t['a_pagar'] or 0):,.0f}".replace(",", "."),
            t["a_pagar"] or t["pago_minimo"] or 0.0, "tarjeta", "/deudas", "card", "#d6336c")
    for q in c.execute("SELECT * FROM cuentas WHERE activa=1 AND tipo='tarjeta' AND manejo_cond='sin_uso' AND cuota_manejo>0").fetchall():
        ciclo = cuentas.ciclo_tarjeta(c, q["user_id"], q["entidad"] or q["nombre"], hoy)
        if ciclo and not ciclo["uso"]:
            add(ciclo["fin"], f"Usa tu {q['nombre']} para evitar la cuota de manejo", "Aún no la usas en este ciclo",
                q["cuota_manejo"], "uso", "/deudas", "card", "#c98200")
    for r in c.execute("SELECT * FROM recordatorios WHERE activo=1").fetchall():
        rep = r["repite"] or "mensual"
        if r["fecha"] and rep != "mensual":
            f0 = date.fromisoformat(r["fecha"])
            fechas = [f0] if rep == "no" else [date(hoy.year + k, f0.month, min(f0.day, calendar.monthrange(hoy.year + k, f0.month)[1])) for k in (0, 1)]
            det = "Cada año" if rep == "anual" else "Evento de la agenda"
        else:
            fechas = []
            for k in range(0, 4):
                y, m = _mes_mas(hoy, k)
                fechas.append(date(y, m, min(r["dia"], calendar.monthrange(y, m)[1])))
            det = "Recordatorio mensual"
        for f in fechas:
            if f >= hoy:
                add(f, r["titulo"], det, r["monto"], "recordatorio", "/avisos", "bell", "#7a1fd6")
    for m_ in c.execute("SELECT * FROM metas WHERE activa=1").fetchall():
        aps = c.execute("SELECT * FROM meta_aportes WHERE meta_id=?", (m_["id"],)).fetchall()
        e = metas.estado_meta(m_, aps, hoy)
        if e["cumplida"] or not e["fin"]:
            continue
        bono = f" · +{e['bono']:.2f} pts E.A.".replace(".", ",") if e["bono"] else ""
        add(e["fin"], f"Meta: {m_['nombre']}", f"Faltan {e['falta']:,.0f}{bono}".replace(",", "."), e["falta"], "meta", "/metas", "target", "#0c8599")
    for r in c.execute("SELECT * FROM ingresos_fijos WHERE activo=1").fetchall():
        for k in range(0, 4):
            y, m = _mes_mas(hoy, k)
            f = date(y, m, min(r["dia"], calendar.monthrange(y, m)[1]))
            if f >= hoy:
                add(f, r["nombre"], "Ingreso esperado", r["monto"], "ingreso", "/ingresos", "up", "#0d9b6a")
    dia_ext = c.execute("SELECT valor FROM config WHERE clave='dia_extracto'").fetchone()
    dia = int(dia_ext["valor"]) if dia_ext else 4
    for k in range(0, 3):
        y, m = _mes_mas(hoy, k)
        f = date(y, m, min(dia, calendar.monthrange(y, m)[1]))
        if f >= hoy:
            add(f, "Subir los extractos del mes", "Nu, Nequi y Daviplata", None, "extractos", "/subir", "upload", "#868e96")
            break
    ev.sort(key=lambda x: (x["fecha"], x["tipo"]))
    return ev


def agrupar(ev, hoy=None):
    hoy = hoy or date.today()
    fin_mes = date(hoy.year, hoy.month, calendar.monthrange(hoy.year, hoy.month)[1])
    cubos = [("Hoy", []), ("Esta semana", []), ("Resto del mes", []), ("Más adelante", [])]
    for e in ev:
        if e["fecha"] <= hoy:
            cubos[0][1].append(e)
        elif e["dias"] <= 7:
            cubos[1][1].append(e)
        elif e["fecha"] <= fin_mes:
            cubos[2][1].append(e)
        else:
            cubos[3][1].append(e)
    return [(t, l) for t, l in cubos if l]


def por_pagar(ev, dias=7):
    return sum(e["monto"] or 0 for e in ev if e["tipo"] in ("deuda", "tarjeta") and e["dias"] <= dias)
