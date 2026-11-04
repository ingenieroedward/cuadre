"""Deudas: tabla de amortización, saldo, costo real (E.A. implícita), abonos y estrategia de pago."""
import calendar
from datetime import date


def add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def tasa_m(ea):
    """E.A. en % -> tasa efectiva mensual (fracción)."""
    return (1 + ea / 100.0) ** (1 / 12) - 1 if ea and ea > 0 else 0.0


def cuota_calc(P, i, n):
    return P / n if i == 0 else P * i / (1 - (1 + i) ** -n)


def ea_implicita(P, cuota, n):
    """E.A. (%) que hace que n cuotas iguales de ese valor paguen un préstamo P."""
    if n <= 0 or cuota * n <= P * 1.0000001:
        return 0.0
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if cuota_calc(P, mid, n) < cuota:
            lo = mid
        else:
            hi = mid
    return ((1 + lo) ** 12 - 1) * 100


def parametros(d):
    """(principal, cuotas, valor_cuota, ea%, tasa_mensual). Completa lo que falte con lo que sí dio la persona."""
    P, n = float(d["principal"]), int(d["n_cuotas"])
    cuota = float(d["cuota"] or 0)
    ea = d["tasa_ea"]
    if cuota > 0 and ea is None:
        ea = ea_implicita(P, cuota, n)
    ea = float(ea or 0)
    i = tasa_m(ea)
    if not cuota:
        cuota = cuota_calc(P, i, n)
    return P, n, cuota, ea, i


def cronograma(P, n, cuota, i, primera):
    bal, rows, k = P, [], 0
    while bal > 0.005 and k < 1200:
        k += 1
        interes = bal * i
        pago = cuota
        if k >= n or pago >= bal + interes - 0.005:
            pago = bal + interes                       # la última cuota salda lo que quede
        cap = pago - interes
        bal -= cap
        rows.append({"n": k, "fecha": add_months(primera, k - 1), "cuota": pago, "interes": interes, "capital": cap,
                     "saldo": max(bal, 0.0)})
    return rows


def _g(d, k, defecto=None):
    try:
        v = d[k]
    except (KeyError, IndexError):
        return defecto
    return defecto if v is None else v


def estado(d, pagos, hoy=None):
    """Las compras a cuotas de la tarjeta se actualizan solas con el extracto (cuota facturada y lo que resta)."""
    if _g(d, "origen", "manual") != "tarjeta":
        return _estado(d, pagos, hoy)
    k = int(_g(d, "cuotas_fact", 0))
    sint = [{"monto": float(d["cuota"] or 0) * k, "tipo": "cuota"}] if k else []
    e = _estado(d, sint, hoy)
    rest = _g(d, "restante")
    if rest is not None:
        e["saldo"] = rest
        e["pagada"] = rest <= 0.005
        e["pct"] = 0 if e["P"] <= 0 else max(0.0, min(1.0, 1 - rest / e["P"]))
        if e["pagada"]:
            e["proxima"], e["dias"], e["proxima_monto"] = None, None, None
    e["automatica"] = True
    return e


def _estado(d, pagos, hoy=None):
    """Todo lo que la pantalla necesita saber de una deuda."""
    hoy = hoy or date.today()
    P, n, cuota, ea, i = parametros(d)
    primera = date.fromisoformat(d["primera_cuota"])
    rows = cronograma(P, n, cuota, i, primera)
    pagado = sum(p["monto"] for p in pagos if p["tipo"] == "cuota")
    abonos = sum(p["monto"] for p in pagos if p["tipo"] == "abono")
    acum, cubiertas, cap_pagado, int_pagado = 0.0, 0, 0.0, 0.0
    for r in rows:
        if pagado + 0.01 >= acum + r["cuota"]:
            acum += r["cuota"]; cubiertas += 1; cap_pagado += r["capital"]; int_pagado += r["interes"]; r["pagada"] = True
        else:
            r["pagada"] = False
    parcial = max(pagado - acum, 0.0)
    saldo = max(P - cap_pagado - abonos, 0.0)
    pagada = saldo <= 0.005
    proxima = None if pagada else add_months(primera, cubiertas)
    # proyección con la cuota actual desde el saldo (si hubo abonos, termina antes)
    bal, k, intereses = saldo, 0, 0.0
    while bal > 0.005 and k < 1200:
        it = bal * i
        pago = min(cuota, bal + it)
        bal = bal + it - pago
        intereses += it
        k += 1
        if pago <= it + 1e-9 and i > 0:
            break                                      # la cuota no cubre ni el interés
    fin = add_months(proxima, k - 1) if proxima and k else None
    sig_monto = None if pagada else max(min(cuota, saldo * (1 + i)) - parcial, 0.0)
    return {"P": P, "n": n, "cuota": cuota, "ea": ea, "i": i, "rows": rows, "cubiertas": cubiertas, "saldo": saldo,
            "pagada": pagada, "proxima": proxima, "proxima_monto": sig_monto, "restantes": k, "int_restante": intereses,
            "fin": fin, "int_pagado": int_pagado, "int_total": sum(r["interes"] for r in rows), "abonos": abonos,
            "pct": 0 if P <= 0 else max(0.0, min(1.0, 1 - saldo / P)), "dias": (proxima - hoy).days if proxima else None,
            "costo_real": d["tasa_ea"] is None and float(d["cuota"] or 0) > 0}


def estrategia(deudas, extra):
    """deudas: [{'nombre','saldo','i','ea','cuota'}]. Simula avalancha, bola de nieve y solo cuotas."""
    def simular(orden_fn, extra_mes):
        ds = [dict(x, bal=x["saldo"]) for x in deudas if x["saldo"] > 0.005]
        presupuesto = sum(x["cuota"] for x in ds) + extra_mes
        mes, interes_total, fin = 0, 0.0, {}
        while any(x["bal"] > 0.005 for x in ds) and mes < 600:
            mes += 1
            vivos = [x for x in ds if x["bal"] > 0.005]
            libre = presupuesto
            for x in vivos:
                it = x["bal"] * x["i"]; x["bal"] += it; interes_total += it
                pago = min(x["cuota"], x["bal"]); x["bal"] -= pago; libre -= pago
            for x in sorted([y for y in ds if y["bal"] > 0.005], key=orden_fn):
                if libre <= 0.005:
                    break
                pago = min(libre, x["bal"]); x["bal"] -= pago; libre -= pago
            for x in ds:
                if x["bal"] <= 0.005 and x["nombre"] not in fin:
                    fin[x["nombre"]] = mes
        return {"meses": mes, "interes": interes_total, "fin": fin}
    base = simular(lambda x: x["bal"], 0)
    ava = simular(lambda x: (-x["ea"], x["bal"]), extra)
    nieve = simular(lambda x: x["bal"], extra)
    ordenes = {"avalancha": [x["nombre"] for x in sorted(deudas, key=lambda x: (-x["ea"], x["saldo"])) if x["saldo"] > 0.005],
               "nieve": [x["nombre"] for x in sorted(deudas, key=lambda x: x["saldo"]) if x["saldo"] > 0.005]}
    mejor = "avalancha" if ava["interes"] <= nieve["interes"] - 0.5 else "nieve"
    if abs(ava["interes"] - nieve["interes"]) < 0.5:
        mejor = "nieve"          # mismo costo: la bola de nieve se siente más rápida
    return {"base": base, "avalancha": ava, "nieve": nieve, "orden": ordenes, "mejor": mejor}


def detectar(c, desc, monto):
    """¿Este movimiento es el pago de alguna deuda registrada? Devuelve la fila de la deuda o None."""
    d = (desc or "").lower()
    for x in c.execute("SELECT * FROM deudas WHERE activo=1 AND COALESCE(origen,'manual')!='tarjeta'").fetchall():
        clave = (x["palabra_clave"] or x["nombre"] or "").lower().strip()
        if not clave or clave not in d:
            continue
        P, n, cuota, ea, i = parametros(x)
        if x["direccion"] == "debo" and monto < 0 and abs(monto) >= cuota * 0.9:
            return x
        if x["direccion"] == "me_deben" and monto > 0 and abs(monto) >= cuota * 0.9:
            return x
    return None


def registrar_automatico(c, desc, monto, fecha, mov_id):
    x = detectar(c, desc, monto)
    if not x:
        return None
    c.execute("INSERT INTO deuda_pagos(deuda_id,fecha,monto,tipo,mov_id,nota) VALUES(?,?,?,?,?,?)",
              (x["id"], fecha, abs(monto), "cuota", mov_id, "Detectado automáticamente"))
    if x["direccion"] == "debo":
        c.execute("UPDATE movs SET categoria='Deudas' WHERE id=?", (mov_id,))
    return x["nombre"]


def sincronizar_tarjeta(c, user_id, banco, movs, fecha_limite):
    """Crea o actualiza una deuda por cada compra a cuotas del extracto de la tarjeta. Devuelve (nuevas, actualizadas)."""
    import hashlib
    nuevas = actualizadas = 0
    for m in movs:
        if not m.get("cuotas"):
            continue
        k, n = m["cuotas"]
        total = abs(m["monto"])
        clave = hashlib.sha1(f"{user_id}|{banco}|{m['fecha']}|{m['descripcion']}|{total:.2f}".encode()).hexdigest()
        mv = m.get("tasa_mv") or 0.0
        ea = ((1 + mv / 100) ** 12 - 1) * 100 if mv > 0 else 0.0
        primera = add_months(date.fromisoformat(fecha_limite), -(k - 1)).isoformat() if fecha_limite else m["fecha"]
        ya = c.execute("SELECT id FROM deudas WHERE clave_ext=?", (clave,)).fetchone()
        if ya:
            c.execute("UPDATE deudas SET cuotas_fact=?, restante=?, cuota=? WHERE id=?",
                      (k, m.get("restante"), m["valor_cuota"], ya["id"]))
            actualizadas += 1
        else:
            c.execute("""INSERT INTO deudas(user_id,nombre,tipo,direccion,principal,n_cuotas,cuota,tasa_ea,primera_cuota,palabra_clave,
                origen,cuotas_fact,restante,tasa_mv,clave_ext) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (user_id, m["descripcion"], "Tarjeta a cuotas", "debo", total, n, m["valor_cuota"], ea, primera, "", "tarjeta",
                       k, m.get("restante"), mv, clave))
            nuevas += 1
    return nuevas, actualizadas
