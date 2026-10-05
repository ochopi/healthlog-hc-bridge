# healthlog-hc-bridge

Puente mínimo: **HC Webhook (Android) → este servicio → HealthLog**.
Traduce el JSON de Health Connect al formato de la API de ingestión de HealthLog.
No guarda datos de salud y trae un panel web de estado. Solo usa la librería estándar de Python.

## Instalar

### Opción A: Portainer (recomendada)

1. Portainer → **Stacks → Add stack → Repository**.
2. URL de tu repo y compose path `docker-compose.yml`. (Portainer construye la imagen desde el `Dockerfile`.)
3. En **Environment variables** agrega (ver `.env.example` para la lista completa):
   `HEALTHLOG_URL`, `HEALTHLOG_MEASUREMENT_TOKEN`, `BRIDGE_KEY`, `DRY_RUN=true`
4. **Deploy the stack**. Portainer mostrará el contenedor como *healthy* gracias al HEALTHCHECK.

Los tokens quedan guardados en Portainer, no en el repo.

### Opción B: terminal

```bash
cp .env.example .env     # .env está en .gitignore; nunca lo subas
nano .env
docker compose up -d --build
```

## Configurar HC Webhook (app Android)

- **Webhooks → Add**: URL `http://IP_DEL_BRIDGE:8088/healthconnect` (formato JSON)
- Custom header: `X-Bridge-Key` = el valor de `BRIDGE_KEY`
- Botón **Test** manda datos de ejemplo: con `DRY_RUN=false` entrarían a tu HealthLog.
- Fuera de casa el teléfono necesita Tailscale activo.

## Flujo recomendado

1. `DRY_RUN=true` → sincroniza → revisa el panel y `docker logs`.
2. `DRY_RUN=false` → redespliega → sincroniza de nuevo.
3. En HealthLog → Measurements, los datos llevan la etiqueta "External device".

## Panel de estado

Abre `http://IP_DEL_BRIDGE:8088/` en el navegador. Muestra: último contacto de la app, estado de HealthLog
(alcanzable, token válido, latencia), cola con barra de progreso y tiempo restante, totales, últimas
sincronizaciones por tipo de dato y avisos/errores recientes. No muestra valores de salud, solo contadores.

Para `curl` o scripts: `curl http://IP_DEL_BRIDGE:8088/status.json`. `/healthz` responde `{"ok": true}` (lo usa Docker).

**Con Nginx Proxy Manager:** Proxy Host `hc-bridge.lab.ochopi.com` → `http`, IP del bridge, puerto `8088`, cert wildcard,
Force SSL. No necesita WebSockets.

**Protección:** el panel no requiere login por defecto. Si quieres uno, define `DASH_USER` y `DASH_PASSWORD`
(Basic auth en `/` y `/status.json`; `/healthz` y el POST de la app no se ven afectados), o usa un Access List de NPM.
`POST /healthconnect` siempre se protege con `X-Bridge-Key`.

## Sincronizaciones largas (backfill)

El bridge responde `202` a la app al instante y procesa en segundo plano, así que la app no da timeout.
El avance se ve en el panel.

- Velocidad: ~4 registros/s (límite de HealthLog: 300 escrituras/min). Ajustable con `RATE_PER_SEC`.
- Si HealthLog está caído o limita, el bridge reintenta solo.
- Si reinicias el contenedor a mitad, lo pendiente se pierde: repite el mismo rango en la app (los duplicados se descartan).
- Para rangos grandes, mejor por trozos (p. ej. 6 meses a la vez).

## Mapeo

| Health Connect | HealthLog |
|---|---|
| weight | WEIGHT |
| body_fat | BODY_FAT |
| steps | ACTIVITY_STEPS (un registro por intervalo) |
| resting_heart_rate | RESTING_HEART_RATE |
| heart_rate_variability (RMSSD) | HRV_RMSSD |
| blood_pressure | BLOOD_PRESSURE_SYS + _DIA |
| oxygen_saturation | OXYGEN_SATURATION |
| respiratory_rate | RESPIRATORY_RATE |
| vo2_max | VO2_MAX |
| body_temperature | BODY_TEMPERATURE |
| sleep (stages) | SLEEP_DURATION con sleepStage (CORE/DEEP/REM/AWAKE/ASLEEP) |
| exercise | workouts (requiere HEALTHLOG_WORKOUT_TOKEN) |

`heart_rate` (muestras continuas) y otros tipos sin equivalente documentado en HealthLog se ignoran.

## Notas

- Los duplicados son seguros: HealthLog responde 409/duplicate y el bridge los cuenta como tales.
- Los tokens van solo en `.env` o en Portainer; no los pegues en chats ni en commits.
