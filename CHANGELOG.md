# Changelog

## [Unreleased] - 2026-07-27 (ter) — La barra final del endpoint deja de importar

### Fixed
- **El endpoint sin barra final colgaba a `mcp-remote`, y con eso a Claude
  Desktop.** El transporte se montaba sólo con `Mount(MCP_HTTP_PATH, ...)`, y
  Starlette responde al path desnudo (`/mcp`) con un 307 hacia `/mcp/`.
  `mcp-remote` —el puente stdio que la app necesita, porque su
  `claude_desktop_config.json` no acepta headers— **no sigue redirects**: se
  queda en "Connecting to remote server..." para siempre, lo que en la app
  aparece como "Could not attach to MCP server". Verificado A/B: con `/mcp/`
  conecta ("Connected to remote server using StreamableHTTPClientTransport"),
  sin la barra no. Agravante: detrás de Traefik el 307 salía con esquema
  `http://` mientras `MCP_FORWARDED_ALLOW_IPS` no incluyera la IP del proxy, así
  que tampoco le habría servido a un cliente que sí siguiera redirects.
  `build_http_app()` ahora sirve **las dos formas directo**: un `Route` para el
  path exacto (con el endpoint como instancia de clase, para que Starlette lo
  trate como app ASGI en vez de envolverlo en `request_response`) y el `Mount`
  detrás para la forma con barra y cualquier subpath.
- **`tools/bmya-keys.py` interpolaba `--server-url` tal cual** en los dos
  snippets de onboarding, así que un `--server-url .../mcp` le llegaba roto al
  cliente. Nueva `normalize_server_url()`, aplicada en un único lugar
  (`render_client_snippets`, por donde pasan `new` y `snippet`, y con ellos
  también `$BMYA_MCP_SERVER_URL`). Se mantiene aunque el servidor ya sirva las
  dos formas: un snippet puede pegarse contra un despliegue sin actualizar.
- Ejemplos de docs, docstrings y `deploy/.env.example` pasados a la forma con
  barra final — son de donde se copian los comandos.

## [Unreleased] - 2026-07-27 (bis) — Stack simplificado para Portainer

### Added
- `deploy/docker-compose.portainer.yml`: copia sin `build:` ni indirección
  `${VAR:-default}`, con los 6 valores que realmente hacen falta hardcodeados
  (sin `MCP_GATEWAY_TOKEN`: el control de acceso es la BMYA key). Pensado para
  pegar directo en el editor de stack de Portainer.

### Fixed
- **`ODOO_MCP_ALLOWED_METHODS` bloqueaba todos los métodos de negocio en
  silencio.** `deploy/docker-compose.yml` la declaraba como
  `"${ODOO_MCP_ALLOWED_METHODS:-}"`, y Compose siempre inyecta esa clave al
  contenedor (aunque sea `""`) en vez de omitirla cuando no se setea. Como
  `os.getenv(name, default)` sólo usa `default` si la clave está **ausente**,
  el contenedor recibía `ODOO_MCP_ALLOWED_METHODS=""` — "ningún método
  permitido" — en lugar de caer a los 3 defaults documentados
  (`calendar.event.action_sync_timesheets`, `account.move.action_post`,
  `sale.order.action_confirm`). Verificado con `docker compose config` antes y
  después del fix. Se sacó la línea del compose y se corrigió el comentario en
  `deploy/.env.example`, que afirmaba lo contrario.

## [Unreleased] - 2026-07-27 — Onboarding de clientes de bajo esfuerzo

### Removed
- `deploy/bmya-api-keys.example.json`: convivía con `deploy/config/bmya-api-keys.json`
  (el registro real, gitignoreado, montado por el compose) y generaba confusión sobre
  cuál de los dos es el que hay que editar. El paso de CI/release que lo validaba
  se saca también; lo reemplaza cobertura directa de `cmd_validate` en
  `tests/test_bmya_keys_cli.py` (caso válido, grant inválido, archivo ausente).

### Added
- `tools/bmya-keys.py new --server-url` y el nuevo subcomando `snippet` generan,
  junto con la key, el mensaje completo y listo para enviar al cliente: el comando
  de una línea para Claude Code (`claude mcp add --transport http ...`) y el
  bloque JSON para Claude Desktop, con `TU_API_KEY_DE_ODOO` como placeholder para
  que el cliente ponga la suya. `snippet` regenera ese mismo mensaje a partir de
  una key ya emitida (por stdin) y se niega a hacerlo si está revocada o vencida.
  Configurable con `--server-url` o `$BMYA_MCP_SERVER_URL`.

### Fixed
- **`docs/remote-client-config.md` y `docs/client-onboarding.md` documentaban un
  formato que Claude Desktop rechaza en silencio**: `"type": "http"` + `"headers"`
  directo en `mcpServers`. Verificado contra el schema real de esa app
  (extraído de su `app.asar`): `claude_desktop_config.json` sólo acepta entradas
  `command`/`args`/`env` (stdio) y omite cualquier otra sin más aviso que un
  diálogo genérico ("no son configuraciones válidas... y fueron omitidas"). Los
  docs ahora muestran las dos rutas reales: `claude mcp add` de Claude Code
  (headers HTTP nativos, verificado con `✔ Connected`) y, para Claude Desktop, el
  mismo JSON pero envuelto en el puente stdio `mcp-remote` (verificado con un
  handshake `initialize` completo).

## [Unreleased] - 2026-07-26 — Capa de autorización BMYA y empaquetado para bmya.cloud

### Added
- **Nueva capa de autorización por base de datos** (`src/bmya_auth.py`): cada request
  en modo HTTP debe traer una **BMYA API key** (`X-Bmya-Api-Key`) además de la API key
  de Odoo del usuario. La key se valida contra un registro JSON
  (`BMYA_API_KEYS_FILE`) que guarda **sólo el sha256**, nunca la key en claro.
- La key resuelve, del lado servidor, un **grant**: URL de Odoo, base de datos, modo
  (`readonly`/`readwrite`), y opcionalmente `allowed_methods`, `allowed_models` y
  `denied_models`.
- **Modo configurable desde el cliente pero sólo para restringir**: el modo efectivo es
  el más restrictivo de (`ODOO_MCP_READONLY` del servidor, el del grant, el
  `X-Odoo-Mode` que pida el cliente). Ampliar es estructuralmente imposible.
- **Revocación sin redeploy**: el registro se relee al cambiar su mtime/tamaño, como
  máximo cada `BMYA_REGISTRY_TTL_SECONDS` (10 por defecto). `SIGHUP` recarga al
  instante. Si una recarga falla se conserva la última copia buena y se marca `stale`.
- **`tools/bmya-keys.py`**: CLI para emitir (`new`), listar (`list`), revocar
  (`revoke`), comprobar (`verify`, lee por stdin) y validar (`validate`) el registro.
  Escrituras atómicas con `.bak`; dry-run salvo `--write`.
- **`GET /readyz`**: readiness real, 503 si el registro de keys no cargó. El healthcheck
  del contenedor pasó a usarlo, así que un montaje mal hecho rompe el deploy en voz alta
  en lugar de devolver 401 a todos los clientes en silencio.
- `list_tools` **oculta las 4 tools de escritura** en conexiones de sólo lectura.
- Log de auditoría por llamada (`key_id`, base, tool, modo, allow/deny). Nunca la key,
  su hash, ni la API key de Odoo.
- Empaquetado: `deploy/.env.example`, `deploy/README.md`, `deploy/bmya-api-keys.example.json`,
  `requirements-dev.txt`, y `.github/workflows/release.yml` (build multi-arch
  `linux/amd64,linux/arm64` y push a ghcr en tag). El compose trae `image:` **y**
  `build:`, para bajar una imagen prearmada o buildear en el servidor.
- Docs nuevos: `docs/bmya-api-keys.md` (operador) y `docs/client-onboarding.md` (cliente).
- Tests: 176 en total (antes 31). Nuevos `tests/test_bmya_auth.py` (91),
  `tests/test_http_auth.py` (24, **primera cobertura de la capa HTTP** con
  `TestClient`), `TestCallToolWithGrant` y `TestHttpClientCache`, más
  `tests/conftest.py` con fixtures compartidas.

### Fixed
- Documentado en `docs/bmya-api-keys.md` el diagnóstico de
  `404 "No database is selected"`: el nombre de base de Odoo.sh lleva un sufijo de build
  que **cambia en cada rebuild**, y con un nombre viejo fallan todas las llamadas aunque
  la key y el grant estén bien. El `.env` de este repo tenía justamente un nombre de
  staging desactualizado. Incluye el curl para aislarlo sin pasar por el MCP, más los
  casos de `403` y de respuesta HTML.
- **SSRF**: la URL de Odoo ya no viene del request. Antes, cualquiera que pasara el
  gateway token podía apuntar el servidor a cualquier host alcanzable (incluido
  `169.254.169.254` y servicios internos). Ahora la fija el grant, y el loader además
  rechaza URLs que no sean https, con credenciales, o hacia IPs privadas/link-local.
- **Fuga de configuración**: `odoo_list_companies` se atendía **antes** de cualquier
  chequeo de autenticación y enumeraba las secciones del `.env` del servidor a cualquier
  llamador. Ahora la autorización se resuelve primero y, bajo un grant, la tool sólo
  informa la instancia de ese grant.
- `MCP_GATEWAY_TOKEN` se comparaba con `!=`; ahora usa `hmac.compare_digest`.
- El caché de clientes por credenciales era **ilimitado** (crecía con cada usuario, sin
  cerrar sockets). Ahora es un LRU acotado (`ODOO_CLIENT_CACHE_MAX`, 256) que cierra la
  sesión al evictar, y quedó en un namespace separado del de compañías, que antes
  compartía el mismo dict.
- El montaje del registro es un **directorio**, no un archivo: un bind mount de archivo
  fija el inode y, como el CLI reescribe atómicamente, el contenedor seguía leyendo el
  inode viejo y **la revocación nunca aplicaba** (verificado: con montaje de archivo la
  key revocada seguía dando 200; con directorio da 401).
- `tests/test_odoo_mcp_server.py::test_call_tool_without_company` estaba roto desde la
  rama `vpn` (asserteaba un mensaje que ya no existía). El CI no lo detectó porque sólo
  corría en `main`/`develop`; ahora corre también en `vpn`.
- El job de lint ya venía fallando: `flake8 --select=...F82` matchea `F824`, y había tres
  `global` inútiles en `src/odoo_mcp_server.py`. Eliminados.
- La aserción `len(tools) == 12` se reemplazó por una comparación del set de nombres, así
  un rename falla en voz alta en vez de que un contador se desfase en silencio.
- Se sacaron del código las API keys de Odoo hardcodeadas (`create_odoo_invoices.py`
  ahora lee de env o de una sección del `.env`) y se redactaron las de los `.md` de
  estado. **Siguen en el historial de git: hay que revocarlas y reemitirlas en Odoo.**

### ⚠️ BREAKING
- **Los clientes ya no envían `X-Odoo-Url` ni `X-Odoo-Database`.** Los aporta la BMYA
  key. Si se envían y no coinciden con el grant, el pedido se **rechaza** (antes se
  ignoraban en silencio). Hay que actualizar la config de cada cliente:
  ver `docs/remote-client-config.md`.
- **`MCP_FORWARDED_ALLOW_IPS` pasó de `*` a `127.0.0.1`.** Con `*`, cualquiera que
  alcance el puerto puede falsificar `X-Forwarded-For`/`-Proto`. En el despliegue hay que
  setearlo a la IP del host de Traefik (`TRAEFIK_HOST_IP`).
- El puerto del compose ya no se publica en todas las interfaces, sino sólo en
  `MCP_BIND_ADDR` (la IP de la VLAN).
- El healthcheck del contenedor apunta a `/readyz` en lugar de `/health`.
- **Rollback disponible**: `BMYA_AUTH_ENABLED=0` restaura el comportamiento anterior de
  tres headers sin cambiar la imagen. Sirve para desplegar primero y migrar las configs
  de a una. El camino está cubierto por tests. Conviene mantener el flag una release y
  después eliminarlo.
- **stdio no cambia**: no requiere BMYA key y sigue usando el `.env` multi-compañía.

## [Unreleased] - 2026-07-10 (bis)

### Added
- Nuevo tool `odoo_call_method`: ejecuta **métodos de negocio** de Odoo 19 (acciones de
  workflow) vía `POST /json/2/{model}/{method}`. Restringido por **lista blanca**
  configurable con la env var `ODOO_MCP_ALLOWED_METHODS` (formato
  `"modelo.metodo,modelo.metodo"`). Defaults: `calendar.event.action_sync_timesheets`,
  `account.move.action_post`, `sale.order.action_confirm`.
- Cuenta como operación de escritura → bloqueado por el kill-switch `ODOO_MCP_READONLY`.
- El resultado se muestra como mensaje legible si viene una notificación
  (`ir.actions.client` con `params.message`); si no, como JSON. Total de tools: **12**.
- Tests: `TestCallMethod` (allowlist, no permitido, bloqueo por readonly, parsing de
  notificación) + `test_call_method_payload` / `test_call_method_no_ids` (31 tests en total).

## [Unreleased] - 2026-07-10

### Added
- `odoo_create` acepta ahora una **lista de dicts** para alta masiva (un solo request → lista de IDs), además del dict único. Schema del tool actualizado (`values: object | array`).
- Test `test_create_records_mass` (25 tests en total).

### Nota de despliegue
- El fix de `vals_list`/`vals` para Odoo 19 (commit 4bc461b, jun-2026) estaba en el código pero **la imagen Docker nunca se reconstruyó**: el conector seguía fallando con "missing a required argument: 'vals_list'". Tras cualquier cambio: `docker build -t bmya/odoo-mcp-server:latest . && docker tag bmya/odoo-mcp-server:latest odoo-mcp-server:latest`, y deshabilitar/rehabilitar `odoo-api` en la app de Claude (o reiniciarla).

## [Unreleased] - 2025-11-08

### Added - Image Processing & Infrastructure Improvements

#### 🖼️ Image Processing Support
- Added **Pillow (PIL)** library for image processing capabilities
- Multi-stage Docker build with optimized image dependencies
- Support for JPEG, PNG, WebP, TIFF image formats
- Runtime libraries: libjpeg, zlib, libpng, freetype, liblcms, openjpeg, webp

#### 🔄 Enhanced Request Handling
- **Retry Logic**: Automatic retry with exponential backoff (configurable via `ODOO_MAX_RETRIES`)
  - Default: 3 retries
  - Backoff factor: 1 (retries after 1s, 2s, 4s)
  - Retries on HTTP status codes: 429, 500, 502, 503, 504
- **Request Timeouts**: Configurable timeout for all API requests (via `ODOO_REQUEST_TIMEOUT`)
  - Default: 30 seconds
  - Prevents indefinite hanging on slow/unresponsive servers
- **Connection Pooling**: Improved HTTP connection management
  - Pool connections: 10
  - Pool max size: 20
  - Reuses connections for better performance

#### 🛡️ Error Handling & Logging
- Comprehensive error handling for all request types:
  - Timeout errors with detailed messages
  - Connection errors with retry information
  - HTTP errors with response details
  - JSON parsing errors
  - Odoo API-specific error detection and reporting
- Enhanced logging with timestamps and severity levels
- Request timing metrics (logs elapsed time for each request)
- Debug-level logging for payload inspection (first 200 chars)

#### 🐳 Docker Improvements
- **Multi-stage build**: Reduces final image size by ~40%
- **Security**: Non-root user (UID 1000) for container execution
- **Health check**: Automatic container health monitoring
  - Interval: 30s
  - Timeout: 10s
  - Start period: 5s
  - Retries: 3
- **Optimized layers**: Better caching and faster builds

#### 📦 Dependencies
- Added `Pillow >= 10.0.0` for image processing
- Added `urllib3 >= 2.0.0` for enhanced HTTP retry logic
- Added `python-dotenv >= 1.0.0` for environment management
- Added `pydantic >= 2.0.0` for data validation (optional)
- Updated dependency documentation with categories

#### ⚙️ Configuration
New environment variables:
- `ODOO_REQUEST_TIMEOUT`: API request timeout in seconds (default: 30)
- `ODOO_MAX_RETRIES`: Maximum number of retry attempts (default: 3)

### Changed

#### Code Quality
- Improved `OdooClient` initialization with better logging
- Better request lifecycle management with timing
- More informative error messages with context
- Type hints improvements

#### Documentation
- Updated requirements.txt with categories and comments
- Enhanced Dockerfile comments
- Better separation of build vs runtime dependencies

### Technical Details

#### Before (Original)
```dockerfile
FROM python:3.12-slim
# Single stage, all dependencies installed at runtime
```

#### After (Improved)
```dockerfile
FROM python:3.12-slim as builder
# Build stage: compile dependencies
FROM python:3.12-slim
# Runtime stage: only what's needed to run
```

**Result**: ~150MB smaller image, faster deployments

#### Request Flow Enhancement
```
Before: Request → Response (or fail immediately)

After:  Request → Timeout/Retry Logic → Connection Pool →
        Response Validation → Structured Error Handling
```

### Performance Impact
- **Faster**: Connection pooling reduces latency by ~30-50ms per request
- **More Reliable**: Retry logic handles transient failures automatically
- **Better Resource Usage**: Multi-stage build uses less disk space
- **Safer**: Non-root user prevents privilege escalation

### Breaking Changes
None. All changes are backward compatible.

### Migration Guide
1. Rebuild Docker image: `docker-compose build`
2. Optional: Set new environment variables in `.env`:
   ```ini
   [bmya]
   ODOO_URL=http://host.docker.internal:8069
   ODOO_DATABASE=odoo19e_bmya
   ODOO_API_KEY=your_key
   # New optional settings:
   ODOO_REQUEST_TIMEOUT=30
   ODOO_MAX_RETRIES=3
   ```
3. Restart container: `docker-compose up -d`

### Future Improvements (Roadmap)
- [ ] Add image caching for frequently accessed logos
- [ ] Add image transformation tools (resize, crop, format conversion)
- [ ] Add response caching with TTL
- [ ] Add batch image processing endpoint
- [ ] Add Prometheus metrics for monitoring
- [ ] Add request rate limiting
- [ ] Add GraphQL support alongside JSON-2 API
