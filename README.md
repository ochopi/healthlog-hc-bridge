# healthlog-hc-bridge

Puente mínimo: **HC Webhook (Android) → este servicio → HealthLog**.
Traduce el JSON de Health Connect al formato de la API de ingestión de HealthLog. No guarda datos.

## Instalar (LXC 104, junto a HealthLog)

```bash
mkdir -p /opt/healthlog-hc-bridge && cd /opt/healthlog-hc-bridge
# copiar aquí: bridge.py, Dockerfile, docker-compose.yml, .env.example
cp .env.example .env
nano .env          # HEALTHLOG_URL, token, BRIDGE_KEY (deja DRY_RUN=true al inicio)
docker compose up -d --build
docker logs -f healthlog-hc-bridge
```

Prueba: `curl http://192.168.1.211:8088/` → `{"ok": true, ...}`

## Configurar HC Webhook (app Android)

- **Webhooks → Add**: URL `http://192.168.1.211:8088/healthconnect` (formato JSON)
- Custom header: `X-Bridge-Key` = el valor de `BRIDGE_KEY` de tu `.env`
- Botón **Test**, luego sincroniza manualmente.
- Fuera de casa el teléfono debe tener Tailscale activo para alcanzar esa IP.

## Flujo recomendado

1. `DRY_RUN=true` → sincroniza → revisa en `docker logs` qué enviaría.
2. `DRY_RUN=false` → `docker compose up -d` → sincroniza de nuevo.
3. Revisa en HealthLog → Measurements (badge "External device").

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

`heart_rate` (muestras continuas) se ignora a propósito: HealthLog no documenta un tipo para eso.

## Notas

- Los duplicados son seguros: HealthLog responde 409/duplicate y el bridge los cuenta como tales.
- Si HealthLog está caído el bridge responde 502 y la app reintenta en la siguiente sincronización.
- Los tokens van solo en `.env` (no los pegues en chats ni en repos).
