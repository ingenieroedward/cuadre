"""Metas de ahorro y 'cuánto puedo gastar': lo que hay hoy menos lo que ya está comprometido."""
import calendar
from datetime import date

import avisos
import deudas
import saldos


def _g(r, k, d=None):
    try:
        v = r[k]
    except (KeyError, IndexError):
        return d
    return d if v is None else v


def estado_meta(m, aportes, hoy=None):
    """Progreso de una meta. mensual: lo aportado en el mes actual; total: todo lo aportado (opcionalmente hacia una fecha)."""
    hoy = hoy or date.today()
    obj = float(m["objetivo"])
    if m["periodo"] == "mensual":
        mes = hoy.strftime("%Y-%m")
        aportado = sum(a["monto"] for a in aportes if a["fecha"][:7] == mes)
        fin = date(hoy.year, hoy.month, calendar.monthrange(hoy.year, hoy.month)[1])
        dias = (fin - hoy).days + 1
    else:
        aportado = sum(a["monto"] for a in aportes)
        fin = date.fromisoformat(m["fecha_limite"]) if m["fecha_limite"] else None
        dias = (fin - hoy).days if fin else None
    falta = max(obj - aportado, 0.0)
    e = {"objetivo": obj, "aportado": aportado, "falta": falta, "pct": min(aportado / obj, 1.0) if obj > 0 else 0.0,
         "cumplida": aportado + 0.005 >= obj, "dias": dias, "fin": fin, "bono": float(_g(m, "bono_ea", 0.0)), "por_dia": None, "por_mes": None}
    if not e["cumplida"] and dias and dias > 0:
        e["por_dia"] = falta / dias
        if m["periodo"] == "total":
            meses = max(dias / 30.4, 1.0)
            e["por_mes"] = falta / meses
    return e


def _fin_mes(d):
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def horizonte(c, hoy):
    """Hasta cuándo se reparte la plata: el fin de mes, o el día del mes en que entra tu próximo ingreso (configurable)."""
    r = c.execute("SELECT valor FROM config WHERE clave='horizonte_dia'").fetchone()
    dia = int(r["valor"]) if r and r["valor"] else 0
    if not dia:
        return _fin_mes(hoy), False
    obj = date(hoy.year, hoy.month, min(dia, calendar.monthrange(hoy.year, hoy.month)[1]))
    if obj <= hoy:
        sig = deudas.add_months(date(hoy.year, hoy.month, 1), 1)
        obj = date(sig.year, sig.month, min(dia, calendar.monthrange(sig.year, sig.month)[1]))
    return obj, True


def disponible(c, hoy=None):
    """¿Cuánto puedo gastar por día? Devuelve None si aún no hay saldos."""
    hoy = hoy or date.today()
    lista = saldos.actuales(c)
    if not lista:
        return None
    fin, es_dia = horizonte(c, hoy)
    dias = max((fin - hoy).days, 1) if es_dia else (fin - hoy).days + 1
    saldo = sum(x["estimado"] for x in lista)
    comp = []
    # cuotas de deudas que vencen antes del horizonte
    for d in c.execute("SELECT * FROM deudas WHERE activo=1 AND direccion='debo' AND COALESCE(origen,'manual')!='tarjeta'").fetchall():
        pagos = c.execute("SELECT monto,tipo FROM deuda_pagos WHERE deuda_id=?", (d["id"],)).fetchall()
        e = deudas.estado(d, pagos, hoy)
        if e["pagada"]:
            continue
        for r in e["rows"]:
            if not r["pagada"] and r["fecha"] <= fin:
                comp.append({"que": f"Cuota de {d['nombre']}", "fecha": r["fecha"], "monto": r["cuota"]})
    # pago de la tarjeta
    for t in c.execute("""SELECT p.* FROM pagos_tarjeta p WHERE p.pagado=0 AND p.id=(SELECT id FROM pagos_tarjeta x
            WHERE x.user_id=p.user_id AND x.banco=p.banco ORDER BY x.fecha_limite DESC, x.id DESC LIMIT 1)""").fetchall():
        f = date.fromisoformat(t["fecha_limite"])
        if f <= fin:
            comp.append({"que": f"Pago de la tarjeta {t['banco']}", "fecha": f, "monto": t["a_pagar"] or t["pago_minimo"] or 0.0})
    # lo que falta para las metas mensuales
    metas_rows = c.execute("SELECT * FROM metas WHERE activa=1 AND periodo='mensual'").fetchall()
    for m in metas_rows:
        aps = c.execute("SELECT * FROM meta_aportes WHERE meta_id=?", (m["id"],)).fetchall()
        e = estado_meta(m, aps, hoy)
        if e["falta"] > 0.005:
            comp.append({"que": f"Meta: {m['nombre']}", "fecha": None, "monto": e["falta"]})
    comp.sort(key=lambda x: (x["fecha"] or date.max))
    total = sum(x["monto"] for x in comp)
    libre = saldo - total
    gastado_hoy = c.execute(f"""SELECT COALESCE(SUM(-monto),0) v FROM movs WHERE monto<0 AND fecha=? AND {avisos.EXC}""",
                            (hoy.isoformat(),)).fetchone()["v"]
    por_dia = libre / dias if dias > 0 else libre
    return {"saldo": saldo, "comp": comp, "comprometido": total, "libre": libre, "dias": dias, "fin": fin, "por_dia": por_dia,
            "gastado_hoy": gastado_hoy, "restante_hoy": por_dia - gastado_hoy, "es_dia": es_dia,
            "estado": "mal" if libre < 0 else ("justo" if por_dia < 20000 else "bien")}
