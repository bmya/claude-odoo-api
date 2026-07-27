# Acceso a Odoo desde tu asistente de IA

BMYA ofrece un servidor MCP que le permite a tu asistente (Claude u otro cliente
compatible) leer y —si corresponde— escribir en tu Odoo.

## Qué recibís de BMYA

1. Una **BMYA API key**, que ya viene atada a tu instancia y tu base de datos.
2. Un bloque JSON listo para pegar en la configuración de tu cliente.

Vas a recibir una key de **lectura** (`bmya_ro_…`) y, si tu caso lo requiere, una
separada de **escritura** (`bmya_rw_…`).

## Qué tenés que hacer vos

**1. Generar tu API key de Odoo.** En tu Odoo, con tu propio usuario:

> Preferencias de usuario → Seguridad de la cuenta → Nueva API key

Esto es importante: el asistente entra a Odoo **como vos**, con tus permisos y
quedando registrado a tu nombre. BMYA no tiene tu clave.

**2. Pegar la configuración** que te enviamos, completando tu API key de Odoo:

```json
{
  "mcpServers": {
    "odoo": {
      "type": "http",
      "url": "https://odoo-mcp.bmya.cloud/mcp",
      "headers": {
        "X-Bmya-Api-Key": "la-key-que-te-dio-bmya",
        "X-Odoo-Api-Key": "la-api-key-que-generaste-en-odoo"
      }
    }
  }
}
```

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
Sí. Agregá `"X-Odoo-Mode": "readonly"` a los headers. Al revés no funciona: una
key de lectura no se puede ampliar.

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

Detalle técnico completo en
[remote-client-config.md](remote-client-config.md).
