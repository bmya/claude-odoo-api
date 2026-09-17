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

- Un router por `Host(mcp.bmya.cloud)` (o el dominio que uses) hacia el
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
      rule: "Host(`mcp.bmya.cloud`)"
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

## Consola de administración

Reemplaza el `ssh barbol && python tools/bmya-keys.py new --write` como forma de
emitir una key: con formulario, con la key mostrada una sola vez junto al bloque
listo para mandarle al cliente, y con registro de **quién** la emitió.

Es un **segundo contenedor**, `bmya-console`, en el mismo host. Comparte con
`odoo-api` únicamente el directorio del registro.

```
operador --ssh -L--> 10.0.0.14:8081  (bmya-console)   escribe -> config/  :rw
                     10.0.0.14:8080  (odoo-mcp-server) lee    -> config/  :ro
```

### Puesta en marcha

Una clave por persona (no una compartida: es lo que llena `created_by`):

```bash
python tools/bmya-console-operator.py daniel@bmya.cl
```

Imprime la clave una sola vez y la línea para `BMYA_CONSOLE_OPERATORS` en
`deploy/.env`, junto con `BMYA_CONSOLE_SESSION_SECRET`. Para agregar a alguien
más, pasale el valor actual con `--existing` y te devuelve la lista completa.

**`BMYA_CONSOLE_OPERATORS` vacío significa que no entra nadie**, nunca "entra
cualquiera", y `/readyz` devuelve 503 diciéndolo.

### Cómo se entra

La consola publica **sólo en la IP de VLAN**, igual que el servidor MCP. Se
entra por un túnel SSH sobre Twingate:

```bash
ssh -L 8081:10.0.0.14:8081 barbol
```

y se navega a `http://localhost:8081`. La cookie de sesión queda en el loopback
del operador y no cruza la VLAN en claro — que es mejor que exponer el puerto al
navegador de la VLAN, no un rodeo.

### El montaje `:rw`, y por qué `read_only: true` sigue en pie

`read_only: true` hace read-only el **filesystem raíz del contenedor**. Los bind
mounts se montan aparte y respetan su propio flag. La consola conserva
`cap_drop: ALL` y `no-new-privileges`, y su única ruta escribible es
`/app/config`.

Sigue siendo **el directorio, nunca el archivo**, y ahora con más razón: la
consola reescribe el inodo en cada emisión.

Corre como **uid 1000, igual que el servidor**, y eso es funcional, no estético:
`bmya_registry._preserve_ownership` saltea el `chown` cuando el dueño ya
coincide, así que con uid 1000 contra un archivo `1000:1000` nunca hace falta
`CAP_CHOWN`. Corriendo como root adentro habría que devolverle esa capability
—perdiendo la postura— o dejaría archivos que el servidor no puede leer, que es
justo la trampa del `stale` silencioso.

### Un segundo escritor: el candado

Desde que existe la consola hay **dos** procesos que escriben el registro (ella
y el CLI por SSH). Todo el ciclo leer→modificar→escribir corre bajo un `flock`
sobre `config/bmya-api-keys.json.lock`. Sin él, una emisión web concurrente con
un `revoke` por consola deja una sola de las dos operaciones, sin error de
ningún lado. El CLI también lo toma: si lo tomara sólo la consola, sería
decorativo.

### Propagación

La consola **no le señaliza** al contenedor del servidor. El servidor ya
reparsea solo cuando cambia la huella `(mtime, size)`, dentro de
`BMYA_REGISTRY_TTL_SECONDS`. Las alternativas eran montar el socket de Docker
(root en el host, entregado al servicio más expuesto), compartir namespace de
PID, o abrirle un endpoint de administración al servicio cuya premisa es no
tener ninguno — todo eso para ahorrar 10 segundos.

En cambio la consola muestra un badge leyendo `/readyz` del servidor. Si dice
`stale`, la escritura salió bien pero el servidor no pudo releer el archivo:
sigue con su última copia buena, la key nueva devuelve 401 y la revocación no
aplica. Casi siempre es el dueño del archivo.

Para forzar la recarga a mano (revocación urgente):

```bash
docker compose kill -s HUP odoo-api
```

### Bitácora de uso

El servidor escribe una línea JSON por llamada a tool en el volumen
`bmya-usage` (`BMYA_USAGE_DIR`), y la consola la lee `:ro` y la resume en
`/usage`. No cobra nada: existe porque es el único dato que no se puede
reconstruir después, y sin él habría que ponerle precio a una llamada a ciegas.
Escribirla nunca puede hacer fallar una llamada.

### Exponerla por Traefik, más adelante

Sin cambios de código: una ruta en la configuración dinámica de Traefik hacia
`http://10.0.0.14:8081`, y `BMYA_CONSOLE_COOKIE_SECURE=1`. Ojo con el orden —
ponerlo en 1 sin TLS delante rompe el login (la cookie Secure no viaja por HTTP
plano); dejarlo en 0 detrás de TLS sólo degrada, no rompe.

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
