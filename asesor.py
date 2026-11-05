"""Asesor financiero con IA (Claude Haiku). Solo manda un RESUMEN de cifras: sin nombres de personas, cédula,
números de cuenta ni descripciones de movimientos."""
import os
from datetime import date, timedelta

import avisos
import cuentas
import deudas
import metas
import saldos

MODELO = os.environ.get("AI_MODEL", "claude-haiku-5-5")
MAX_DIA = int(os.environ.get("AI_MAX_DIA", 30))

SISTEMA = """Eres el asesor de finanzas personales de una familia colombiana que usa Nu, Nequi y Daviplata. Hablas en español de Colombia, \
cercano y directo, sin sermones. Todas las cifras están en pesos colombianos (COP).

Reglas:
- Usa SOLO las cifras del contexto; no inventes saldos, tasas ni fechas. Si falta un dato, dilo y di cómo conseguirlo.
- Da recomendaciones concretas con números del contexto (cuánto, hasta cuándo). Prioriza: primero lo que ahorra más plata o evita un cobro.
- Sé breve: un resumen de una línea, de 3 a 5 acciones numeradas y, si aplica, una alerta. Máximo ~250 palabras en los análisis y ~150 en las respuestas a preguntas.
- Formato: **negritas** para las cifras clave y listas con "-" o "1.". No uses títulos ni tablas.
- No eres un asesor financiero profesional: no prometas rendimientos ni recomiendes productos de inversión específicos. Para decisiones grandes (créditos, inversiones), sugiere confirmar con el banco.

Hechos de la tarjeta de crédito Nu de esta familia (de su contrato y de los correos de Nu):
- Los intereses de la tarjeta se calculan de forma compuesta y diaria (año de 360 días) y solo se pagan si no se paga el total del extracto; pagar el total antes de la fecha límite evita intereses.
- Las compras de una sola cuota no pagan interés (0 %) si se paga el total; hay compras a cuotas sin intereses con aliados (por ejemplo Mercado Libre y Mac Center).
- Nu Plus da puntos por las compras que se pueden redimir para pagar parte de la deuda o reducir los intereses de una compra.
- Pagar antes de la fecha límite ayuda a tener reportes positivos en centrales de riesgo.
- Los avances en cajeros cuestan (comisión fija más cobros de terceros); conviene evitarlos.
- La cuota de manejo de la tarjeta depende de las condiciones vigentes en la app de Nu; el contexto dice si aplica y cuánto.
- Las cajitas de Nu pagan rendimientos a diario; cumplir la meta mensual de la cajita sube la tasa unos puntos E.A."""

MODOS = {
    "analisis": ("Analiza mis finanzas de este mes y dime en qué enfocarme. Cubre: ritmo de gasto, deudas, tarjeta, ahorro y cualquier alerta.", "medium", 4000),
    "tarjeta": ("Dime cómo sacarle el mayor provecho a mi tarjeta de crédito Nu y evitar costos: uso del cupo, pago, cuotas, cuota de manejo, "
                "puntos y compras a cuotas. Si hay un riesgo de pagar intereses o cargos, dímelo primero.", "medium", 4000),
}


def hay_clave():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _pesos(v):
    return avisos.cop(v or 0.0)


def contexto(c, hoy=None):
    """Resumen en texto de la situación de la familia. Sin datos personales."""
    hoy = hoy or date.today()
    mes = hoy.strftime("%Y-%m")
    ant = (hoy.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    L = [f"Fecha de hoy: {hoy.isoformat()}. Mes en curso: {mes}."]

    def tot(m):
        r = c.execute(f"""SELECT COALESCE(SUM(CASE WHEN monto>0 THEN monto END),0) i, COALESCE(SUM(CASE WHEN monto<0 THEN -monto END),0) g
            FROM movs WHERE substr(fecha,1,7)=? AND {avisos.EXC}""", (m,)).fetchone()
        return r["i"], r["g"]
    i1, g1 = tot(mes)
    i0, g0 = tot(ant)
    L.append(f"Este mes: ingresos {_pesos(i1)}, gastos {_pesos(g1)}. Mes anterior: ingresos {_pesos(i0)}, gastos {_pesos(g0)}.")
    cats = c.execute(f"""SELECT categoria, SUM(-monto) v FROM movs WHERE monto<0 AND substr(fecha,1,7)=? AND {avisos.EXC}
        GROUP BY categoria ORDER BY v DESC LIMIT 8""", (mes,)).fetchall()
    prev = {r["categoria"]: r["v"] for r in c.execute(f"""SELECT categoria, SUM(-monto) v FROM movs WHERE monto<0
        AND substr(fecha,1,7)=? AND {avisos.EXC} GROUP BY categoria""", (ant,))}
    if cats:
        L.append("Gasto por categoría este mes (y el mes anterior): " + "; ".join(
            f"{r['categoria']} {_pesos(r['v'])} (antes {_pesos(prev.get(r['categoria'], 0))})" for r in cats) + ".")
    subs = c.execute("""SELECT descripcion, MAX(-monto) v FROM movs WHERE monto<0 AND categoria='Suscripciones' AND fecha>=?
        GROUP BY descripcion ORDER BY v DESC LIMIT 8""", ((hoy - timedelta(days=45)).isoformat(),)).fetchall()
    if subs:
        L.append("Suscripciones detectadas: " + "; ".join(f"{r['descripcion']} {_pesos(r['v'])}" for r in subs) + ".")
    for p in c.execute("SELECT categoria, limite FROM presupuestos").fetchall():
        g = c.execute(f"""SELECT COALESCE(SUM(-monto),0) v FROM movs WHERE monto<0 AND categoria=? AND substr(fecha,1,7)=? AND {avisos.EXC}""",
                      (p["categoria"], mes)).fetchone()["v"]
        L.append(f"Presupuesto {p['categoria']}: gastado {_pesos(g)} de {_pesos(p['limite'])} ({g / p['limite'] * 100:.0f} %).")
    # saldos y cajitas
    for s in saldos.actuales(c):
        t = c.execute("SELECT tipo, exenta FROM cuentas WHERE user_id=? AND nombre=? LIMIT 1", (s["user_id"], s["banco"])).fetchone()
        extra = f" (exenta de 4x1000)" if t and t["exenta"] else (" (paga 4x1000)" if t and t["tipo"] != "tarjeta" and s["banco"] != "Efectivo" else "")
        L.append(f"Saldo estimado en {s['banco']}: {_pesos(s['estimado'])}{extra}.")
        cj = s.get("cajitas")
        if cj:
            L.append(f"  Cajitas de {s['banco']}: {_pesos(cj['saldo'])} al {cj['ea']:.2f} % E.A., rinden ≈ {_pesos(cj['rend_dia'])} al día y {_pesos(cj['rend_mes'])} al mes.")
    d = metas.disponible(c, hoy)
    if d:
        L.append(f"Cuánto puede gastar: saldo total {_pesos(d['saldo'])}, comprometido hasta el {d['fin']} {_pesos(d['comprometido'])}, "
                 f"libre {_pesos(d['libre'])} = {_pesos(d['por_dia'])} por día durante {d['dias']} días. Hoy lleva gastado {_pesos(d['gastado_hoy'])}.")
        for x in d["comp"][:8]:
            L.append(f"  Compromiso: {x['que']}{' el ' + x['fecha'].isoformat() if x['fecha'] else ''} por {_pesos(x['monto'])}.")
    # deudas
    for dd in c.execute("SELECT * FROM deudas WHERE activo=1 AND direccion='debo' AND COALESCE(origen,'manual')!='tarjeta'").fetchall():
        pagos = c.execute("SELECT monto,tipo FROM deuda_pagos WHERE deuda_id=?", (dd["id"],)).fetchall()
        e = deudas.estado(dd, pagos, hoy)
        if not e["pagada"]:
            L.append(f"Deuda '{dd['nombre']}' ({dd['tipo']}): saldo {_pesos(e['saldo'])}, cuota {_pesos(e['cuota'])}, {e['ea']:.2f} % E.A.{' (costo real, incluye cobros)' if e['costo_real'] else ''}, "
                     f"quedan {e['restantes']} cuotas hasta {e['fin']}, intereses por pagar {_pesos(e['int_restante'])}.")
    # tarjeta
    for t in c.execute("""SELECT p.* FROM pagos_tarjeta p WHERE p.id=(SELECT id FROM pagos_tarjeta x WHERE x.user_id=p.user_id AND x.banco=p.banco
            ORDER BY x.fecha_limite DESC, x.id DESC LIMIT 1)""").fetchall():
        uso = (t["usado"] / t["cupo"] * 100) if t["cupo"] and t["usado"] is not None else None
        L.append(f"Tarjeta de crédito {t['banco']}: deuda total {_pesos(t['deuda_total'])}, cupo {_pesos(t['cupo'])}"
                 f"{f' (usa el {uso:.0f} %)' if uso is not None else ''}, pago mínimo {_pesos(t['pago_minimo'])}, pago total sin intereses {_pesos(t['a_pagar'])}, "
                 f"fecha límite {t['fecha_limite']}, {'ya marcada como pagada' if t['pagado'] else 'sin pagar todavía'}.")
        f = cuentas.ficha_tarjeta(c, t["user_id"], t["banco"])
        if f and f["cuota_manejo"]:
            ciclo = cuentas.ciclo_tarjeta(c, t["user_id"], t["banco"], hoy)
            if f["manejo_cond"] == "sin_uso":
                L.append(f"  Cuota de manejo {_pesos(f['cuota_manejo'])}: solo se cobra si no usa la tarjeta en el ciclo; "
                         + (f"este ciclo (hasta el {ciclo['fin']}) {'ya la usó' if ciclo['uso'] else 'AÚN NO la ha usado'}." if ciclo else "ciclo desconocido."))
            else:
                L.append(f"  Cuota de manejo {_pesos(f['cuota_manejo'])} cada mes.")
    for q in c.execute("SELECT * FROM deudas WHERE activo=1 AND origen='tarjeta'").fetchall():
        e = deudas.estado(q, [], hoy)
        if not e["pagada"]:
            L.append(f"  Compra a cuotas en la tarjeta '{q['nombre']}': cuota {q['cuotas_fact']} de {q['n_cuotas']} de {_pesos(e['cuota'])}, quedan {_pesos(e['saldo'])}"
                     f"{', sin intereses' if not q['tasa_mv'] else f', {q['tasa_mv']:.2f} % mensual'}.")
    for m in c.execute("SELECT * FROM metas WHERE activa=1").fetchall():
        aps = c.execute("SELECT * FROM meta_aportes WHERE meta_id=?", (m["id"],)).fetchall()
        e = metas.estado_meta(m, aps, hoy)
        L.append(f"Meta '{m['nombre']}' ({'mensual' if m['periodo'] == 'mensual' else 'objetivo'}): {_pesos(e['aportado'])} de {_pesos(e['objetivo'])}"
                 f"{' (cumplida)' if e['cumplida'] else f', faltan {_pesos(e['falta'])}'}{f', bono +{e['bono']:.2f} pts E.A.' if e['bono'] else ''}.")
    costo = 0.0
    for x in cuentas.listado(c, mes, saldos.actuales(c)):
        costo += x["costo"]
    if costo:
        L.append(f"Costo de tener las cuentas este mes (4x1000, cuotas de manejo, comisiones): {_pesos(costo)}.")
    return "\n".join(L)


def usos_hoy(c, user_id):
    return c.execute("SELECT COUNT(*) n FROM asesor_hist WHERE user_id=? AND substr(creado,1,10)=?",
                     (user_id, avisos.ahora().date().isoformat())).fetchone()["n"]


def consultar(mensajes, effort="medium", max_tokens=4000):
    """Llama a Claude. mensajes: lista de {'role','content'} que empieza y termina en 'user'. Devuelve (texto, aviso)."""
    import anthropic                                   # se importa aquí: la app arranca aunque no haya clave ni SDK
    cliente = anthropic.Anthropic(timeout=60.0, max_retries=2)
    r = cliente.messages.create(model=MODELO, max_tokens=max_tokens, system=SISTEMA, messages=mensajes,
                                output_config={"effort": effort})
    if r.stop_reason == "refusal":
        return "", "El asistente no pudo responder a esta consulta."
    texto = next((b.text for b in r.content if b.type == "text"), "").strip()
    aviso = "La respuesta quedó cortada por longitud; vuelve a preguntar de forma más corta." if r.stop_reason == "max_tokens" else ""
    return texto, aviso


def mensaje_de_error(e):
    try:
        import anthropic
    except Exception:
        return "No está instalada la librería de Anthropic en el servidor."
    if isinstance(e, anthropic.AuthenticationError):
        return "La clave de Anthropic no es válida. Revisa ANTHROPIC_API_KEY en Dokploy."
    if isinstance(e, anthropic.PermissionDeniedError):
        return "La clave de Anthropic no tiene permiso para este modelo."
    if isinstance(e, anthropic.RateLimitError):
        return "Anthropic está limitando las consultas. Intenta de nuevo en un minuto."
    if isinstance(e, anthropic.APIConnectionError):
        return "No pude conectarme con Anthropic. Revisa la salida a internet del servidor."
    if isinstance(e, anthropic.APIStatusError):
        return f"Anthropic respondió con un error ({e.status_code}). Intenta de nuevo."
    return "No se pudo obtener el consejo en este momento."
