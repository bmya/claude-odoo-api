# Conectarse al servidor MCP Odoo remoto (transporte HTTP)

En modo HTTP el servidor **no guarda credenciales**. Cada usuario envía su propia
instancia de Odoo (URL, base de datos, API key) como **headers HTTP** desde su
configuración MCP local. Así cada quien queda sujeto a sus permisos de Odoo y
puede apuntar a varias bases (incluidas las de clientes) agregando una entrada
por instancia.

## Headers

| Header            | Requerido | Descripción                                              |
|-------------------|-----------|----------------------------------------------------------|
| `X-Odoo-Url`      | sí        | URL del Odoo (p. ej. `https://clienteX.bmya.cloud`)      |
| `X-Odoo-Database` | sí        | Nombre de la base de datos                               |
| `X-Odoo-Api-Key`  | sí        | **Tu** API key personal de Odoo (define tus permisos)    |
| `X-Gateway-Token` | opcional  | Solo si el servidor tiene `MCP_GATEWAY_TOKEN` configurado |

> La API key de Odoo se genera en Odoo: *Preferencias de usuario → Seguridad de
> la cuenta → Nueva API key*. Es personal e intransferible.

## Config del cliente MCP

Una entrada por cada base/cliente que uses. Ejemplo (Claude Desktop / Claude Code):

```json
{
  "mcpServers": {
    "odoo-clienteX": {
      "type": "http",
      "url": "https://odoo-mcp.bmya.cl/mcp",
      "headers": {
        "X-Odoo-Url": "https://clienteX.bmya.cloud",
        "X-Odoo-Database": "clienteX_db",
        "X-Odoo-Api-Key": "TU_API_KEY_PERSONAL",
        "X-Gateway-Token": "SOLO_SI_ESTA_CONFIGURADO"
      }
    },
    "odoo-otrocliente": {
      "type": "http",
      "url": "https://odoo-mcp.bmya.cl/mcp",
      "headers": {
        "X-Odoo-Url": "https://otro.bmya.cloud",
        "X-Odoo-Database": "otro_db",
        "X-Odoo-Api-Key": "TU_API_KEY_PERSONAL"
      }
    }
  }
}
```

## Prueba local (Fase 1)

Con el contenedor levantado en local (`cd deploy && docker compose up -d --build`
o `MCP_TRANSPORT=http python src/odoo_mcp_server.py`):

- HTTP:  `http://localhost:8080/mcp`
- HTTPS (si seteás `MCP_TLS_CERTFILE`/`MCP_TLS_KEYFILE`): `https://localhost:8443/mcp`

Verificación rápida del endpoint de salud:

```bash
curl -s http://localhost:8080/health   # -> {"status":"ok"}
```

Smoke test del handshake MCP + una tool de lectura (sin cliente MCP):

```bash
BASE=http://localhost:8080/mcp
HDR=(-H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
     -H "X-Odoo-Url: http://host.docker.internal:8069" \
     -H "X-Odoo-Database: TU_DB" \
     -H "X-Odoo-Api-Key: TU_API_KEY")

# initialize
curl -s "${HDR[@]}" -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}' "$BASE"

# tools/call -> search_count
curl -s "${HDR[@]}" -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"odoo_search_count","arguments":{"model":"res.partner","domain":[]}}}' "$BASE"
```

En modo stateless cada request es independiente; los headers de credenciales
viajan en cada llamada.
