# Conectarse al servidor MCP Odoo remoto (transporte HTTP)

El servidor corre en un solo lugar y lo comparten varios usuarios y varias bases.
**No guarda credenciales de Odoo**: cada request las trae.

## Headers

| Header | Requerido | Descripción |
|---|---|---|
| `X-Bmya-Api-Key` | sí | La key que emite BMYA. Determina, del lado servidor, **a qué instancia y base** se conecta la sesión, y si es de lectura o de escritura. |
| `X-Odoo-Api-Key` | sí | **Tu** API key personal de Odoo. Define tus permisos y deja la auditoría a tu nombre dentro de Odoo. |
| `X-Odoo-Mode` | opcional | `readonly` para restringirte a lectura aunque tu key permita escribir. **Sólo restringe**: pedir `readwrite` con una key de lectura no hace nada. |
| `X-Gateway-Token` | opcional | Sólo si el servidor tiene `MCP_GATEWAY_TOKEN` configurado. |

> **`X-Odoo-Url` y `X-Odoo-Database` ya no se envían.** Los aporta la BMYA key.
> Si los mandás y no coinciden con tu key, el pedido se rechaza en vez de
> ignorarse en silencio, para que nunca creas que estás leyendo una base cuando
> en realidad es otra.

## Cómo obtener cada cosa

La **BMYA API key** la entrega BMYA, una por base y por modo:

```
bmya_ro_a3f19c_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX   (lectura)
bmya_rw_b7e204_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX   (escritura)
```

La **API key de Odoo** la generás vos en tu propio usuario: *Preferencias de
usuario → Seguridad de la cuenta → Nueva API key*. Es personal e intransferible.

## Config del cliente MCP

BMYA te va a mandar, junto con tu key, un bloque ya armado con las dos opciones
de abajo (lo genera `tools/bmya-keys.py new`/`snippet`, ver
[bmya-api-keys.md](bmya-api-keys.md)) — no hace falta escribirlo a mano. Esta
sección explica el porqué de cada una.

> **Ojo:** el formato clásico `"type": "http"` + `"headers"` en
> `mcpServers` **no funciona en la app Claude Desktop** — su
> `claude_desktop_config.json` sólo acepta entradas `command`/`args` (stdio) y
> rechaza en silencio cualquier otra cosa ("no son configuraciones válidas de
> servidores MCP y fueron omitidas"), verificado contra el schema real de la
> app. Sí funciona con `claude mcp add` en Claude Code, que soporta HTTP y
> headers de forma nativa.

### Opción A — Claude Code (un solo comando, recomendado)

```bash
claude mcp add --transport http odoo-clienteX https://odoo-mcp.bmya.cloud/mcp/ \
  --header "X-Bmya-Api-Key: bmya_ro_a3f19c_..." \
  --header "X-Odoo-Api-Key: TU_API_KEY_PERSONAL"
```

Nada que editar, conecta al instante. Repetir por cada base/modo que uses.

### Opción B — Claude Desktop (la app)

Como esa app no acepta headers directamente, la conexión pasa por
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote), un puente que la app sí
puede lanzar como proceso normal (`npx`) y que es quien le habla al servidor por
HTTP llevando los headers. Pegar esto en *Configuración → Developer → Edit
Config* (o directamente en `claude_desktop_config.json`) y reiniciar la app por
completo:

```json
{
  "mcpServers": {
    "odoo-clienteX": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote", "https://odoo-mcp.bmya.cloud/mcp/", "--transport", "http-only",
        "--header", "X-Bmya-Api-Key: bmya_ro_a3f19c_...",
        "--header", "X-Odoo-Api-Key: TU_API_KEY_PERSONAL"
      ]
    }
  }
}
```

La primera vez que arranca descarga `mcp-remote` vía `npx` (unos segundos);
después queda en caché. Requiere Node.js instalado.

Con una key de sólo lectura el servidor **ni siquiera lista** las herramientas de
escritura (`odoo_create`, `odoo_write`, `odoo_unlink`, `odoo_call_method`), así
que el modelo no las intenta, en cualquiera de las dos opciones.

Para ver contra qué instancia quedaste conectado, pedile al asistente que llame
`odoo_list_companies`: devuelve la URL, la base, el modo efectivo y los métodos
habilitados **de tu propia key**, y nada más.

## Prueba local

Con el contenedor levantado (`cd deploy && docker compose up -d --build`) o el
proceso directo:

```bash
BMYA_API_KEYS_FILE=./deploy/config/bmya-api-keys.json MCP_TRANSPORT=http \
  BMYA_ALLOW_INSECURE_URLS=1 python src/odoo_mcp_server.py
```

Salud y readiness:

```bash
curl -s http://localhost:8080/health   # -> {"status":"ok"}
```

```bash
curl -s http://localhost:8080/readyz   # -> {"status":"ready","stale":false}
```

`/health` es sólo liveness. `/readyz` da 503 si el registro de keys no cargó, y es
lo que usa el healthcheck del contenedor.

Smoke test del handshake MCP y una tool de lectura:

```bash
BASE=http://localhost:8080/mcp/
HDR=(-H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
     -H "X-Bmya-Api-Key: $BMYA_KEY" \
     -H "X-Odoo-Api-Key: $ODOO_KEY")

curl -s "${HDR[@]}" -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}' "$BASE"
```

```bash
curl -s "${HDR[@]}" -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"odoo_search_count","arguments":{"model":"res.partner","domain":[]}}}' "$BASE"
```

## Si algo falla

| Qué ves | Qué significa |
|---|---|
| **401** `{"error":"unauthorized"}` | Falta `X-Bmya-Api-Key`, está mal escrita, fue revocada o venció. El servidor devuelve el mismo mensaje en los cuatro casos, a propósito: no informa cuál. Pedí una key nueva. |
| **401** con la key correcta | Si hay `MCP_GATEWAY_TOKEN` configurado, también hay que mandar `X-Gateway-Token`. |
| **503** `{"error":"service_unavailable"}` | El registro de keys no está disponible en el servidor. No es tu configuración. |
| `Error: Missing X-Odoo-Api-Key header` | La BMYA key validó, pero falta *tu* API key de Odoo. |
| `Error: Read-only connection` | Tu key es de lectura, o el servidor está en modo lectura global (`ODOO_MCP_READONLY`). |
| `Error: This BMYA API key is bound to ...` | Estás mandando `X-Odoo-Url` / `X-Odoo-Database`. Quitalos. |
| `Error: Model '...' is not available` | Ese modelo está excluido para tu key. |
| `Error: Method '...' is not allowed` | Ese método de negocio no está habilitado para tu key. |
| Se queda en `Connecting to remote server...` y nunca conecta (en la app: *"Could not attach to MCP server"*) | La URL termina sin `/`. Contra un servidor viejo, el path desnudo respondía un 307 hacia la forma con barra y `mcp-remote` no sigue redirects. Usá `https://odoo-mcp.bmya.cloud/mcp/`. Desde 2026-07 el servidor sirve las dos formas directo, así que esto sólo aplica a despliegues sin actualizar. |

## Notas

- El transporte es **stateless**: los headers viajan en cada request y no hay
  sesión que expire del lado del servidor.
- Revocar un acceso lo hace BMYA editando el registro; la key deja de funcionar en
  segundos, sin reiniciar el servidor y sin que el cliente tenga que hacer nada.
- Si se te filtró una BMYA key, avisá para revocarla. Tu API key de Odoo la
  revocás vos desde Odoo.

## Ver también

- [bmya-api-keys.md](bmya-api-keys.md) — operación del registro de keys (BMYA).
- [../deploy/README.md](../deploy/README.md) — despliegue del servidor.
