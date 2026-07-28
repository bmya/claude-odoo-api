# Despliegue en bmya.cloud

Servidor MCP de Odoo en modo HTTP remoto multi-tenant, detrás del Traefik que
corre en **otro host** y le pega a este por la VLAN.

```
cliente (Claude) --https--> Traefik (otro host) --http--> 10.0.0.14:8080 (este host)
                  TLS                            VLAN
```

El servidor no guarda credenciales de Odoo. Cada request trae:

| Header | Qué es |
|---|---|
| `X-Bmya-Api-Key` | La key que emite BMYA. Resuelve, del lado servidor, la URL y la base de Odoo, y si la conexión es de lectura o de escritura. |
| `X-Odoo-Api-Key` | La API key personal del propio usuario en Odoo. Los permisos y la auditoría dentro de Odoo siguen siendo suyos. |
| `X-Odoo-Mode` | Opcional. Sólo sirve para **restringirse** (`readonly`); nunca para ampliar. |
| `X-Gateway-Token` | Opcional, sólo si se configuró `MCP_GATEWAY_TOKEN`. |

## Puesta en marcha

```bash
cd deploy
cp .env.example .env      # editar: MCP_BIND_ADDR, TRAEFIK_HOST_IP, MCP_IMAGE
```

Crear el registro de keys y emitir la primera:

```bash
cd deploy && mkdir -p config && echo '{"version":1,"grants":[]}' > config/bmya-api-keys.json
```

```bash
cd deploy && python ../tools/bmya-keys.py new --file config/bmya-api-keys.json --write \
  --database clientex_prod --url https://clientex.bmya.cloud \
  --mode readonly --label "ClienteX lectura"
```

La key en claro se imprime **una sola vez**: el archivo guarda sólo su sha256.
Si se pierde, se revoca y se emite otra.

> **El montaje es el directorio `config/`, no el archivo.** No es un detalle
> cosmético: un bind mount de un archivo suelto fija su inode, y el CLI reescribe
> el registro de forma atómica (tempfile + rename). Con un montaje de archivo el
> contenedor se queda leyendo el inode viejo ya desvinculado **para siempre**, así
> que una revocación nunca toma efecto. Está verificado: montando el archivo, la
> key revocada seguía respondiendo 200; montando el directorio, pasa a 401.

El bind mount conserva los uid del host y el contenedor corre como uid 1000:

```bash
sudo chown -R 1000:1000 deploy/config && sudo chmod 600 deploy/config/bmya-api-keys.json
```

Sólo hace falta una vez: los `--write` siguientes heredan ese owner. Si el CLI no
puede preservarlo, avisa por stderr — no lo ignores, es lo que deja al servidor
`stale` y hace que una key recién emitida devuelva 401. Ver
[../docs/bmya-api-keys.md](../docs/bmya-api-keys.md).

Levantar, en cualquiera de las dos formas:

```bash
cd deploy && docker compose up -d --build
```

```bash
cd deploy && docker compose pull && docker compose up -d
```

Verificar:

```bash
curl -s http://10.0.0.14:8080/readyz
```

`/readyz` devuelve 200 sólo si el registro de keys cargó; es lo que usa el
healthcheck del contenedor, así que un montaje mal hecho rompe el deploy en voz
alta en vez de devolver 401 a todos los clientes en silencio. `/health` es sólo
liveness. Los dos están exentos de autenticación.

## Configuración del lado Traefik

Traefik está en otro host, así que su configuración **no** vive en este repo (no
hay labels de Docker ni red compartida). Lo que hay que definir allá:

- Un router por `Host(odoo-mcp.bmya.cloud)` (o el dominio que uses) hacia el
  service `http://10.0.0.14:8080`.
- **Nada que buferee la respuesta**, y `readTimeout` / `idleTimeout` holgados en
  el entrypoint: el transporte es Streamable HTTP con SSE, y un proxy que
  buferea rompe el streaming.
- `X-Forwarded-Proto` propagado. Del lado del servidor, uvicorn corre con
  `proxy_headers=True` y sólo confía en `TRAEFIK_HOST_IP`.
- El rate limiting va acá (middleware `ratelimit`, ~60/min con burst 20 como
  punto de partida) más HSTS. Por eso el servidor no trae su propio limitador.

Ejemplo de router en configuración dinámica de Traefik:

```yaml
http:
  routers:
    odoo-mcp:
      rule: "Host(`odoo-mcp.bmya.cloud`)"
      entryPoints: [websecure]
      service: odoo-mcp
      middlewares: [odoo-mcp-ratelimit]
      tls:
        certResolver: le
  services:
    odoo-mcp:
      loadBalancer:
        servers:
          - url: "http://10.0.0.14:8080"
  middlewares:
    odoo-mcp-ratelimit:
      rateLimit:
        average: 60
        burst: 20
```

## TLS

El contenedor sirve HTTP plano porque el salto Traefik → backend va por la VLAN.
**Si ese salto alguna vez deja de ser red privada hay que cifrarlo**: los headers
llevan la BMYA key y la API key de Odoo del usuario, y en HTTP plano viajan
legibles para cualquiera en el camino. Para eso, setear `MCP_TLS_CERTFILE` y
`MCP_TLS_KEYFILE` (el contenedor termina TLS él mismo) y apuntar el service de
Traefik a `https://`.

## Operación

Listar y revocar keys (la emisión con `--write` se hace en el host, porque el
montaje es `:ro`):

```bash
docker compose exec odoo-api python tools/bmya-keys.py list --file /app/config/bmya-api-keys.json
```

```bash
cd deploy && python ../tools/bmya-keys.py revoke <key_id> --file config/bmya-api-keys.json --write
```

La revocación toma efecto en `BMYA_REGISTRY_TTL_SECONDS` sin reiniciar nada. Para
que sea inmediata:

```bash
docker compose kill -s HUP odoo-api
```

### Cuidado con el estado `stale`

Si el archivo queda inválido o desaparece **estando el servidor arriba**, se
conserva la última copia buena y se registra un ERROR, en lugar de dejar a todos
los clientes afuera. `/readyz` sigue devolviendo 200 pero con `"stale": true`.

Esa combinación significa que **las revocaciones dejaron de aplicarse**, así que
conviene monitorear ese flag:

```bash
curl -s http://10.0.0.14:8080/readyz | grep -q '"stale":false' || echo "registro desactualizado"
```

Si en cambio el registro falta **desde el arranque**, no hay copia buena: `/readyz`
da 503, cada request da 503 y el healthcheck marca el contenedor `unhealthy`.

## Rollback

`BMYA_AUTH_ENABLED=0` restaura el comportamiento anterior (el cliente manda
`X-Odoo-Url` / `X-Odoo-Database`) sin cambiar la imagen. Sirve para desplegar
primero y migrar las configuraciones de los clientes de a una.

## Ver también

- [../docs/bmya-api-keys.md](../docs/bmya-api-keys.md) — esquema del registro y
  operación de las keys.
- [../docs/remote-client-config.md](../docs/remote-client-config.md) — cómo se
  configura un cliente.
- [../docs/client-onboarding.md](../docs/client-onboarding.md) — qué se le
  entrega a un cliente.
