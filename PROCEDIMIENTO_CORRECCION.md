# Procedimiento de Corrección - Bug de Permisos Docker

**Fecha:** 2025-12-23
**Problema:** ModuleNotFoundError en contenedor Docker
**Solución:** Corrección de permisos en Dockerfile

---

## Comandos Ejecutados (En Orden)

### 1. Diagnóstico del Problema

```bash
# Verificar estado de contenedores
docker ps -a

# Verificar logs (contenedor no existía)
docker logs odoo-mcp-server
# Error: No such container

# Probar la imagen existente
docker run --rm bmya/odoo-mcp-server:latest python -c "import requests"
# ModuleNotFoundError: No module named 'requests' ❌

# Inspeccionar permisos dentro del contenedor
docker run --rm bmya/odoo-mcp-server:latest whoami
# Resultado: odoo

# Verificar ubicación de paquetes Python
docker run --rm --user root bmya/odoo-mcp-server:latest ls -la /root/.local/lib/python3.12/site-packages/
# Paquetes encontrados en /root/.local (propiedad de root)

# Confirmar que usuario odoo no puede acceder
docker run --rm bmya/odoo-mcp-server:latest ls -la /root/.local/
# Permission denied ❌

# Revisar PATH configurado
docker run --rm bmya/odoo-mcp-server:latest python -c "import sys; print('\n'.join(sys.path))"
# PATH no incluye /root/.local
```

**Diagnóstico:** Usuario `odoo` no tiene acceso a `/root/.local` donde están las dependencias.

---

### 2. Revisión del Código

```bash
# Ver Dockerfile actual
cat Dockerfile

# Ver historial de commits
git log --oneline

# Ver Dockerfile original del primer commit
git show 33c7f16:Dockerfile

# Confirmar que el bug está desde el inicio
git diff 33c7f16:Dockerfile HEAD:Dockerfile
# (Sin diferencias significativas - bug original)
```

**Conclusión:** El bug existe desde el commit inicial.

---

### 3. Corrección del Dockerfile

```bash
# Editar Dockerfile usando herramienta Edit
# Cambios realizados:
# - Línea 40-41: Crear usuario odoo ANTES de copiar archivos
# - Línea 44: COPY --from=builder --chown=odoo:odoo /root/.local /home/odoo/.local
# - Línea 47: COPY --chown=odoo:odoo src/ ./src/
# - Línea 53: ENV PATH=/home/odoo/.local/bin:$PATH

# Verificar cambios
git diff Dockerfile
```

**Cambios principales:**
1. Reordenar: Crear usuario odoo primero
2. Copiar dependencias a `/home/odoo/.local` con ownership
3. Actualizar PATH a `/home/odoo/.local/bin`

---

### 4. Reconstrucción de la Imagen

```bash
# Build de la imagen con correcciones
docker build -t bmya/odoo-mcp-server:latest /Users/danielb/ClaudeCodeProjects/claude-odoo-api

# Output:
# [+] Building 8.4s
# #15 exporting to image
# #15 exporting layers 2.4s done
# #15 naming to docker.io/bmya/odoo-mcp-server:latest done
# ✅ SUCCESS

# Verificar nueva imagen
docker images bmya/odoo-mcp-server
# CREATED: 5 hours ago (nueva)
# SIZE: 323MB
```

**Resultado:** Imagen reconstruida exitosamente con ID `b18c9297c061`

---

### 5. Verificación de la Corrección

```bash
# Test 1: Importar módulos Python
docker run --rm bmya/odoo-mcp-server:latest python -c "import requests; import mcp; print('✅ Módulos cargados correctamente')"
# ✅ Módulos cargados correctamente

# Test 2: Cargar configuración multi-company
docker run --rm -v /Users/danielb/ClaudeCodeProjects/claude-odoo-api/.env:/app/.env:ro bmya/odoo-mcp-server:latest python -c "
import sys
sys.path.insert(0, '/app/src')
from odoo_mcp_server import load_company_configs
configs = load_company_configs()
print(f'✅ Loaded {len(configs)} companies: {list(configs.keys())}')
"
# 2025-12-23 22:45:56,115 - odoo-mcp-server - INFO - Loaded 2 company configurations: ['bmya', 'companycl']
# ✅ Loaded 2 companies: ['bmya', 'companycl']

# Test 3: Verificar permisos de archivos
docker run --rm bmya/odoo-mcp-server:latest ls -la /home/odoo/.local/lib/python3.12/site-packages/ | head -10
# total 976
# drwxr-xr-x 91 odoo odoo 4096 Dec 23 17:31 .
# ... (todos los archivos propiedad de odoo:odoo) ✅

# Test 4: Verificar PATH
docker run --rm bmya/odoo-mcp-server:latest bash -c 'echo $PATH'
# /home/odoo/.local/bin:/usr/local/bin:...  ✅
```

**Todos los tests: EXITOSOS ✅**

---

### 6. Actualización del Docker MCP Toolkit

```bash
# Deshabilitar servidor para forzar recarga
docker mcp server disable odoo-api
# Tip: ✓ Server disabled

# Re-habilitar con nueva imagen
docker mcp server enable odoo-api
# Tip: ✓ Server enabled

# Verificar estado
docker mcp server list
# MCP Servers (2 enabled)
# NAME            OAUTH    SECRETS    CONFIG    DESCRIPTION
# odoo-api        -        -          -         (✓ habilitado)
# perplexity-ask  -        ✓ done     -         (✓ habilitado)
```

**Estado:** Servidor habilitado y usando nueva imagen.

---

### 7. Commit de Cambios

```bash
# Stage del Dockerfile corregido
git add Dockerfile

# Commit con mensaje descriptivo
git commit -m "[FIX] Dockerfile: Corregir permisos de usuario odoo

Problema:
- Las dependencias Python se copiaban a /root/.local pero el usuario
  ejecutor es 'odoo' sin acceso a ese directorio
- Causaba ModuleNotFoundError al ejecutar el contenedor

Solución:
- Copiar dependencias a /home/odoo/.local con ownership correcto
- Actualizar PATH a /home/odoo/.local/bin
- Crear usuario odoo ANTES de copiar archivos para aplicar --chown

Impacto:
- El contenedor ahora puede importar correctamente requests, mcp y
  todas las dependencias
- Se mantiene la seguridad al ejecutar como usuario no-root

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"

# Verificar commit
git log --oneline -1
# 26c7ba6 [FIX] Dockerfile: Corregir permisos de usuario odoo ✅

# Ver estado actual
git status
# On branch main
# Your branch is ahead of 'origin/main' by 1 commit.
# Changes not staged for commit:
#   modified:   CLAUDE.md
#   modified:   ESTADO_FINAL_2025-12-23.md
# Untracked files:
#   PROCEDIMIENTO_CORRECCION.md
```

**Commit:** Realizado exitosamente (hash: 26c7ba6)

---

## Archivos Modificados

### Dockerfile (COMMITEADO)
**Antes (líneas 40-52):**
```dockerfile
# Copy Python dependencies from builder
COPY --from=builder /root/.local /root/.local

# Copy the MCP server source
COPY src/ ./src/

# Create non-root user for security
RUN useradd -m -u 1000 odoo && chown -R odoo:odoo /app
USER odoo

# Set environment variables (can be overridden at runtime)
ENV PATH=/root/.local/bin:$PATH
```

**Después (líneas 40-53):**
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

**Diff:**
```diff
-COPY --from=builder /root/.local /root/.local
+RUN useradd -m -u 1000 odoo && chown -R odoo:odoo /app
+
+COPY --from=builder --chown=odoo:odoo /root/.local /home/odoo/.local
+
 COPY src/ ./src/

-RUN useradd -m -u 1000 odoo && chown -R odoo:odoo /app
+COPY --chown=odoo:odoo src/ ./src/
+
 USER odoo

-ENV PATH=/root/.local/bin:$PATH
+ENV PATH=/home/odoo/.local/bin:$PATH
```

### CLAUDE.md (PENDIENTE COMMIT)
- Agregada sección "🔧 CRITICAL FIX HISTORY - 2025-12-23"
- Documentación del problema, solución y verificación
- Agregada sección "MCP Server Management"
- Documentación de Docker MCP Toolkit y mcp-manager

### ESTADO_FINAL_2025-12-23.md (NUEVO, PENDIENTE COMMIT)
- Documento completo del estado final del proyecto
- Incluye checklist, próximos pasos y referencias

### PROCEDIMIENTO_CORRECCION.md (NUEVO, ESTE ARCHIVO)
- Comandos exactos ejecutados
- Proceso paso a paso de diagnóstico y corrección

---

## Pendientes

### Git
```bash
# Commitear documentación
git add CLAUDE.md ESTADO_FINAL_2025-12-23.md PROCEDIMIENTO_CORRECCION.md
git commit -m "docs: Documentar corrección crítica de permisos en Dockerfile"

# Push al repositorio remoto
git push origin main
```

### Docker Hub (opcional)
```bash
# Actualizar imagen pública
docker push bmya/odoo-mcp-server:latest
```

---

## Resumen Técnico

**Problema:** Permisos incorrectos en Dockerfile multi-stage build
**Causa:** Copiar dependencias a `/root/.local` pero ejecutar como usuario `odoo`
**Solución:** Copiar a `/home/odoo/.local` con `--chown=odoo:odoo`
**Tiempo de corrección:** ~2 horas (diagnóstico + corrección + verificación + documentación)
**Estado final:** ✅ Funcionando correctamente

**Lección aprendida:** En Docker multi-stage builds con usuarios no-root, asegurar que:
1. Usuario se crea ANTES de copiar archivos
2. COPY incluye `--chown=usuario:grupo`
3. PATH apunta a directorios accesibles por el usuario ejecutor
4. Verificar permisos antes de dar por terminado el build

---

**Generado:** 2025-12-23
**Por:** Claude Code (Sonnet 4.5)
