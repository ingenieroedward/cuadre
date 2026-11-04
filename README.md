# Cuadre — finanzas de la familia

App web (FastAPI + SQLite) para subir extractos PDF de varios bancos, ver resumen por mes/persona/categoría
y agregar ingresos/gastos manuales. Login por miembro.

## Despliegue en Dokploy
1. Sube esta carpeta (`app/`) a un repositorio Git (privado).
2. En Dokploy: Create Service > **Compose** > conecta el repo, ruta `docker-compose.yml`.
3. Variables de entorno: `SECRET_KEY` (cadena larga aleatoria), `ADMIN_USER`, `ADMIN_PASSWORD`.
4. Pestaña Domains: tu dominio, puerto `8000`, HTTPS (Let's Encrypt) activado.
5. Los datos viven en el volumen `cuadre_data` (/data/finanzas.db). Haz respaldo de ese volumen.

## Local
    pip install -r requirements.txt
    ADMIN_PASSWORD=algo-largo uvicorn main:app --reload

## Notas
- La clave del PDF (cédula) se usa solo para abrirlo y no se guarda; el PDF no se guarda, solo los movimientos.
- Subir dos veces el mismo extracto no duplica movimientos.
- El lector es genérico (líneas con fecha + valor). Para ajustar un banco, edita `lector.py`.
- Cambiar la categoría de un movimiento con "todos" marcado crea una regla para los parecidos.

## Variable OWN_NAMES
Nombres (en minúscula, separados por coma) con los que apareces en tus propias transferencias, por ejemplo
`OWN_NAMES=edward david diaz padilla,edwsystem`. Esos movimientos se marcan como "Traslado propio" y no
cuentan como ingreso ni gasto. "Pagaste tu tarjeta" tampoco cuenta (la compra se cuenta al leer la tarjeta).
