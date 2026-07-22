#!/usr/bin/env python3
"""
Odoo 19 MCP Server

This MCP server provides tools to interact with Odoo 19's External JSON-2 API.
Supports multiple company configurations with enhanced error handling, retry logic,
and image processing capabilities.
"""

import os
import json
import asyncio
import hashlib
import logging
import time
from typing import Any, Optional, Dict
from configparser import ConfigParser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from mcp.server import Server
from mcp.types import Tool, TextContent
import mcp.server.stdio

# Configure logging with more detail
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("odoo-mcp-server")

# Configuration file path
CONFIG_FILE = os.getenv("ODOO_CONFIG_FILE", ".env")

# Request configuration from environment
REQUEST_TIMEOUT = int(os.getenv("ODOO_REQUEST_TIMEOUT", "30"))
MAX_RETRIES = int(os.getenv("ODOO_MAX_RETRIES", "3"))

# Transport: "stdio" (default, local one-process-per-user) or "http"
# (Streamable HTTP, shareable over the network). In HTTP mode credentials are
# supplied per request via headers (see resolve_odoo_client), so the server
# stores no Odoo credentials of its own.
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio").lower()
MCP_HTTP_HOST = os.getenv("MCP_HTTP_HOST", "127.0.0.1")
MCP_HTTP_PORT = int(os.getenv("MCP_HTTP_PORT", "8080"))
MCP_HTTP_PATH = os.getenv("MCP_HTTP_PATH", "/mcp")

# TLS: if both are set, uvicorn serves HTTPS directly. Otherwise plain HTTP is
# served and TLS is expected to be terminated by a reverse proxy (Traefik).
MCP_TLS_CERTFILE = os.getenv("MCP_TLS_CERTFILE") or None
MCP_TLS_KEYFILE = os.getenv("MCP_TLS_KEYFILE") or None
# Which client IPs are trusted to set X-Forwarded-* (default: trust the proxy).
MCP_FORWARDED_ALLOW_IPS = os.getenv("MCP_FORWARDED_ALLOW_IPS", "*")

# Optional gateway token: when set, HTTP requests must carry a matching
# X-Gateway-Token header. A reconfigurable "front door" independent of the
# network gate (e.g. Twingate/VLAN).
MCP_GATEWAY_TOKEN = os.getenv("MCP_GATEWAY_TOKEN") or None

# Per-request credential headers (case-insensitive) used in HTTP transport.
HEADER_URL = "x-odoo-url"
HEADER_DB = "x-odoo-database"
HEADER_KEY = "x-odoo-api-key"

# Read-only kill-switch: when enabled, write operations (create/write/unlink)
# are blocked without redeploying. Useful to protect production instances.
READ_ONLY = os.getenv("ODOO_MCP_READONLY", "").lower() in ("1", "true", "yes")

# Allowlist of Odoo business methods that odoo_call_method may invoke.
# Configurable via ODOO_MCP_ALLOWED_METHODS ("model.method,model.method").
DEFAULT_ALLOWED_METHODS = [
    "calendar.event.action_sync_timesheets",
    "account.move.action_post",
    "sale.order.action_confirm",
]
ODOO_ALLOWED_METHODS = {
    m.strip()
    for m in os.getenv("ODOO_MCP_ALLOWED_METHODS", ",".join(DEFAULT_ALLOWED_METHODS)).split(",")
    if m.strip()
}


class OdooClient:
    """Client for interacting with Odoo JSON-2 API with retry logic and connection pooling"""

    def __init__(self, url: str, database: str, api_key: str):
        self.url = url.rstrip("/")
        self.database = database
        self.api_key = api_key

        # Create session with retry logic
        self.session = self._create_session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "X-Odoo-Database": database,
            "Content-Type": "application/json"
        })

        logger.info(f"Initialized OdooClient for {database} at {url}")

    def _create_session(self) -> requests.Session:
        """Create a requests session with retry logic and connection pooling"""
        session = requests.Session()

        # Configure retry strategy
        retry_strategy = Retry(
            total=MAX_RETRIES,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS", "TRACE"],
            backoff_factor=1  # Will retry after 1, 2, 4 seconds
        )

        # Mount adapter with retry strategy
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=20
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        return session

    def _make_request(self, model: str, method: str, payload: dict) -> Any:
        """Make a request to the Odoo API with timeout and error handling"""
        endpoint = f"{self.url}/json/2/{model}/{method}"

        start_time = time.time()
        logger.debug(f"Making request to {endpoint} with payload: {json.dumps(payload, default=str)[:200]}...")

        try:
            response = self.session.post(
                endpoint,
                json=payload,
                timeout=REQUEST_TIMEOUT
            )

            elapsed = time.time() - start_time
            logger.debug(f"Request completed in {elapsed:.2f}s with status {response.status_code}")

            response.raise_for_status()
            result = response.json()

            # Validate response structure
            if isinstance(result, dict) and 'error' in result:
                error_msg = result.get('error', {}).get('message', 'Unknown error')
                logger.error(f"Odoo API returned error: {error_msg}")
                raise ValueError(f"Odoo API error: {error_msg}")

            return result

        except requests.exceptions.Timeout:
            logger.error(f"Request timeout after {REQUEST_TIMEOUT}s for {endpoint}")
            raise TimeoutError(f"Request to Odoo API timed out after {REQUEST_TIMEOUT}s")

        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error to {endpoint}: {e}")
            raise ConnectionError(f"Failed to connect to Odoo API: {e}")

        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP error {e.response.status_code} for {endpoint}: {e}")
            try:
                error_detail = e.response.json()
                raise ValueError(f"Odoo API HTTP {e.response.status_code}: {error_detail}")
            except json.JSONDecodeError:
                raise ValueError(f"Odoo API HTTP {e.response.status_code}: {e.response.text}")

        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed for {endpoint}: {e}")
            raise

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from {endpoint}: {e}")
            raise ValueError(f"Invalid JSON response from Odoo API: {e}")

        except Exception as e:
            logger.error(f"Unexpected error for {endpoint}: {e}")
            raise

    def search_read(
        self,
        model: str,
        domain: list,
        fields: Optional[list] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order: Optional[str] = None
    ) -> list:
        """Search and read records"""
        payload = {"domain": domain}
        if fields:
            payload["fields"] = fields
        if limit:
            payload["limit"] = limit
        if offset:
            payload["offset"] = offset
        if order:
            payload["order"] = order

        return self._make_request(model, "search_read", payload)

    def create(self, model: str, values) -> Any:
        """Create one record (dict) or several records (list of dicts).

        Odoo 19 /json/2 expects the ``vals_list`` kwarg; it accepts a single
        dict or a list of dicts (mass creation). Returns the new id(s).
        """
        payload = {"vals_list": values}
        return self._make_request(model, "create", payload)

    def write(self, model: str, ids: list, values: dict) -> bool:
        """Update existing records"""
        payload = {"ids": ids, "vals": values}
        return self._make_request(model, "write", payload)

    def unlink(self, model: str, ids: list) -> bool:
        """Delete records"""
        payload = {"ids": ids}
        return self._make_request(model, "unlink", payload)

    def search(
        self,
        model: str,
        domain: list,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order: Optional[str] = None
    ) -> list:
        """Search for record IDs"""
        payload = {"domain": domain}
        if limit:
            payload["limit"] = limit
        if offset:
            payload["offset"] = offset
        if order:
            payload["order"] = order

        return self._make_request(model, "search", payload)

    def read(self, model: str, ids: list, fields: Optional[list] = None) -> list:
        """Read specific records by ID"""
        payload = {"ids": ids}
        if fields:
            payload["fields"] = fields

        return self._make_request(model, "read", payload)

    def search_count(self, model: str, domain: list) -> int:
        """Count records matching domain"""
        payload = {"domain": domain}
        return self._make_request(model, "search_count", payload)

    def call_method(
        self,
        model: str,
        method: str,
        ids: Optional[list] = None,
        kwargs: Optional[dict] = None
    ) -> Any:
        """Invoke an Odoo business method via /json/2/{model}/{method}."""
        payload = {**(kwargs or {})}
        if ids:
            payload["ids"] = ids
        return self._make_request(model, method, payload)


def _format_call_method_result(result: Any) -> str:
    """Render a business-method result: readable notification message if present,
    otherwise the raw JSON (handles ir.actions.client dicts, bool and None)."""
    if isinstance(result, dict):
        message = result.get("params", {}).get("message")
        if message:
            return message
    return json.dumps(result, indent=2, default=str)


# Initialize the MCP server
app = Server("odoo-mcp-server")

# Store multiple company configurations
company_configs: Dict[str, Dict[str, str]] = {}
odoo_clients: Dict[str, OdooClient] = {}


def load_company_configs() -> Dict[str, Dict[str, str]]:
    """Load company configurations from .env file"""
    global company_configs

    if company_configs:
        return company_configs

    if not os.path.exists(CONFIG_FILE):
        raise ValueError(f"Configuration file not found: {CONFIG_FILE}")

    config = ConfigParser()
    config.read(CONFIG_FILE)

    for section in config.sections():
        company_configs[section] = {
            'url': config.get(section, 'ODOO_URL'),
            'database': config.get(section, 'ODOO_DATABASE'),
            'api_key': config.get(section, 'ODOO_API_KEY'),
            'company_id': config.get(section, 'COMPANY_ID', fallback='1')
        }

    if not company_configs:
        raise ValueError("No company configurations found in .env file")

    logger.info(f"Loaded {len(company_configs)} company configurations: {list(company_configs.keys())}")
    return company_configs


def get_odoo_client(company: str) -> OdooClient:
    """Get or create Odoo client instance for specific company"""
    global odoo_clients

    if company not in odoo_clients:
        configs = load_company_configs()

        if company not in configs:
            available = ", ".join(configs.keys())
            raise ValueError(f"Company '{company}' not found. Available companies: {available}")

        config = configs[company]
        odoo_clients[company] = OdooClient(
            config['url'],
            config['database'],
            config['api_key']
        )
        logger.info(f"Created Odoo client for company: {company}")

    return odoo_clients[company]


def list_available_companies() -> list[str]:
    """Get list of available company names"""
    configs = load_company_configs()
    return list(configs.keys())


def _get_or_create_client(url: str, database: str, api_key: str) -> OdooClient:
    """Get or create a client keyed by a hash of its credentials.

    Keying on the credential hash (not on a company name) isolates tenants:
    two users with different Odoo credentials never share a cached client,
    while connection pooling is preserved for repeated calls with the same
    credentials.
    """
    global odoo_clients
    key = hashlib.sha256(f"{url}|{database}|{api_key}".encode()).hexdigest()
    if key not in odoo_clients:
        odoo_clients[key] = OdooClient(url, database, api_key)
    return odoo_clients[key]


def _request_headers():
    """Return the inbound HTTP headers when running under HTTP transport, else None.

    Under stdio transport there is no request object, so this returns None and
    callers fall back to the .env/company configuration.
    """
    try:
        ctx = app.request_context
    except LookupError:
        return None
    request = getattr(ctx, "request", None)
    return request.headers if request is not None else None


def resolve_odoo_client(arguments: dict) -> OdooClient:
    """Resolve the Odoo client for a tool call.

    HTTP transport: credentials come from the X-Odoo-* connection headers
    (per-user, multi-tenant). stdio transport (or missing headers): fall back
    to the 'company' argument against the server .env.
    """
    headers = _request_headers()
    if headers is not None:
        url = headers.get(HEADER_URL)
        database = headers.get(HEADER_DB)
        api_key = headers.get(HEADER_KEY)
        if url and database and api_key:
            return _get_or_create_client(url, database, api_key)

    company = arguments.get("company")
    if company:
        return get_odoo_client(company)

    raise ValueError(
        "No Odoo credentials provided. Send X-Odoo-Url, X-Odoo-Database and "
        "X-Odoo-Api-Key headers on the MCP connection, or configure a company "
        "in the server .env and pass the 'company' argument."
    )


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available Odoo tools"""
    return [
        Tool(
            name="odoo_list_companies",
            description="List all available company configurations",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="odoo_search_read",
            description="Search and read records from an Odoo model. Combines search and read operations.",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "Optional. Company configuration name from the server .env sections (stdio/local mode). In HTTP mode credentials come from the X-Odoo-* connection headers and this is ignored."
                    },
                    "model": {
                        "type": "string",
                        "description": "The Odoo model name (e.g., 'res.partner', 'account.move', 'product.product')"
                    },
                    "domain": {
                        "type": "array",
                        "description": "Search domain as a list of criteria (e.g., [['name', '=', 'John']]). Use [] for all records.",
                        "default": []
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of field names to retrieve. If not specified, returns all fields."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of records to return"
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of records to skip"
                    },
                    "order": {
                        "type": "string",
                        "description": "Sorting order (e.g., 'name asc', 'create_date desc')"
                    }
                },
                "required": ["model"]
            }
        ),
        Tool(
            name="odoo_create",
            description="Create one record (dict) or several records (list of dicts) in an Odoo model",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "Optional. Company configuration name from the server .env (stdio/local mode). In HTTP mode credentials come from the X-Odoo-* connection headers and this is ignored."
                    },
                    "model": {
                        "type": "string",
                        "description": "The Odoo model name (e.g., 'res.partner', 'account.move')"
                    },
                    "values": {
                        "type": ["object", "array"],
                        "items": {"type": "object"},
                        "description": "Field values for the new record (dict), or a list of dicts for mass creation"
                    }
                },
                "required": ["model", "values"]
            }
        ),
        Tool(
            name="odoo_write",
            description="Update existing records in an Odoo model",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "Optional. Company configuration name from the server .env (stdio/local mode). In HTTP mode credentials come from the X-Odoo-* connection headers and this is ignored."
                    },
                    "model": {
                        "type": "string",
                        "description": "The Odoo model name"
                    },
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "List of record IDs to update"
                    },
                    "values": {
                        "type": "object",
                        "description": "Dictionary of field values to update"
                    }
                },
                "required": ["model", "ids", "values"]
            }
        ),
        Tool(
            name="odoo_unlink",
            description="Delete records from an Odoo model",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "Optional. Company configuration name from the server .env (stdio/local mode). In HTTP mode credentials come from the X-Odoo-* connection headers and this is ignored."
                    },
                    "model": {
                        "type": "string",
                        "description": "The Odoo model name"
                    },
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "List of record IDs to delete"
                    }
                },
                "required": ["model", "ids"]
            }
        ),
        Tool(
            name="odoo_search",
            description="Search for record IDs matching criteria (without reading full records)",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "Optional. Company configuration name from the server .env (stdio/local mode). In HTTP mode credentials come from the X-Odoo-* connection headers and this is ignored."
                    },
                    "model": {
                        "type": "string",
                        "description": "The Odoo model name"
                    },
                    "domain": {
                        "type": "array",
                        "description": "Search domain as a list of criteria",
                        "default": []
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of IDs to return"
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of records to skip"
                    },
                    "order": {
                        "type": "string",
                        "description": "Sorting order"
                    }
                },
                "required": ["model"]
            }
        ),
        Tool(
            name="odoo_read",
            description="Read specific records by their IDs",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "Optional. Company configuration name from the server .env (stdio/local mode). In HTTP mode credentials come from the X-Odoo-* connection headers and this is ignored."
                    },
                    "model": {
                        "type": "string",
                        "description": "The Odoo model name"
                    },
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "List of record IDs to read"
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of field names to retrieve"
                    }
                },
                "required": ["model", "ids"]
            }
        ),
        Tool(
            name="odoo_search_count",
            description="Count the number of records matching search criteria",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "Optional. Company configuration name from the server .env (stdio/local mode). In HTTP mode credentials come from the X-Odoo-* connection headers and this is ignored."
                    },
                    "model": {
                        "type": "string",
                        "description": "The Odoo model name"
                    },
                    "domain": {
                        "type": "array",
                        "description": "Search domain as a list of criteria",
                        "default": []
                    }
                },
                "required": ["model"]
            }
        ),
        Tool(
            name="odoo_list_models",
            description="Discover available Odoo models (tables). Returns technical name and label. Use a filter to narrow results before querying with other tools.",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "Optional. Company configuration name from the server .env (stdio/local mode). In HTTP mode credentials come from the X-Odoo-* connection headers and this is ignored."
                    },
                    "filter": {
                        "type": "string",
                        "description": "Optional text to match against model technical name or label (case-insensitive). Omit to list all models."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of models to return (default 100)"
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="odoo_fields_get",
            description="Introspect the fields of an Odoo model (name, label, type, relation, required, readonly). Use this to learn what fields exist before building a search or create.",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "Optional. Company configuration name from the server .env (stdio/local mode). In HTTP mode credentials come from the X-Odoo-* connection headers and this is ignored."
                    },
                    "model": {
                        "type": "string",
                        "description": "The Odoo model name to introspect (e.g., 'res.partner')"
                    },
                    "attributes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Field attributes to return from ir.model.fields. Defaults to name, field_description, ttype, relation, required, readonly."
                    }
                },
                "required": ["model"]
            }
        ),
        Tool(
            name="odoo_name_search",
            description="Resolve records by name (e.g., find a partner or product without building a domain). Matches the 'name' field case-insensitively and returns id and display_name. For models without a 'name' field, use odoo_search_read instead.",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "Optional. Company configuration name from the server .env (stdio/local mode). In HTTP mode credentials come from the X-Odoo-* connection headers and this is ignored."
                    },
                    "model": {
                        "type": "string",
                        "description": "The Odoo model name to search (e.g., 'res.partner')"
                    },
                    "name": {
                        "type": "string",
                        "description": "Text to match against the record display name (case-insensitive)"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of matches to return (default 10)"
                    }
                },
                "required": ["model", "name"]
            }
        ),
        Tool(
            name="odoo_call_method",
            description=(
                "Invoke an Odoo business method (workflow action) on a model, e.g. post an "
                "invoice or confirm a sale order. For safety, only methods present in the "
                "allowlist are accepted (configure it with the ODOO_MCP_ALLOWED_METHODS env "
                "var, comma-separated 'model.method' pairs). Counts as a write operation, so "
                "it is blocked when ODOO_MCP_READONLY is enabled."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "Optional. Company configuration name from the server .env (stdio/local mode). In HTTP mode credentials come from the X-Odoo-* connection headers and this is ignored."
                    },
                    "model": {
                        "type": "string",
                        "description": "The Odoo model name (e.g., 'account.move', 'sale.order')"
                    },
                    "method": {
                        "type": "string",
                        "description": "The business method to call (e.g., 'action_post', 'action_confirm')"
                    },
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional list of record IDs the method operates on"
                    },
                    "kwargs": {
                        "type": "object",
                        "description": "Optional keyword arguments to pass to the method"
                    }
                },
                "required": ["model", "method"]
            }
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Handle tool calls"""
    try:
        if name == "odoo_list_companies":
            headers = _request_headers()
            if headers is not None and headers.get(HEADER_URL):
                return [TextContent(
                    type="text",
                    text=(
                        "Current connection instance (from headers):\n"
                        f"  URL: {headers.get(HEADER_URL)}\n"
                        f"  Database: {headers.get(HEADER_DB)}"
                    )
                )]
            companies = list_available_companies()
            return [TextContent(
                type="text",
                text=f"Available companies: {', '.join(companies)}\n\nTotal: {len(companies)}"
            )]

        # All other tools need a resolved Odoo client: credentials come from the
        # X-Odoo-* headers (HTTP transport) or from the 'company' .env section.
        client = resolve_odoo_client(arguments)

        # Read-only kill-switch: block write operations when enabled
        if READ_ONLY and name in ("odoo_create", "odoo_write", "odoo_unlink", "odoo_call_method"):
            return [TextContent(
                type="text",
                text="Error: Server in read-only mode: write operations are disabled (ODOO_MCP_READONLY)"
            )]

        if name == "odoo_search_read":
            result = client.search_read(
                model=arguments["model"],
                domain=arguments.get("domain", []),
                fields=arguments.get("fields"),
                limit=arguments.get("limit"),
                offset=arguments.get("offset"),
                order=arguments.get("order")
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "odoo_create":
            result = client.create(
                model=arguments["model"],
                values=arguments["values"]
            )
            if isinstance(result, list):
                return [TextContent(type="text", text=f"Created {len(result)} records with IDs: {result}")]
            return [TextContent(type="text", text=f"Created record with ID: {result}")]

        elif name == "odoo_write":
            result = client.write(
                model=arguments["model"],
                ids=arguments["ids"],
                values=arguments["values"]
            )
            return [TextContent(type="text", text=f"Updated successfully: {result}")]

        elif name == "odoo_unlink":
            result = client.unlink(
                model=arguments["model"],
                ids=arguments["ids"]
            )
            return [TextContent(type="text", text=f"Deleted successfully: {result}")]

        elif name == "odoo_search":
            result = client.search(
                model=arguments["model"],
                domain=arguments.get("domain", []),
                limit=arguments.get("limit"),
                offset=arguments.get("offset"),
                order=arguments.get("order")
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "odoo_read":
            result = client.read(
                model=arguments["model"],
                ids=arguments["ids"],
                fields=arguments.get("fields")
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "odoo_search_count":
            result = client.search_count(
                model=arguments["model"],
                domain=arguments.get("domain", [])
            )
            return [TextContent(type="text", text=f"Count: {result}")]

        elif name == "odoo_list_models":
            text_filter = arguments.get("filter")
            domain = (
                ["|", ["model", "ilike", text_filter], ["name", "ilike", text_filter]]
                if text_filter else []
            )
            result = client.search_read(
                model="ir.model",
                domain=domain,
                fields=["model", "name"],
                limit=arguments.get("limit", 100),
                order="model asc"
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "odoo_fields_get":
            attributes = arguments.get("attributes") or [
                "name", "field_description", "ttype",
                "relation", "required", "readonly"
            ]
            result = client.search_read(
                model="ir.model.fields",
                domain=[["model", "=", arguments["model"]]],
                fields=attributes,
                order="name asc"
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "odoo_name_search":
            result = client.search_read(
                model=arguments["model"],
                domain=[["name", "ilike", arguments["name"]]],
                fields=["id", "display_name"],
                limit=arguments.get("limit", 10)
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "odoo_call_method":
            model = arguments["model"]
            method = arguments["method"]
            key = f"{model}.{method}"
            if key not in ODOO_ALLOWED_METHODS:
                allowed = ", ".join(sorted(ODOO_ALLOWED_METHODS)) or "(none)"
                return [TextContent(type="text", text=(
                    f"Error: Method '{key}' is not allowed. To enable it, add it to the "
                    f"ODOO_MCP_ALLOWED_METHODS env var (comma-separated 'model.method' pairs). "
                    f"Currently allowed: {allowed}"
                ))]
            result = client.call_method(
                model=model,
                method=method,
                ids=arguments.get("ids"),
                kwargs=arguments.get("kwargs")
            )
            return [TextContent(type="text", text=_format_call_method_result(result))]

        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        logger.error(f"Error executing tool {name}: {e}")
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def run_stdio():
    """Run the MCP server over stdio (local, one process per user)."""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )


def run_http():
    """Run the MCP server over Streamable HTTP (shareable over the network).

    Stateless: each request is independent and credentials arrive per request
    via the X-Odoo-* headers. Serves plain HTTP by default (TLS terminated by a
    reverse proxy) or HTTPS directly when MCP_TLS_CERTFILE/KEYFILE are set.
    """
    import contextlib
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from starlette.responses import JSONResponse, Response
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    session_manager = StreamableHTTPSessionManager(app=app, stateless=True)

    async def handle_mcp(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    async def health(_request):
        return JSONResponse({"status": "ok"})

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with session_manager.run():
            yield

    class GatewayTokenMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path != "/health" and \
                    request.headers.get("x-gateway-token") != MCP_GATEWAY_TOKEN:
                return Response("Unauthorized", status_code=401)
            return await call_next(request)

    middleware = [Middleware(GatewayTokenMiddleware)] if MCP_GATEWAY_TOKEN else []

    starlette_app = Starlette(
        debug=False,
        routes=[
            Route("/health", health, methods=["GET"]),
            Mount(MCP_HTTP_PATH, app=handle_mcp),
        ],
        middleware=middleware,
        lifespan=lifespan,
    )

    ssl_kwargs = {}
    if MCP_TLS_CERTFILE and MCP_TLS_KEYFILE:
        ssl_kwargs = {"ssl_certfile": MCP_TLS_CERTFILE, "ssl_keyfile": MCP_TLS_KEYFILE}

    scheme = "https" if ssl_kwargs else "http"
    logger.info(
        f"HTTP transport listening on {scheme}://{MCP_HTTP_HOST}:{MCP_HTTP_PORT}{MCP_HTTP_PATH} "
        f"(gateway token: {'on' if MCP_GATEWAY_TOKEN else 'off'})"
    )
    uvicorn.run(
        starlette_app,
        host=MCP_HTTP_HOST,
        port=MCP_HTTP_PORT,
        proxy_headers=True,
        forwarded_allow_ips=MCP_FORWARDED_ALLOW_IPS,
        **ssl_kwargs,
    )


def main():
    """Run the MCP server using the configured transport (stdio or http)."""
    logger.info("Starting Odoo MCP Server (Multi-Company Support)")
    logger.info(f"Transport: {MCP_TRANSPORT}")

    try:
        companies = list_available_companies()
        logger.info(f"Loaded {len(companies)} companies: {', '.join(companies)}")
    except Exception as e:
        logger.warning(f"Could not load company configurations on startup: {e}")
        logger.warning("In HTTP mode this is expected: credentials come from request headers")

    if MCP_TRANSPORT == "http":
        run_http()
    else:
        asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
