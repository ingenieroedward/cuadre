"""Saldo por cuenta: último saldo conocido (extracto o ajuste manual) + los movimientos posteriores."""
import avisos

CORTE_EXTRACTO = 2_147_483_647   # los movimientos de la fecha de cierre ya están dentro del saldo del extracto


def guardar(c, user_id, banco, fecha, saldo, corte_id, fuente):
    c.execute("""INSERT INTO saldos(user_id,banco,fecha,saldo,corte_id,fuente) VALUES(?,?,?,?,?,?)
        ON CONFLICT(user_id,banco,fecha,fuente) DO UPDATE SET saldo=excluded.saldo, corte_id=excluded.corte_id""",
              (user_id, banco, fecha, saldo, corte_id, fuente))


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
        out.append({"id": s["id"], "user_id": s["user_id"], "nombre": s["nombre"], "banco": s["banco"], "fecha": s["fecha"],
                    "base": s["saldo"], "fuente": s["fuente"], "delta": delta["v"], "n": delta["n"], "pend": delta["p"] or 0,
                    "estimado": s["saldo"] + delta["v"]})
    return out
