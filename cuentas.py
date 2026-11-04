"""Cuentas dinámicas: tipo (ahorros, corriente, bajo monto), exención del 4x1000 y estimado del impuesto."""
TIPOS = {"ahorros": "Cuenta de ahorros", "corriente": "Cuenta corriente", "bajo_monto": "Depósito de bajo monto",
         "tarjeta": "Tarjeta de crédito"}
RESERVADOS = ("", "Manual", "Efectivo", "Otro")
GMF = 0.004          # 4 x 1000


def defaults(nombre):
    n = (nombre or "").lower()
    if any(k in n for k in ("nequi", "daviplata", "dale", "movii", "bre-b")):
        return "bajo_monto", 0       # la persona la marca como exenta si lo está (Nequi, por ejemplo, puede cobrarlo)
    if n.strip() == "nu" or n.startswith("nu "):
        return "ahorros", 1          # el extracto de Nu muestra "Impuesto del 4x1000 $0,00"
    return "ahorros", 0              # por defecto paga 4x1000; la persona la marca como exenta si lo está


def asegurar(c, user_id, nombre):
    """Crea la cuenta si no existe (cuando subes un extracto o llega una captura con un banco nuevo)."""
    if not nombre or nombre in RESERVADOS:
        return
    tipo, ex = defaults(nombre)
    c.execute("INSERT OR IGNORE INTO cuentas(user_id,nombre,entidad,tipo,exenta) VALUES(?,?,?,?,?)",
              (user_id, nombre, nombre, tipo, ex))


def asegurar_tarjeta(c, user_id, banco):
    """Cada tarjeta de crédito tiene su propia ficha (para su cuota de manejo): 'Tarjeta Nu'."""
    if not banco or banco in RESERVADOS:
        return
    c.execute("INSERT OR IGNORE INTO cuentas(user_id,nombre,entidad,tipo,exenta) VALUES(?,?,?,'tarjeta',0)",
              (user_id, f"Tarjeta {banco}", banco))


def manejo_tarjeta(c, user_id, banco):
    r = c.execute("""SELECT cuota_manejo FROM cuentas WHERE user_id=? AND tipo='tarjeta' AND activa=1
        AND (lower(entidad)=lower(?) OR lower(nombre)=lower(?)) ORDER BY id LIMIT 1""", (user_id, banco, f"Tarjeta {banco}")).fetchone()
    return (r["cuota_manejo"] or 0.0) if r else None


def resolver(c, user_id, entidad):
    """El banco que detecta una alerta (p. ej. 'Nu') -> el nombre de la cuenta de esa persona (p. ej. 'Nu Ahorros')."""
    if not entidad or entidad in RESERVADOS:
        return entidad
    r = c.execute("""SELECT nombre FROM cuentas WHERE user_id=? AND activa=1 AND (lower(nombre)=lower(?) OR lower(entidad)=lower(?))
        ORDER BY id LIMIT 1""", (user_id, entidad, entidad)).fetchone()
    if r:
        return r["nombre"]
    asegurar(c, user_id, entidad)
    return entidad


def nombres(c, fallback):
    n = [r["nombre"] for r in c.execute("SELECT DISTINCT nombre FROM cuentas WHERE activa=1 ORDER BY nombre")]
    return n or list(fallback)


def sembrar(c):
    """Primera vez: convierte los bancos que ya tenías en cuentas."""
    if c.execute("SELECT 1 FROM cuentas").fetchone():
        return
    for r in c.execute("""SELECT DISTINCT user_id, banco FROM movs UNION SELECT DISTINCT user_id, banco FROM saldos
                          UNION SELECT DISTINCT user_id, banco FROM pagos_tarjeta"""):
        asegurar(c, r["user_id"], r["banco"])


def listado(c, mes, saldos_actuales):
    """Cada cuenta con su saldo estimado, salidas del mes y 4x1000 estimado."""
    saldo = {(s["user_id"], s["banco"]): s for s in saldos_actuales}
    out = []
    for q in c.execute("""SELECT q.*, u.nombre AS dueno FROM cuentas q JOIN users u ON u.id=q.user_id
                          WHERE q.activa=1 ORDER BY u.nombre, q.nombre""").fetchall():
        salidas = c.execute("""SELECT COALESCE(SUM(-monto),0) v, COUNT(*) n FROM movs WHERE user_id=? AND banco=? AND monto<0
            AND origen!='tarjeta' AND substr(fecha,1,7)=?""", (q["user_id"], q["nombre"], mes)).fetchone()
        total = c.execute("SELECT COUNT(*) n FROM movs WHERE user_id=? AND banco=?", (q["user_id"], q["nombre"])).fetchone()["n"]
        cobros = 0 if q["tipo"] == "tarjeta" else c.execute("""SELECT COALESCE(SUM(-monto),0) v FROM movs WHERE user_id=? AND banco=? AND categoria='Intereses y cobros'
            AND monto<0 AND substr(fecha,1,7)=?""", (q["user_id"], q["nombre"], mes)).fetchone()["v"]
        s = saldo.get((q["user_id"], q["nombre"]))
        gmf = 0.0 if (q["exenta"] or q["tipo"] == "tarjeta") else salidas["v"] * GMF
        manejo = q["cuota_manejo"] or 0.0
        # si el extracto ya trae el cobro, se usa el real; si no, la cuota configurada
        costo = gmf + (cobros if cobros > 0 else manejo)
        out.append({"q": q, "salidas": salidas["v"], "gmf": gmf, "saldo": s["estimado"] if s else None, "movs": total,
                    "manejo": manejo, "cobros": cobros, "costo": costo})
    return out


def avisos_exencion(lista):
    """Más de una cuenta de ahorros exenta por persona suele no ser válido: se avisa, no se bloquea."""
    por = {}
    for x in lista:
        if x["q"]["tipo"] == "ahorros" and x["q"]["exenta"]:
            por.setdefault(x["q"]["user_id"], []).append(x["q"]["nombre"])
    return [f"{x['q']['dueno']} tiene {len(por[x['q']['user_id']])} cuentas de ahorros marcadas como exentas "
            f"({', '.join(por[x['q']['user_id']])}). Normalmente solo una cuenta de ahorros por persona puede estar exenta; confírmalo con tu banco."
            for x in lista if len(por.get(x["q"]["user_id"], [])) > 1 and x["q"]["tipo"] == "ahorros" and x["q"]["exenta"]][:1]
