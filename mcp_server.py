"""Servidor MCP de Cuadre (Streamable HTTP, sin estado): deja que una IA (ChatGPT, Claude, etc.) consulte las finanzas de la familia
y actúe como asesor. Por defecto es de SOLO LECTURA; registrar movimientos exige MCP_ESCRITURA=1.
Protocolo: JSON-RPC 2.0 sobre POST. Autenticación: token propio del MCP (no el de captura), por cabecera Bearer o dentro de la URL."""
import json
import os
import re
import time
from datetime import date, timedelta

import agenda
import asesor
import captura
import lector
import metas
import saldos

VERSIONES = ("2025-06-18", "2025-03-26", "2024-11-05")
ESCRITURA = os.environ.get("MCP_ESCRITURA", "0") == "1"
NOMBRES = os.environ.get("MCP_NOMBRES", "0") == "1"      # por defecto se oculta el apellido de las personas en las transferencias

INSTRUCCIONES = (
    "Eres asesor financiero de una familia colombiana que usa Cuadre. Los valores están en pesos colombianos (COP). "
    "Empieza por resumen_financiero y pide más detalle solo si lo necesitas. Sé concreto, no inventes cifras y no des garantías: "
    "recomienda y explica el porqué. Los traslados entre cuentas propias y los pagos de tarjeta no son ingresos ni gastos."
)

_ULTIMOS = {}      # token -> [instantes], límite de pedidos por minuto


def limitado(token, maximo=90):
    ahora = time.time()
    v = [t for t in _ULTIMOS.get(token, []) if ahora - t < 60]
    v.append(ahora)
    _ULTIMOS[token] = v
    return len(v) > maximo


def _ocultar(desc):
    """'Enviaste a LINA MARIA PADILLA NAVAJA' -> 'Enviaste a Lina': no se entregan apellidos de terceros salvo que se pida."""
    if NOMBRES or not desc:
        return desc
    m = re.match(r"^(recibiste de|enviaste a|para|de|envio con bre-b a:|recibi por bre-b de:|pago recibido de)\s+(.+)$", desc.strip(), re.I)
    if not m:
        return desc
    primero = re.sub(r"[^A-Za-zÁÉÍÓÚÑáéíóúñ]", "", m[2].split()[0]) if m[2].split() else ""
    return f"{m[1]} {primero.capitalize()}" if primero else m[1]


def _mes(v, hoy):
    return v if v and re.fullmatch(r"\d{4}-\d{2}", v) else hoy.strftime("%Y-%m")


def _r(x):
    return None if x is None else round(float(x), 2)


# ---------- herramientas ----------
def t_resumen(c, u, a, h):
    return asesor.contexto(c)


def t_saldos(c, u, a, h):
    return [{"cuenta": x["banco"], "titular": x["nombre"], "saldo_estimado": _r(x["estimado"]), "base": _r(x["base"]), "fecha_base": x["fecha"],
             "fuente_base": x["fuente"], "movimientos_desde_la_base": x["n"], "pendientes_de_extracto": x["pend"],
             "gmf_4x1000_estimado": _r(x.get("gmf")),
             "cajitas": ({"saldo": _r(x["cajitas"]["saldo"]), "tasa_ea": _r(x["cajitas"]["ea"]), "rinde_al_mes": _r(x["cajitas"]["rend_mes"])}
                         if x.get("cajitas") else None)} for x in saldos.actuales(c)]


def t_movimientos(c, u, a, h):
    hoy = date.today()
    mes = a.get("mes")
    w, p = ["1=1"], []
    if mes:
        w.append("substr(m.fecha,1,7)=?"); p.append(_mes(mes, hoy))
    if a.get("categoria"):
        w.append("m.categoria=?"); p.append(str(a["categoria"]))
    if a.get("texto"):
        w.append("lower(m.descripcion) LIKE ?"); p.append(f"%{str(a['texto']).lower()}%")
    if a.get("tipo") == "gasto":
        w.append("m.monto<0")
    elif a.get("tipo") == "ingreso":
        w.append("m.monto>0")
    lim = max(1, min(int(a.get("limite") or 50), 200))
    rows = c.execute(f"""SELECT m.fecha,m.banco,m.descripcion,m.monto,m.categoria,m.estado,u.nombre FROM movs m JOIN users u ON u.id=m.user_id
        WHERE {' AND '.join(w)} ORDER BY m.fecha DESC, m.id DESC LIMIT ?""", p + [lim]).fetchall()
    return {"nota": "Las categorías «Traslado propio» y «Pago de tarjeta» no cuentan como ingreso ni gasto.",
            "movimientos": [{"fecha": r["fecha"], "cuenta": r["banco"], "persona": r["nombre"], "descripcion": _ocultar(r["descripcion"]),
                             "monto": _r(r["monto"]), "categoria": r["categoria"], "estado": r["estado"]} for r in rows]}


def t_gastos(c, u, a, h):
    mes = _mes(a.get("mes"), date.today())
    exc = h["EXC"]
    tot = c.execute(f"""SELECT COALESCE(SUM(CASE WHEN monto>0 THEN monto END),0) ing, COALESCE(SUM(CASE WHEN monto<0 THEN -monto END),0) gas
        FROM movs WHERE substr(fecha,1,7)=? AND {exc}""", (mes,)).fetchone()
    cats = c.execute(f"""SELECT categoria, SUM(-monto) v, COUNT(*) n FROM movs WHERE monto<0 AND {exc} AND substr(fecha,1,7)=?
        GROUP BY categoria ORDER BY v DESC""", (mes,)).fetchall()
    pers = c.execute(f"""SELECT u.nombre, COALESCE(SUM(CASE WHEN m.monto>0 THEN m.monto END),0) ing, COALESCE(SUM(CASE WHEN m.monto<0 THEN -m.monto END),0) gas
        FROM movs m JOIN users u ON u.id=m.user_id WHERE substr(m.fecha,1,7)=? AND m.{exc} GROUP BY u.id""", (mes,)).fetchall()
    return {"mes": mes, "ingresos": _r(tot["ing"]), "gastos": _r(tot["gas"]), "balance": _r(tot["ing"] - tot["gas"]),
            "gastos_por_categoria": [{"categoria": r["categoria"], "total": _r(r["v"]), "movimientos": r["n"]} for r in cats],
            "por_persona": [{"persona": r["nombre"], "ingresos": _r(r["ing"]), "gastos": _r(r["gas"])} for r in pers]}


def t_deudas(c, u, a, h):
    todas = h["cargar_deudas"](c)
    out = {"deudas": [], "tarjetas": []}
    for x in todas:
        d, e = x["d"], x["e"]
        if e["pagada"] or d["origen"] == "tarjeta":
            continue
        out["deudas"].append({"nombre": d["nombre"], "direccion": "debo" if d["direccion"] == "debo" else "me deben", "titular": d["responsable"],
                              "saldo": _r(e["saldo"]), "cuota": _r(e["cuota"]), "cuotas_pagadas": e["cubiertas"], "cuotas_total": e["n"],
                              "tasa_ea_pct": _r(e["ea"]), "proxima_cuota": e["proxima"].isoformat() if e.get("proxima") else None})
    for x in h["cargar_tarjetas"](c, todas):
        t = x["t"]
        out["tarjetas"].append({"tarjeta": t["banco"], "titular": t["responsable"], "deuda_total": _r(t["deuda_total"]), "pago_minimo": _r(t["pago_minimo"]),
                                "pago_sin_intereses": _r(t["a_pagar"]), "fecha_limite": t["fecha_limite"], "pagado": bool(t["pagado"]),
                                "cupo": _r(t["cupo"]), "usado": _r(t["usado"]), "disponible": _r(t["disponible"]),
                                "cuota_de_manejo_mes": _r(x["manejo"]),
                                "compras_a_cuotas": [{"nombre": q["d"]["nombre"], "cuota": _r(q["e"]["cuota"]), "saldo": _r(q["e"]["saldo"])} for q in x["cuotas"]]})
    return out


def t_agenda(c, u, a, h):
    dias = max(7, min(int(a.get("dias") or 45), 180))
    return [{"fecha": e["fecha"].isoformat(), "que": e["titulo"], "detalle": e["detalle"], "monto": _r(e["monto"]), "tipo": e["tipo"]}
            for e in agenda.eventos(c, date.today(), dias)]


def t_disponible(c, u, a, h):
    d = metas.disponible(c)
    if not d:
        return {"mensaje": "Aún no hay saldos registrados."}
    return {"puede_gastar_por_dia": _r(d["por_dia"]), "dias_hasta": d["fin"].isoformat(), "saldo_hoy": _r(d["saldo"]),
            "ingresos_esperados": _r(d["ingresos_total"]), "comprometido": _r(d["comprometido"]), "libre": _r(d["libre"]),
            "gastado_hoy": _r(d["gastado_hoy"]), "limitante": d.get("lim_que"),
            "pagos_pendientes": [{"que": x["que"], "fecha": x["fecha"].isoformat() if x["fecha"] else None, "monto": _r(x["monto"])} for x in d["comp"]],
            "estado": d["estado"]}


def t_registrar(c, u, a, h):
    if not ESCRITURA:
        raise ValueError("El registro de movimientos está desactivado en este servidor (MCP_ESCRITURA).")
    tipo = a.get("tipo")
    if tipo not in ("gasto", "ingreso"):
        raise ValueError("tipo debe ser 'gasto' o 'ingreso'")
    monto = abs(float(a.get("monto")))
    if monto <= 0:
        raise ValueError("monto debe ser mayor a cero")
    reglas = [(r["kw"], r["categoria"]) for r in c.execute("SELECT kw,categoria FROM reglas")]
    fecha = a.get("fecha") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(a.get("fecha") or "")) else date.today().isoformat()
    captura.insertar(c, u["id"], tipo, monto, str(a.get("descripcion") or tipo.capitalize())[:120], fecha, str(a.get("cuenta") or "Efectivo")[:40], "mcp", reglas)
    c.commit()
    return {"ok": True, "mensaje": f"{tipo.capitalize()} de {monto:,.0f} registrado como pendiente de extracto."}


def _tool(nombre, desc, props=None, req=None, solo_lectura=True):
    return {"name": nombre, "description": desc,
            "inputSchema": {"type": "object", "properties": props or {}, **({"required": req} if req else {})},
            "annotations": {"readOnlyHint": solo_lectura, "destructiveHint": False, "idempotentHint": solo_lectura, "openWorldHint": False}}


HERRAMIENTAS = {
    "resumen_financiero": (t_resumen, _tool("resumen_financiero", "Panorama completo de la familia en texto: saldos, ingresos y gastos del mes y del anterior, deudas, tarjetas, metas, "
                                            "presupuestos, ingresos fijos y costos bancarios. Úsala primero.")),
    "saldos_cuentas": (t_saldos, _tool("saldos_cuentas", "Saldo estimado de cada cuenta (Nu, Nequi, Daviplata, efectivo), con cajitas y 4x1000 estimado.")),
    "movimientos": (t_movimientos, _tool("movimientos", "Lista movimientos (ingresos y gastos) con filtros.", {
        "mes": {"type": "string", "description": "Mes AAAA-MM. Si se omite, todos."}, "categoria": {"type": "string"},
        "texto": {"type": "string", "description": "Texto que debe contener la descripción."},
        "tipo": {"type": "string", "enum": ["gasto", "ingreso"]}, "limite": {"type": "integer", "description": "Máximo 200 (por defecto 50)."}})),
    "gastos_por_categoria": (t_gastos, _tool("gastos_por_categoria", "Ingresos, gastos y balance de un mes, con gastos por categoría y por persona.",
                                             {"mes": {"type": "string", "description": "AAAA-MM. Por defecto el mes actual."}})),
    "deudas_y_tarjetas": (t_deudas, _tool("deudas_y_tarjetas", "Créditos y tarjetas: saldo, cuotas, tasa E.A., fecha límite, pago mínimo, pago sin intereses y compras a cuotas.")),
    "agenda": (t_agenda, _tool("agenda", "Próximos pagos e ingresos esperados (cuotas, tarjeta, sueldos, metas).",
                               {"dias": {"type": "integer", "description": "Ventana en días (7 a 180, por defecto 45)."}})),
    "disponible_diario": (t_disponible, _tool("disponible_diario", "Cuánto puede gastar la familia por día hasta fin de mes sin quedarse corta en los pagos que vienen.")),
}
if ESCRITURA:
    HERRAMIENTAS["registrar_movimiento"] = (t_registrar, _tool("registrar_movimiento", "Registra un gasto o ingreso a nombre del usuario (queda pendiente hasta el extracto).", {
        "tipo": {"type": "string", "enum": ["gasto", "ingreso"]}, "monto": {"type": "number", "description": "En pesos, positivo."},
        "descripcion": {"type": "string"}, "cuenta": {"type": "string", "description": "Nu, Nequi, Daviplata o Efectivo."},
        "fecha": {"type": "string", "description": "AAAA-MM-DD"}}, ["tipo", "monto"], solo_lectura=False))


def _resp(i, resultado=None, error=None):
    r = {"jsonrpc": "2.0", "id": i}
    if error:
        r["error"] = error
    else:
        r["result"] = resultado
    return r


def procesar(msg, db, usuario, helpers):
    """Atiende un mensaje JSON-RPC. Devuelve la respuesta (dict) o None si es una notificación."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _resp(None, error={"code": -32600, "message": "Solicitud inválida"})
    metodo, i, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if i is None and metodo and metodo.startswith("notifications/"):
        return None
    if metodo == "initialize":
        pedida = p.get("protocolVersion")
        return _resp(i, {"protocolVersion": pedida if pedida in VERSIONES else VERSIONES[1], "capabilities": {"tools": {"listChanged": False}},
                         "serverInfo": {"name": "cuadre", "title": "Cuadre · finanzas de la familia", "version": "1.0.0"}, "instructions": INSTRUCCIONES})
    if metodo == "ping":
        return _resp(i, {})
    if metodo == "tools/list":
        return _resp(i, {"tools": [t for _, t in HERRAMIENTAS.values()]})
    if metodo == "tools/call":
        nombre, args = p.get("name"), p.get("arguments") or {}
        if nombre not in HERRAMIENTAS:
            return _resp(i, error={"code": -32602, "message": f"Herramienta desconocida: {nombre}"})
        c = db()
        try:
            datos = HERRAMIENTAS[nombre][0](c, usuario, args if isinstance(args, dict) else {}, helpers)
            texto = datos if isinstance(datos, str) else json.dumps(datos, ensure_ascii=False)
            return _resp(i, {"content": [{"type": "text", "text": texto}], "isError": False})
        except Exception as e:                       # el error se devuelve a la IA para que pueda corregir su llamada
            return _resp(i, {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
        finally:
            c.close()
    return _resp(i, error={"code": -32601, "message": f"Método no soportado: {metodo}"})
