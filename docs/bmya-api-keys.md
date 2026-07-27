# Registro de BMYA API keys (guía del operador)

Cada key que emite BMYA autoriza **una base de datos** con **un modo** (lectura o
escritura). El servidor las valida contra un JSON local, que guarda sólo el
`sha256` de cada key.

## Por qué está diseñado así

- **La key fija la URL y la base.** El cliente no las manda. Además de reducir su
  configuración a dos headers, elimina la posibilidad de que alguien apunte el
  servidor a cualquier host alcanzable (era un SSRF: bastaba pasar la puerta de
  entrada y mandar la URL que se quisiera).
- **Sólo se guarda el hash.** Si el JSON se filtra —backup, snapshot, commit por
  error— no sirve para entrar. El precio es que una key perdida no se recupera:
  se revoca y se emite otra.
- **El modo lo lleva la key.** Una `bmya_ro_` y una `bmya_rw_` por base. Quitarle
  la escritura a alguien es borrar o revocar una key, sin tocar su acceso de
  lectura.
- **El cliente sólo puede restringirse.** El modo efectivo es el más restrictivo
  de tres: el techo del servidor (`ODOO_MCP_READONLY`), el de la key, y el
  `X-Odoo-Mode` que pida el cliente. Ampliar es estructuralmente imposible.
- **Revocar es editar un archivo.** Sin redeploy y sin reiniciar.

## Formato de la key

```
bmya_<ro|rw>_<key_id>_<secreto>
   │     │        │        └─ 256 bits de entropía (secrets.token_urlsafe(32))
   │     │        └─ 6 hex, identificador público: aparece en los logs y se usa para revocar
   │     └─ pista humana del modo; NO es autoritativa (manda el `mode` del registro)
   └─ prefijo fijo
```

El `key_id` es público a propósito: permite auditar y revocar sin manejar nunca el
secreto. Si el prefijo `ro`/`rw` no coincide con el `mode` del registro, el
servidor avisa por log y **gana el registro**.

El sha256 sin sal es correcto acá justamente porque el secreto es aleatorio de 256
bits: no hay superficie de fuerza bruta ni de rainbow table, y es lo que permite
buscar en el registro en O(1).

## Esquema

```json
{
  "version": 1,
  "updated_at": "2026-07-26T12:00:00+00:00",
  "grants": [
    {
      "key_id": "a3f19c",
      "key_sha256": "…64 hex minúsculas…",
      "label": "ClienteX prod - lectura",
      "odoo_url": "https://clientex.bmya.cloud",
      "database": "clientex_prod",
      "mode": "readonly",
      "allowed_methods": null,
      "allowed_models": null,
      "denied_models": ["res.users", "ir.config_parameter", "ir.mail_server"],
      "revoked": false,
      "expires_at": "2027-01-31T23:59:59+00:00",
      "created_at": "2026-07-26T12:00:00+00:00",
      "revoked_at": null,
      "notes": "contacto: juan@clientex.cl"
    }
  ]
}
```

| Campo | Obligatorio | Notas |
|---|---|---|
| `key_id` | sí | 6 hex. Público. Único en el archivo. |
| `key_sha256` | sí | 64 hex minúsculas. Único. |
| `odoo_url` | sí | `https://` obligatorio, sin credenciales, sin query. Se rechaza si es IP loopback/privada/link-local, o si no termina en un sufijo de `BMYA_ALLOWED_URL_SUFFIXES`. |
| `database` | sí | Nombre exacto de la base. |
| `mode` | sí | `readonly` o `readwrite`. Cualquier otra cosa **rechaza el grant** (un typo tiene que hacer ruido, no degradar en silencio). |
| `allowed_methods` | no | Ausente o `null` ⇒ **hereda** la lista del servidor. `[]` ⇒ **ningún** método. Una lista ⇒ se intersecta con `ODOO_MCP_ALLOWED_METHODS`, así que sólo puede restringir. |
| `allowed_models` | no | `null` ⇒ sin restricción. Lista ⇒ allowlist. |
| `denied_models` | no | Se aplica último y gana siempre, incluso sobre `allowed_models`. |
| `revoked` | no | `true` ⇒ la key deja de funcionar. |
| `expires_at` | no | ISO-8601. Sin zona se interpreta UTC, así que el vencimiento no depende de la zona del servidor. |
| `label`, `notes`, `created_at`, `revoked_at` | no | Sólo para vos. `label` aparece en `odoo_list_companies` y en `list`. |

Nota sobre `allowed_methods`: es el único campo donde **ausente ≠ vacío**. Es lo
que permite que una key opte por no habilitar ningún método sin tener que
enumerarlos.

Un grant inválido se descarta con un ERROR en el log y **el resto sigue
funcionando** (no se cae todo el archivo por un typo en una entrada). `validate`
del CLI falla si se descartó alguno, así que usalo como gate antes de desplegar.

## Operación

Emitir (la key en claro se imprime **una sola vez**):

```bash
python tools/bmya-keys.py new --file deploy/config/bmya-api-keys.json --write \
  --database clientex_prod --url https://clientex.bmya.cloud \
  --mode readonly --label "ClienteX lectura" --expires 2027-01-31
```

Sin `--write` imprime la key y la entrada, y no toca el archivo (útil para
revisar antes de aplicar).

Listar (nunca muestra secretos ni hashes):

```bash
python tools/bmya-keys.py list --file deploy/config/bmya-api-keys.json
```

Revocar:

```bash
python tools/bmya-keys.py revoke a3f19c --file deploy/config/bmya-api-keys.json --write
```

Comprobar a qué grant corresponde una key en claro (se lee por stdin, nunca por
argv, para que no quede en el historial del shell):

```bash
echo "$KEY" | python tools/bmya-keys.py verify --stdin --file deploy/config/bmya-api-keys.json
```

Validar el archivo (exit ≠ 0 si algo se rechazó):

```bash
python tools/bmya-keys.py validate --file deploy/config/bmya-api-keys.json
```

Las escrituras son atómicas (tempfile + rename, modo `600`) y dejan un `.bak`.

## Cuánto tarda en aplicar un cambio

El servidor relee el archivo cuando cambian su mtime o su tamaño, y como máximo
cada `BMYA_REGISTRY_TTL_SECONDS` (10 por defecto). Para que sea inmediato:

```bash
cd deploy && docker compose kill -s HUP odoo-api
```

**El registro se monta como directorio, no como archivo.** Un bind mount de un
archivo suelto fija su inode, y como el CLI reescribe de forma atómica, el
contenedor se quedaría leyendo el inode viejo y **la revocación nunca aplicaría**.
Ver [../deploy/README.md](../deploy/README.md).

## Permisos y respaldo

```bash
sudo chown -R 1000:1000 deploy/config && sudo chmod 600 deploy/config/bmya-api-keys.json
```

El contenedor corre como uid 1000 y los bind mounts conservan los uid del host. El
servidor avisa por log si el archivo es legible más allá de su dueño.

El archivo no tiene secretos utilizables (sólo hashes), pero **perderlo deja a
todos afuera**: incluilo en el backup. Si se pierde, hay que reemitir todas las
keys.

## Qué se registra en los logs

Cada llamada deja una línea de auditoría con `key_id`, base, tool, modo efectivo y
allow/deny. **Nunca** se loguea la key, ni su hash, ni la API key de Odoo del
usuario.

## Fase 2

La validación va a pasar a consultarse contra `www.bmya.cl`. `get_registry()` en
`src/bmya_auth.py` es la única función que sabe de dónde salen los grants, así que
ese cambio es implementar un backend nuevo y despachar por `BMYA_KEYS_BACKEND`:
`resolve_grant`, el middleware y `call_tool` no se tocan.
