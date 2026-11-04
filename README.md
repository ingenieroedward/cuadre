# Cuadre — finanzas de la familia

App web (FastAPI + SQLite), instalable en el celular (PWA): sube extractos PDF de varios bancos, resumen por
mes/persona/categoría, ingresos y gastos manuales, presupuestos, recordatorios y avisos push.

## Despliegue en Dokploy
1. Servicio **Compose** apuntando a este repositorio (`docker-compose.yml`).
2. Variable opcional: `OWN_NAMES=edward david diaz padilla,edwsystem` (tus nombres en transferencias propias; se marcan "Traslado propio").
3. Dominio con HTTPS (necesario para los avisos push) al puerto `8000`.
4. Abre el sitio: la primera vez pide **crear el administrador** (usuario y clave). Nada de usuarios en variables de entorno.
5. Los datos (base de datos, llave de sesión y llaves de avisos) viven en el volumen `cuadre_data`. Respáldalo.

## Captura en el momento (sin esperar el extracto)
- **Botón +** en cualquier pantalla (y `/rapido?monto=12000&desc=Taxi`, útil para atajos de iPhone): anota un gasto o ingreso.
  Si eliges un banco queda *Pendiente* y el extracto lo confirma solo; si eliges Efectivo queda confirmado.
- **Notificaciones del celular (Android):** una app como MacroDroid envía el texto de la notificación a `POST /api/captura`
  con tu token personal (ver **Captura automática** dentro de la app). En iPhone no es posible leer notificaciones de otras apps.
- **Correos de alerta:** define `IMAP_HOST`, `IMAP_USER` y `IMAP_PASSWORD` (buzón dedicado, contraseña de aplicación) y reenvía allí las
  alertas de tus bancos a `buzon+tuusuario@...`. Nequi manda un correo por cada movimiento.
- **Conciliación:** al subir el extracto, lo capturado se empareja por banco, valor y fecha (±5 días) y no se duplica.
  Con Nu además se comprueba que lo leído cuadre con "Lo que entró / Lo que salió" del resumen.

## Claves
Cada persona cambia su clave en **Yo → Mi cuenta**. El administrador restablece las de otros en **Familia**.

## Avisos push
- En **Avisos**, pulsa "Activar avisos" en cada celular. iPhone: Safari → Compartir → *Añadir a pantalla de inicio*, abrir desde ahí.
- Tarjeta: 3 días antes, 1 día antes y el día del límite (se lee del extracto de la tarjeta).
- Recordatorios mensuales de la familia, recordatorio de subir extractos, y presupuestos por categoría (80 % y 100 %).
- Passkeys: crea una en **Yo → Mi cuenta** (huella, Face ID o PIN) y entra sin clave. Funcionan en el dominio con HTTPS donde las creaste.
- Zona horaria: `TZ` (por defecto America/Bogota).

## Local
    pip install -r requirements.txt
    uvicorn main:app --reload

## Notas
- La clave del PDF (cédula) solo abre el archivo; no se guarda. Tampoco se guarda el PDF.
- Subir dos veces el mismo extracto no duplica movimientos. Se detecta solo si es cuenta o tarjeta.
- El lector es genérico más reglas de Nu (cuenta y tarjeta). Para otros bancos, edita `lector.py`.
