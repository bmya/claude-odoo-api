# MCP local para demos (`mcp-odoo-local`)

Variante stdio del servidor pensada para el Odoo local (p. ej. el contenedor
`odoo20e` en `localhost:8369`). Permite preparar una demo por cliente sin tocar
la configuración: duplicás una plantilla y Claude carga clientes, productos,
etc. en la base nueva.

Funciona en Odoo 17 a 20. En 19 y 20 usa JSON-2; en 17 y 18, JSON-RPC, que
requiere `ODOO_LOGIN`.

## Cómo funciona

- **`[DEFAULT]` = la instancia local.** Si define `ODOO_URL` y `ODOO_API_KEY`,
  cualquier base que la instancia liste (dentro de su `dbfilter`) se usa
  directamente por nombre: `company="odoo20e_demo_clientex"`. No hace falta una
  sección por base.
- **La plantilla trae la key.** Duplicar una base copia sus usuarios y sus API
  keys, así que todas las bases clonadas de `odoo20e_template_cl` aceptan la
  misma key (admin/admin).
- **El config se relee al cambiar.** Editar `local/odoo.env` no requiere
  reiniciar el MCP. En Docker Desktop el cambio tarda ~1 s en verse dentro del
  contenedor.
- **Modo por sección.** `ODOO_MODE=readonly` bloquea create/write/unlink/call_method
  en esa sección, y también cuando la base se nombra por su nombre de base. Así
  la plantilla queda protegida.
- **Métodos.** Con `ODOO_MCP_ALLOWED_METHODS=*` `odoo_call_method` acepta
  cualquier método. También se aceptan patrones como `sale.order.*`.

## Tools propias del modo local

Estas tools no se ofrecen en el modo HTTP.

| Tool | Qué hace |
|---|---|
| `odoo_list_databases` | Muestra las secciones del config (base y modo) y las bases que lista cada instancia. |
| `odoo_duplicate_database` | `source` (sección o base), `target` y `neutralize` opcional. Copia la base y su filestore vía `/web/database/duplicate`. Requiere `ODOO_MASTER_PASSWORD`. No reintenta, para no duplicar dos veces. |

Para borrar una base de demo se usa `http://localhost:8369/web/database/manager`;
el MCP no la borra.

## Puesta en marcha

1. **Plantilla.** Crear `odoo20e_template_cl` (admin/admin, Chile, es_CL, los
   módulos de la demo). Después, en Preferencias → Seguridad de la cuenta,
   crear una API key con **scope RPC** y duración **persistente**. El scope
   "MCP" corresponde al MCP propio de Odoo Enterprise (`ai_mcp`, ruta `/mcp`);
   este servidor usa `/json/2`, que valida el scope `rpc`.
2. **Config.** Copiar `local/odoo.env.example` a `local/odoo.env` (está en el
   gitignore) y completar `ODOO_API_KEY` y `ODOO_MASTER_PASSWORD` (el
   `admin_passwd` del `odoo.conf`).
3. **Imagen.** Desde la raíz del worktree:
   ```bash
   docker build -t bmya/odoo-mcp-server:local .
   ```
4. **Registro en la app de Claude.** Agregar en
   `~/Library/Application Support/Claude/claude_desktop_config.json` y reiniciar
   la app:
   ```json
   "mcp-odoo-local": {
     "command": "docker",
     "args": ["run", "-i", "--rm",
       "-v", "/Users/danielb/ClaudeCodeProjects/claude-odoo-api-local/local:/app/local:ro",
       "-e", "ODOO_CONFIG_FILE=/app/local/odoo.env",
       "-e", "ODOO_MCP_ALLOWED_METHODS=*",
       "-e", "MCP_SERVER_NAME=mcp-odoo-local",
       "bmya/odoo-mcp-server:local"]
   }
   ```
   Se monta el **directorio** `local/`, no el archivo. Un bind mount de un
   archivo fija el inode, y los editores que guardan con rename dejarían al
   contenedor leyendo la versión vieja.

   Para que un rebuild de la imagen tome efecto, deshabilitá y volvé a
   habilitar el MCP en la app.

## Flujo de una demo

> "Duplicá `template_cl` como `odoo20e_demo_clientex` y cargá 10 clientes,
> 15 productos y 3 cotizaciones confirmadas orientadas a una distribuidora de
> repuestos."

Claude hace, en orden:

1. `odoo_duplicate_database(source="template_cl", target="odoo20e_demo_clientex")`
2. `odoo_create` con `company="odoo20e_demo_clientex"` para clientes y productos
3. `odoo_call_method sale.order.action_confirm` para confirmar las cotizaciones

El nombre de la base debe pasar el `dbfilter` de la instancia (`odoo20e_` en
el contenedor `odoo20e`). Si no lo pasa, la base se crea pero no es
alcanzable, y la tool lo avisa.

## Variables

| Dónde | Clave | Uso |
|---|---|---|
| config | `ODOO_URL`, `ODOO_API_KEY`, `ODOO_LOGIN` | Instancia y credencial. En `[DEFAULT]` aplican a todas las secciones y a las bases nombradas directamente. |
| config | `ODOO_DATABASE` | Base de una sección. |
| config | `ODOO_MODE` | `readwrite` (por defecto) o `readonly`. |
| config | `ODOO_MASTER_PASSWORD` | Solo para `odoo_duplicate_database`. |
| env | `ODOO_MCP_ALLOWED_METHODS` | `*`, `model.*` o una lista de `model.method`. |
| env | `ODOO_MCP_READONLY` | Kill-switch global: bloquea también la duplicación. |
| env | `MCP_SERVER_NAME` | Nombre que anuncia el servidor (`mcp-odoo-local`). |
| env | `ODOO_DB_OPERATION_TIMEOUT` | Timeout de la duplicación en segundos (600 por defecto). |
| env | `ODOO_DB_LIST_TTL` | Cuánto se cachea la lista de bases (10 s por defecto). |
