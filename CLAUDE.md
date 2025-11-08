# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an MCP (Model Context Protocol) server that provides tools to interact with Odoo 19's External JSON-2 API. The server supports multiple company/instance configurations and exposes 8 tools for CRUD operations on Odoo databases: list_companies, search_read, create, write, unlink, search, read, and search_count.

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

```bash
# Build and run
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down

# Build image only
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
