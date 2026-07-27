# Acceso a Odoo desde tu asistente de IA

BMYA ofrece un servidor MCP que le permite a tu asistente (Claude u otro cliente
compatible) leer y —si corresponde— escribir en tu Odoo.

## Qué recibís de BMYA

1. Una **BMYA API key**, que ya viene atada a tu instancia y tu base de datos.
2. Un mensaje con dos bloques listos para copiar, uno para cada cliente de IA
   posible (más abajo explicamos cuál usar).

Vas a recibir una key de **lectura** (`bmya_ro_…`) y, si tu caso lo requiere, una
separada de **escritura** (`bmya_rw_…`).

## Qué tenés que hacer vos

**1. Generar tu API key de Odoo.** En tu Odoo, con tu propio usuario:

> Preferencias de usuario → Seguridad de la cuenta → Nueva API key

Esto es importante: el asistente entra a Odoo **como vos**, con tus permisos y
quedando registrado a tu nombre. BMYA no tiene tu clave.

**2. Usar el bloque que corresponde a tu cliente de IA.**

Si usás **Claude Code** (terminal), copiá y pegá este único comando,
reemplazando `TU_API_KEY_DE_ODOO` por la que generaste en el paso 1:

```bash
claude mcp add --transport http odoo https://odoo-mcp.bmya.cloud/mcp/ \
  --header "X-Bmya-Api-Key: la-key-que-te-dio-bmya" \
  --header "X-Odoo-Api-Key: TU_API_KEY_DE_ODOO"
```

Si usás la **app Claude Desktop**, pegá este bloque en
*Configuración → Developer → Edit Config* (reemplazando también
`TU_API_KEY_DE_ODOO`) y reiniciá la app por completo:

```json
{
  "mcpServers": {
    "odoo": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote", "https://odoo-mcp.bmya.cloud/mcp/", "--transport", "http-only",
        "--header", "X-Bmya-Api-Key: la-key-que-te-dio-bmya",
        "--header", "X-Odoo-Api-Key: TU_API_KEY_DE_ODOO"
      ]
    }
  }
}
```

(Ese bloque usa `mcp-remote`, un puente estándar que Claude Desktop necesita para
hablar con servidores remotos como este. Necesita tener Node.js instalado; la
primera vez tarda unos segundos en descargarlo.)

**3. Verificar.** Pedile al asistente: *"listá las compañías de Odoo"*. Debería
responder con la URL y la base de datos de tu instancia, y el modo (lectura o
escritura). Si dice eso, está funcionando.

## Qué ve y qué no ve BMYA

- **BMYA no guarda tu clave de Odoo.** Viaja en tus pedidos y no se almacena.
- **BMYA no guarda el contenido de tus consultas.** El servidor sólo registra, por
  auditoría, qué key hizo qué operación y sobre qué base: nunca los datos.
- **Tu key sólo alcanza tu instancia.** No es técnicamente posible usarla para
  llegar a la base de otro cliente: la instancia está fijada del lado del
  servidor, no la elige quien hace el pedido.
- **Con una key de lectura no se puede escribir**, ni pidiéndolo explícitamente.
  El asistente no ve siquiera las herramientas de escritura.

## Preguntas frecuentes

**¿Puedo restringirme a sólo lectura aunque tenga una key de escritura?**
Sí: agregá un header más, `X-Odoo-Mode: readonly` (un `--header` extra en Claude
Code, o un par más en `args` en Claude Desktop). Al revés no funciona: una key de
lectura no se puede ampliar.

**¿Cómo se revoca el acceso?**
Avisale a BMYA y la key deja de funcionar en segundos, sin que tengas que hacer
nada. También podés revocar tu propia API key desde Odoo, lo que corta el acceso
inmediatamente por tu lado.

**¿Y si se filtra mi BMYA key?**
Avisá y la revocamos, y te emitimos otra. Por diseño, esa key sola no da acceso a
Odoo: hace falta además una API key válida de Odoo.

**¿Qué pasa si pierdo la key?**
No se puede recuperar (en el servidor sólo queda su huella criptográfica). Se
emite una nueva y se revoca la anterior.

**¿Puedo usar varias bases?**
Sí: una entrada por base, cada una con su propia BMYA key.

**¿Vence?**
Puede tener fecha de vencimiento. Si la tiene, te la informamos al entregarla.

## Si algo no funciona

| Qué pasa | Qué hacer |
|---|---|
| El asistente dice que no está autorizado | La BMYA key está mal copiada, venció o fue revocada. Escribinos. |
| Pide la API key de Odoo | Falta el header `X-Odoo-Api-Key` o está vacío. |
| Dice que la conexión es de sólo lectura | Necesitás una key de escritura. Escribinos. |
| Un modelo o una acción "no está disponible" | Está deliberadamente fuera del alcance de tu key. Escribinos si lo necesitás. |
| Se queda "conectando" y nunca termina | Fijate que la URL termine en barra: `.../mcp/`, no `.../mcp`. |

Detalle técnico completo en
[remote-client-config.md](remote-client-config.md).
