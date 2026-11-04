"""Saldo por cuenta: último saldo conocido (extracto o ajuste manual) + los movimientos posteriores."""
from datetime import date

import avisos

CORTE_EXTRACTO = 2_147_483_647   # los movimientos de la fecha de cierre ya están dentro del saldo del extracto


def guardar(c, user_id, banco, fecha, saldo, corte_id, fuente):
    c.execute("""INSERT INTO saldos(user_id,banco,fecha,saldo,corte_id,fuente) VALUES(?,?,?,?,?,?)
        ON CONFLICT(user_id,banco,fecha,fuente) DO UPDATE SET saldo=excluded.saldo, corte_id=excluded.corte_id""",
              (user_id, banco, fecha, saldo, corte_id, fuente))


def guardar_cajitas(c, user_id, banco, fecha, info):
    cj, cu = info.get("cajitas"), info.get("cuenta_nu") or {}
    if not cj:
        return
    c.execute("""INSERT INTO cajitas(user_id,banco,fecha,saldo_cajitas,ea_cajitas,rend_cajitas,saldo_cuenta,ea_cuenta,rend_cuenta)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,banco,fecha) DO UPDATE SET saldo_cajitas=excluded.saldo_cajitas,
        ea_cajitas=excluded.ea_cajitas, rend_cajitas=excluded.rend_cajitas, saldo_cuenta=excluded.saldo_cuenta,
        ea_cuenta=excluded.ea_cuenta, rend_cuenta=excluded.rend_cuenta""",
              (user_id, banco, fecha, cj["saldo"], cj["ea"], cj["rend"], cu.get("saldo"), cu.get("ea"), cu.get("rend")))


def actuales(c):
    """Una fila por (persona, banco): saldo base, movimientos posteriores y saldo estimado hoy."""
    filas = c.execute("""SELECT s.*, u.nombre FROM saldos s JOIN users u ON u.id=s.user_id
        WHERE s.id = (SELECT id FROM saldos x WHERE x.user_id=s.user_id AND x.banco=s.banco ORDER BY x.fecha DESC, x.id DESC LIMIT 1)
        ORDER BY u.nombre, s.banco""").fetchall()
    out = []
    for s in filas:
        delta = c.execute("""SELECT COALESCE(SUM(monto),0) v, COUNT(*) n, SUM(estado='provisional') p FROM movs
            WHERE user_id=? AND banco=? AND origen!='tarjeta' AND (fecha > ? OR (fecha=? AND id > ?))""",
                          (s["user_id"], s["banco"], s["fecha"], s["fecha"], s["corte_id"])).fetchone()
        caj = c.execute("""SELECT * FROM cajitas WHERE user_id=? AND banco=? ORDER BY fecha DESC LIMIT 1""", (s["user_id"], s["banco"])).fetchone()
        cfg = c.execute("SELECT ea_actual FROM rend_cfg WHERE user_id=? AND banco=?", (s["user_id"], s["banco"])).fetchone()
        info_caj, acum = None, 0.0
        ea_cfg = cfg["ea_actual"] if cfg and cfg["ea_actual"] is not None else None
        if (caj and caj["ea_cajitas"] is not None) or ea_cfg is not None:
            ea_ext = caj["ea_cajitas"] if caj and caj["ea_cajitas"] is not None else None
            ea = ea_cfg if ea_cfg is not None else ea_ext
            # si no hay desglose de cajitas en un extracto, se supone que todo el saldo está en cajitas
            est = max(s["saldo"] + delta["v"] - ((caj["saldo_cuenta"] or 0) if caj else 0), 0.0)
            dias = max((date.today() - date.fromisoformat(s["fecha"])).days, 0)
            acum = est * ((1 + ea / 100) ** (dias / 365) - 1)           # los rendimientos se pagan a diario
            info_caj = {"ea_extracto": ea_ext, "ea": ea, "extra": (ea - ea_ext) if ea_ext is not None else 0.0, "saldo": est + acum,
                        "fecha": caj["fecha"] if caj else s["fecha"], "base": s["fecha"], "dias": dias, "acum": acum,
                        "rend_dia": (est + acum) * ((1 + ea / 100) ** (1 / 365) - 1),
                        "rend_mes": (est + acum) * ((1 + ea / 100) ** (1 / 12) - 1),
                        "rend_extracto": caj["rend_cajitas"] if caj else None}
        out.append({"cajitas": info_caj, "id": s["id"], "user_id": s["user_id"], "nombre": s["nombre"], "banco": s["banco"], "fecha": s["fecha"],
                    "base": s["saldo"], "fuente": s["fuente"], "delta": delta["v"], "n": delta["n"], "pend": delta["p"] or 0,
                    "estimado": s["saldo"] + delta["v"] + acum})
    return out
