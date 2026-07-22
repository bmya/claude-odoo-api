# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an MCP (Model Context Protocol) server that provides tools to interact with Odoo 19's External JSON-2 API. The server supports multiple company/instance configurations and exposes 12 tools on Odoo databases: list_companies, search_read, create, write, unlink, search, read, search_count, list_models, fields_get, name_search, and call_method.

The introspection tools (`list_models`, `fields_get`) and `name_search` are implemented on top of the proven `search_read` endpoint over the meta-models `ir.model` / `ir.model.fields` — they do NOT use new endpoints, since `/json/2/{model}/call` returns 404 on these instances. Note: `name_search` filters on the `name` field (not `display_name`, which is a non-searchable computed field on this instance and is silently ignored by ilike).

Write operations can be globally disabled with `ODOO_MCP_READONLY=1` (also `true`/`yes`) — a kill-switch to protect production without redeploying. This also blocks `call_method`, since business-method actions mutate data.

The `call_method` tool runs arbitrary Odoo business methods (e.g. `account.move.action_post`), so it is gated by an allowlist. The allowlist is configured with `ODOO_MCP_ALLOWED_METHODS` (comma-separated `model.method` pairs); its defaults are `calendar.event.action_sync_timesheets`, `account.move.action_post`, and `sale.order.action_confirm`. Calls to methods outside the allowlist are rejected with an error explaining how to enable them.

## 🔧 CRITICAL FIX HISTORY - 2025-12-23

### Problem Discovered
El MCP server tenía un bug crítico de permisos en el Dockerfile desde el commit inicial (33c7f16):

**Síntoma:**
```
ModuleNotFoundError: No module named 'requests'
```

**Causa raíz:**
- Las dependencias Python se instalaban con `pip install --user` en el builder stage → `/root/.local`
- Se copiaban a `/root/.local` en el stage final
- Se creaba usuario `odoo` y se cambiaba con `USER odoo`
- El PATH apuntaba a `/root/.local/bin` pero el proceso corría como usuario `odoo`
- Usuario `odoo` NO tiene permisos para leer `/root/.local`

**Líneas problemáticas en Dockerfile (original):**
```dockerfile
COPY --from=builder /root/.local /root/.local  # ❌ Copiado a /root
COPY src/ ./src/
RUN useradd -m -u 1000 odoo && chown -R odoo:odoo /app
USER odoo                                        # ❌ Usuario sin acceso a /root
ENV PATH=/root/.local/bin:$PATH                 # ❌ PATH inaccesible
```

### Solution Implemented (commit 26c7ba6)

**Cambios en Dockerfile:**
1. Crear usuario `odoo` **ANTES** de copiar archivos
2. Copiar dependencias a `/home/odoo/.local` con ownership correcto
3. Actualizar PATH para apuntar al directorio del usuario odoo

**Código corregido:**
```dockerfile
# Create non-root user for security
RUN useradd -m -u 1000 odoo && chown -R odoo:odoo /app

# Copy Python dependencies from builder to odoo user home
COPY --from=builder --chown=odoo:odoo /root/.local /home/odoo/.local

# Copy the MCP server source
COPY --chown=odoo:odoo src/ ./src/

# Switch to non-root user
USER odoo

# Set environment variables (can be overridden at runtime)
ENV PATH=/home/odoo/.local/bin:$PATH
```

### Verification Test Results

**Test 1 - Módulos Python accesibles:**
```bash
docker run --rm bmya/odoo-mcp-server:latest python -c "import requests; import mcp; print('✅ Módulos cargados correctamente')"
# Resultado: ✅ Módulos cargados correctamente
```

**Test 2 - Carga de configuración multi-company:**
```bash
docker run --rm -v /Users/danielb/ClaudeCodeProjects/claude-odoo-api/.env:/app/.env:ro bmya/odoo-mcp-server:latest python -c "..."
# Resultado: ✅ Loaded 2 companies: ['bmya', 'companycl']
```

### Docker Image Status

**Imagen actual:**
- Repository: `bmya/odoo-mcp-server:latest`
- Image ID: `b18c9297c061` (nuevo, reconstruido 2025-12-23 22:45)
- Size: 323MB
- Status: ✅ Funcionando correctamente

**Estado en Docker MCP Toolkit:**

`odoo-api` lo lanza la app de Claude vía `docker run -i ... bmya/odoo-mcp-server:latest`
(servidor MCP local por stdio), no por el gateway `docker mcp`. Por eso no aparece en
`docker mcp profile server ls`. Para ver/gestionar su estado, usá el panel de
"Servidores MCP locales" de la app de Claude.

### Git Status

**Commit realizado:**
- Hash: `26c7ba6`
- Mensaje: `[FIX] Dockerfile: Corregir permisos de usuario odoo`
- Branch: `main`
- Estado: ✅ Commiteado localmente
- Pendiente: `git push origin main` (no ejecutado aún)

**Archivos modificados pero no commiteados:**
- `.claude/settings.local.json` (cambios de sesión)
- `CLAUDE.md` (este archivo - documentación actualizada)
- `add_mcp_server.md` (untracked)
- `.gemini-clipboard/` (untracked)

## 🩺 TROUBLESHOOTING - "Server disconnected" tras reiniciar/actualizar Docker

**Síntoma:** En "Servidores MCP locales" de la app de Claude, `odoo-api` (y normalmente
también `MCP_DOCKER`) aparecen como **failed / Server disconnected**.

**Causa:** No es un bug de la imagen ni de `claude_desktop_config.json`. Cuando el daemon
de Docker Desktop se reinicia (p. ej. al actualizar Docker), todo proceso `docker run -i`
adjunto por stdio queda cortado. En los logs (`~/Library/Logs/Claude/mcp-server-odoo-api.log`)
se ve `error waiting for container: unexpected EOF` y la desconexión simultánea de todos
los servidores MCP basados en Docker. La app no relanza el contenedor automáticamente, así
que queda pegado en "failed".

**Remediación:** Con Docker Desktop totalmente arrancado (`docker info` responde),
deshabilitar y volver a habilitar `odoo-api` en la app (o reiniciar la app).

**Verificar la imagen aparte (debe responder limpio en stdout):**
```bash
docker run --rm bmya/odoo-mcp-server:latest python -c "import requests, mcp; print('OK')"
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
 | docker run --rm -i -v /Users/danielb/ClaudeCodeProjects/claude-odoo-api/.env:/app/.env:ro \
   -e ODOO_CONFIG_FILE=/app/.env bmya/odoo-mcp-server:latest 2>/dev/null
```

**Nota:** Esta caída es inherente a cualquier MCP stdio lanzado vía `docker run -i`; un
reinicio del daemon siempre lo corta. Ningún cambio de config lo evita.

## Architecture

### Core Components

**OdooClient Class** (`src/odoo_mcp_server.py:27-100`)
- Manages HTTP session with persistent headers for authentication
- Encapsulates all Odoo API interactions via `/json/2/{model}/{method}` endpoint pattern
- All methods use POST requests with JSON payloads
- Authentication via Bearer token (`Authorization: Bearer {api_key}`)
- Database selection via custom header (`X-Odoo-Database: {database}`)

**MCP Server** (`src/odoo_mcp_server.py`)
- Built on `mcp.server.Server` using stdio transport
- Tools are defined in `list_tools()` with JSON Schema validation
- Tool execution handled in `call_tool()` dispatcher
- Supports multiple company configurations via `company_configs` dict
- `odoo_clients` dict stores one client instance per company (lazy-initialized)

### Odoo API Pattern

All Odoo API calls follow this structure:
```
POST {ODOO_URL}/json/2/{model_name}/{method_name}
Headers:
  - Authorization: Bearer {api_key}
  - X-Odoo-Database: {database}
  - Content-Type: application/json
Body: JSON payload specific to the method
```

See `create_odoo_invoices.py` for working examples of search_read and create operations.

### Configuration

The server uses a `.env` file with INI-style sections for multi-company support:

```ini
[company1]
ODOO_URL=http://localhost:8069
ODOO_DATABASE=database_name
ODOO_API_KEY=api_key_here
COMPANY_ID=1  # Optional, defaults to 1

[company2]
ODOO_URL=http://localhost:8069
ODOO_DATABASE=another_db
ODOO_API_KEY=another_key
COMPANY_ID=2
```

Configuration loading:
- `load_company_configs()` reads all sections from `.env` using `configparser`
- `get_odoo_client(company)` creates/returns cached client for specific company
- `list_available_companies()` returns list of configured company names
- Environment variable `ODOO_CONFIG_FILE` can override default `.env` path

### Transports: stdio (local) vs HTTP (remote, multi-tenant)

The server supports two transports, selected by `MCP_TRANSPORT` (default `stdio`):

- **stdio** (default): one process per user, launched by the Claude app via `docker run -i`. Credentials come from the mounted multi-company `.env` (see above) and the tool `company` argument selects a section. Unchanged legacy behavior.
- **http** (`MCP_TRANSPORT=http`): Streamable HTTP served by uvicorn/Starlette (`run_http()` in `src/odoo_mcp_server.py`) at `MCP_HTTP_PATH` (default `/mcp`), for a shared/remote deployment. The server stores **no** Odoo credentials — each user sends their own instance as connection headers `X-Odoo-Url` / `X-Odoo-Database` / `X-Odoo-Api-Key`, so every user is bound to their own Odoo permissions and can target multiple databases (one MCP client entry per instance). See [docs/remote-client-config.md](docs/remote-client-config.md).

Key HTTP-mode internals (all in `src/odoo_mcp_server.py`):
- `resolve_odoo_client(arguments)` reads the `X-Odoo-*` headers from `app.request_context.request.headers`; falls back to `.env`/`company` when there are no headers (stdio). The `company` tool argument is optional and ignored in HTTP mode.
- Clients are cached by a **SHA-256 hash of `(url|database|api_key)`** (not by company name) via `_get_or_create_client()`, so tenants never share a cached client.
- Runs **stateless** (`StreamableHTTPSessionManager(stateless=True)`): each request is independent; credential headers travel on every request.
- `GET /health` returns `{"status":"ok"}` (real container healthcheck).
- **TLS**: serves plain HTTP behind a TLS-terminating reverse proxy (Traefik) with `proxy_headers=True` (honors `X-Forwarded-Proto`). Set `MCP_TLS_CERTFILE`/`MCP_TLS_KEYFILE` to serve HTTPS directly (e.g. self-signed for local tests).
- **Gateway token** (optional, reconfigurable front door): if `MCP_GATEWAY_TOKEN` is set, requests must carry a matching `X-Gateway-Token` header (`/health` exempt); otherwise access relies on the network gate (Twingate/VLAN).
- The `ODOO_MCP_READONLY` and `ODOO_MCP_ALLOWED_METHODS` guardrails are server-level and apply to every user/connection.

HTTP server-level env vars are documented in `.env.http.example` (no Odoo credentials there). The `.env` INI is only for stdio mode.

## Development Commands

### Local Development

```bash
# Setup
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Run the MCP server
python src/odoo_mcp_server.py

# Set environment variables
export ODOO_URL=http://localhost:8069
export ODOO_DATABASE=your_database
export ODOO_API_KEY=your_api_key
```

### Docker

The compose stack lives in `deploy/` and MUST be run from there. It is not at the
repo root on purpose: Docker Compose auto-loads a root `.env` for interpolation,
and this repo's root `.env` is the stdio credential INI (values are not Compose
syntax), which triggers spurious "variable is not set" warnings. Running from
`deploy/` (no local `.env`) avoids reading the secret.

```bash
# Build and run the HTTP server (from deploy/)
cd deploy && docker compose up -d --build

# View logs
cd deploy && docker compose logs -f

# Stop
cd deploy && docker compose down

# Legacy stdio transport via compose (normally the Claude app runs this itself)
cd deploy && docker compose --profile stdio up odoo-api-stdio

# Build image only (from repo root)
docker build -t odoo-mcp-server .

# Run with custom env vars
docker run -i --rm \
  -e ODOO_URL=http://localhost:8069 \
  -e ODOO_DATABASE=your_db \
  -e ODOO_API_KEY=your_key \
  odoo-mcp-server
```

### Testing API Connection

```bash
# Test Odoo accessibility
curl http://localhost:8069/web/database/selector

# Test API authentication
curl -X POST http://localhost:8069/json/2/res.partner/search_count \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "X-Odoo-Database: YOUR_DATABASE" \
  -H "Content-Type: application/json" \
  -d '{"domain": []}'
```

## Adding New MCP Tools

To add a new Odoo operation:

1. **Add method to `OdooClient` class** (if needed)
   - Follow pattern: build payload dict, call `_make_request(model, method, payload)`
   - Return the JSON response directly

2. **Add tool definition in `list_tools()`**
   - Define tool name (prefix with `odoo_`)
   - Provide clear description
   - Define inputSchema with JSON Schema format
   - **IMPORTANT:** Add `company` parameter as required for all Odoo operations
   - Mark other required parameters

3. **Add handler in `call_tool()`**
   - Extract `company` argument and get client: `client = get_odoo_client(company)`
   - Extract other arguments
   - Call corresponding `OdooClient` method
   - Return `TextContent` with JSON result or success message
   - Handle errors with descriptive messages

Example tool definition:
```python
Tool(
    name="odoo_my_operation",
    description="Description of operation",
    inputSchema={
        "type": "object",
        "properties": {
            "company": {
                "type": "string",
                "description": "The company configuration name"
            },
            # ... other parameters
        },
        "required": ["company", ...]
    }
)
```

## MCP Catalog Generation

To distribute the server via MCP catalogs (e.g., for the Docker Desktop MCP Toolkit), the `bmya-mcp-catalog.yaml` source file must be converted to `catalog.json`.

This process requires the `PyYAML` dependency, which has been added to `requirements.txt`.

The command to perform the conversion is:
```bash
python -c "import sys, yaml, json; json.dump(yaml.safe_load(open('bmya-mcp-catalog.yaml')), sys.stdout, indent=2)" > catalog.json
```

## Odoo Domain Filter Syntax

Domains are search criteria expressed as lists:
- Simple condition: `[["field", "operator", value]]`
- AND (implicit): `[["field1", "=", "A"], ["field2", "=", "B"]]`
- OR: `["|", ["field1", "=", "A"], ["field2", "=", "B"]]`
- NOT: `["!", ["field", "=", value]]`
- Nested: `["|", ["state", "=", "draft"], "&", ["amount", ">", 1000], ["partner_id", "!=", False]]`

Common operators: `=`, `!=`, `>`, `<`, `>=`, `<=`, `in`, `not in`, `ilike` (case-insensitive), `like`, `=ilike`, `=like`

## Key Odoo Models

Reference these common models when building queries:
- `res.partner` - Contacts/companies
- `account.move` - Invoices/bills
- `sale.order` - Sales orders
- `product.product` - Products
- `stock.picking` - Inventory transfers
- `project.task` - Tasks

## Important Notes

- External API requires Odoo Custom pricing plan (not available on Free/Standard)
- All API calls run in their own SQL transaction (auto-commit on success, rollback on error)
- When running in Docker with local Odoo, use `host.docker.internal` instead of `localhost` or `--network host`
- The MCP server runs in stdio mode - it reads from stdin and writes to stdout following MCP protocol

## Docker MCP Registry Submission

### Status
**PR #814 submitted to docker/mcp-registry on 2025-12-02**
- PR URL: https://github.com/docker/mcp-registry/pull/814
- Status: Pending review by Docker team

### Submission Process Completed

1. **Verified Dockerfile**: Confirmed existence of Dockerfile in repository (bmya/claude-odoo-api)

2. **Forked docker/mcp-registry**: Created fork at Danisan/mcp-registry

3. **Created Server Entry**: Added `servers/odoo-api/server.yaml` with:
   - Server name: `odoo-api`
   - Docker image: `bmya/odoo-mcp-server`
   - Category: business
   - Tags: odoo, erp, business, crm, secrets
   - Icon: https://www.google.com/s2/favicons?domain=odoo.com&sz=64
   - Source project: https://github.com/bmya/claude-odoo-api
   - Source commit: 5f93afd973bcda0386140c465e5fac8728f156b6

4. **Created tools.json**: Added comprehensive tool definitions to avoid build failures:
   - 8 tools documented: list_companies, search_read, create, write, unlink, search, read, search_count
   - Full argument specifications for each tool
   - Required because server needs configuration before it can list tools

5. **Submitted Pull Request**: PR #814 to docker/mcp-registry
   - Includes full feature description
   - Documents multi-company support
   - Notes MIT license compliance
   - Provides comprehensive tool list

### Configuration in Registry

The server entry requires three secrets for configuration:
```yaml
config:
  secrets:
    - name: odoo.url
      env: ODOO_URL
      example: http://localhost:8069
    - name: odoo.database
      env: ODOO_DATABASE
      example: your_database
    - name: odoo.api_key
      env: ODOO_API_KEY
      example: your_api_key_here
```

### Next Steps
1. Monitor PR #814 for CI validation results
2. Respond to any feedback from Docker team review
3. Provide test credentials if requested via https://forms.gle/6Lw3nsvu2d6nFg8e6
4. Once merged, server will be available in official Docker MCP Registry

### Files in Registry Submission
- `servers/odoo-api/server.yaml` - Server configuration and metadata
- `servers/odoo-api/tools.json` - Tool definitions for build process

---

## MCP Server Management

### Docker MCP Toolkit (Official)

El servidor `odoo-api` corre como servidor MCP local de la app de Claude (la app lanza
`docker run -i ... bmya/odoo-mcp-server:latest` por stdio). No se gestiona por el gateway
`docker mcp`, así que habilitar/deshabilitar se hace desde el panel de "Servidores MCP
locales" de la app. Para que un rebuild de la imagen tome efecto, deshabilitá/volvé a
habilitar `odoo-api` en la app (o reiniciá la app): el próximo `docker run` usa la imagen
nueva.

> **Nota:** los comandos `docker mcp server enable/disable/list` quedaron obsoletos en
> versiones recientes del toolkit (`docker mcp server` ahora solo expone `init`). La
> gestión por gateway pasó a `docker mcp profile server add/remove`.

**Comandos del toolkit aún vigentes (gateway):**
```bash
# Servidores registrados en el gateway por profile
docker mcp profile server ls

# Catálogos
docker mcp catalog list
docker mcp catalog show bmya-mcp-catalog
```

**Catálogo Custom: bmya-mcp-catalog**
- Definido en: `bmya-mcp-catalog.yaml` y `catalog.json`
- Importado en Docker MCP Toolkit
- Contiene el servidor `odoo-api` con configuración Docker

**Estado actual del servidor:**
- ✅ Imagen Docker: `bmya/odoo-mcp-server:latest`
- ✅ Montaje de volumen: `.env` file para configuración multi-company
- ✅ Lanzado por la app de Claude como servidor MCP local (stdio)

### MCP Manager (~/mcp-manager)

**Propósito:**
- Wrapper UI interactivo sobre comandos `docker mcp`
- Permite gestionar servidores MCP por proyecto usando archivos `.mcp-config.json`
- Reduce consumo de tokens al deshabilitar servidores no necesarios por proyecto

**NO es necesario para:**
- Activar/desactivar servidores locales de la app (se hace desde el panel de "Servidores MCP locales" de la app)
- Conectar clientes (Claude/Cursor) al gateway (se hace con `docker mcp client connect`)

**ES útil para:**
- Gestión granular por proyecto de qué servidores están activos
- UI amigable con checkboxes para selección múltiple
- Visualización del estado actual de servidores

**Ubicación:** `/Users/danielb/mcp-manager/`
**Archivos principales:**
- `mcp-manager.py` - Script principal con UI interactiva
- `README-MCP-MANAGER.md` - Documentación completa
- `install-mcp-manager.sh` - Instalador automático

### Uso del Servidor en Proyectos

**Proyecto lead-enrichment:**
- Configuración: `~/lead-enrichment/.mcp-config.json`
- Servidores habilitados: `MCP_DOCKER`, `mcp__odoo-api`, `mcp__perplexity-ask`
- Estado: ✅ Servidor odoo-api disponible y funcional

**Configuración Multi-Company:**
El archivo `.env` en este repositorio define 2 compañías:
1. **bmya** - Producción en https://www.bmya.cl
2. **companycl** - Testing local en http://host.docker.internal:8069

Cada proyecto que use el servidor puede especificar qué compañía usar al llamar las tools MCP.
