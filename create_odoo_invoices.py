import json
import os
import sys
from configparser import ConfigParser

import requests

# Credentials come from the environment, or from a section of the .env INI:
#     ODOO_API_KEY=... ODOO_DATABASE=... ODOO_URL=... python create_odoo_invoices.py
#     ODOO_COMPANY=bmya python create_odoo_invoices.py
# Never hardcode a key here: this file is committed.
COMPANY = os.getenv("ODOO_COMPANY")
CONFIG_FILE = os.getenv("ODOO_CONFIG_FILE", ".env")

api_key = os.getenv("ODOO_API_KEY")
database = os.getenv("ODOO_DATABASE")
base_url = os.getenv("ODOO_URL", "http://localhost:8069").rstrip("/")

if COMPANY and not (api_key and database):
    config = ConfigParser()
    config.read(CONFIG_FILE)
    if not config.has_section(COMPANY):
        sys.exit(f"Section [{COMPANY}] not found in {CONFIG_FILE}")
    api_key = api_key or config.get(COMPANY, "ODOO_API_KEY")
    database = database or config.get(COMPANY, "ODOO_DATABASE")
    base_url = config.get(COMPANY, "ODOO_URL", fallback=base_url).rstrip("/")

if not api_key or not database:
    sys.exit(
        "Missing credentials: set ODOO_API_KEY and ODOO_DATABASE (and optionally "
        "ODOO_URL), or ODOO_COMPANY to read them from a .env section."
    )

headers = {
    "Authorization": f"Bearer {api_key}",
    "X-Odoo-Database": database,
}

# Fetch document types
response = requests.post(
    f"{base_url}/json/2/l10n_latam.document.type/search_read",
    headers=headers,
    json={"domain": [], "fields": ["id", "code"], "limit": 100}
)
response.raise_for_status()
data = response.json()
latam_code_to_id = {d["code"]: d["id"] for d in data}

# Fetch uoms
response = requests.post(
    f"{base_url}/json/2/uom.uom/search_read",
    headers=headers,
    json={"domain": [], "fields": ["id", "display_name"], "limit": 100}
)
response.raise_for_status()
data = response.json()
uom_name_to_id = {d["display_name"]: d["id"] for d in data}
default_uom_id = uom_name_to_id.get("Unit")
if not default_uom_id:
    raise ValueError("Default UOM 'Unit' not found")

# Fetch a journal
response = requests.post(
    f"{base_url}/json/2/account.journal/search_read",
    headers=headers,
    json={
        "domain": [["type", "=", "sale"], ["l10n_latam_use_documents", "=", True]],
        "fields": ["id"], "limit": 1
    }
)
response.raise_for_status()
data = response.json()
journal_id = data[0]["id"] if data else None
if not journal_id:
    raise ValueError("No sales journal found")

partner_id = 11

with open('salida.json', 'r', encoding='utf-8') as f:
    libre_dte_list = json.load(f)

for json_libredte in libre_dte_list:
    tipo_dte = json_libredte["Encabezado"]["IdDoc"]["TipoDTE"]
    doc_type_id = latam_code_to_id.get(tipo_dte)
    if not doc_type_id:
        print(f"Skipping unknown tipo_dte: {tipo_dte}")
        continue

    move_type = "out_refund" if tipo_dte in ["61", "112"] else "out_invoice"

    line_vals_list = []
    for det in json_libredte["Detalle"]:
        line_vals = {
            "name": det["NmbItem"],
            "quantity": det["QtyItem"],
            "price_unit": det.get("PrcItem", 0.0),
            "product_uom_id": default_uom_id,
        }
        if "UnmdItem" in det:
            uom_name = det["UnmdItem"]
            if uom_name in uom_name_to_id:
                line_vals["product_uom_id"] = uom_name_to_id[uom_name]
        if det.get("IndExe"):
            line_vals["tax_ids"] = []
        line_vals_list.append([0, 0, line_vals])

    ref_vals_list = []
    for ref in json_libredte.get("Referencia", []):
        ref_doc_type_id = latam_code_to_id.get(ref["TpoDocRef"])
        if not ref_doc_type_id:
            continue
        ref_vals = {
            "l10n_cl_reference_doc_type_id": ref_doc_type_id,
            "origin_doc_number": str(ref["FolioRef"]),
            "reason": ref.get("RazonRef", ""),
        }
        if "CodRef" in ref:
            ref_vals["reference_doc_code"] = ref["CodRef"]
        ref_vals_list.append([0, 0, ref_vals])

    move_vals = {
        "partner_id": partner_id,
        "journal_id": journal_id,
        "move_type": move_type,
        "l10n_latam_document_type_id": doc_type_id,
        "invoice_line_ids": line_vals_list,
        "l10n_cl_reference_ids": ref_vals_list,
    }

    response = requests.post(
        f"{base_url}/json/2/account.move/create",
        headers=headers,
        json={"vals_list": move_vals}
    )
    response.raise_for_status()
    created_id = response.json()
    print(f"Created draft move id: {created_id}")