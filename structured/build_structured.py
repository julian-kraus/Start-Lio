#!/usr/bin/env python3
"""Build source-driven handover artifacts below structured/."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "current"
OUT = ROOT / "structured"
SPEC = "codex-2026-11-21-v1"
REFERENCE = {}

FACT_DEFS = {
    "po_line": ("Purchase-order line", "po_number,po_line", "supplier_id,material_id,qty,unit_price,currency,order_date,requested_date,promised_date,actual_delivery_date,status,incoterm,buyer"),
    "goods_receipt": ("Goods receipt", "gr_number", "po_number,po_line,supplier_id,material_id,qty_ordered,qty_received,gr_date,delivery_note,inspector,remark"),
    "stock_batch": ("Stock snapshot batch", "material_id,batch", "qty,uom,snapshot_date,last_count,site"),
    "packout": ("Packout log", "date,product_id", "units_packed,line"),
    "bom_line": ("Bill-of-material line", "parent_id,child_id", "qty_per,valid_from,valid_to"),
    "component": ("Component master", "material_id", "description,category,uom,critical,drawing_rev"),
    "product": ("Product master", "product_id", "lifecycle,launch_date,plan_weekly_units"),
    "route": ("Transport route", "route_id", "transit_days,mode,origin,destination,via,carrier_id"),
    "carrier": ("Logistics provider", "carrier_id", "name,city,country,modes"),
    "port": ("Port master", "locode", "name,country,lat,lon,mode"),
    "site": ("Site master", "site_id", "name,operator,type,country,city,lat,lon"),
    "supplier": ("Supplier profile", "supplier_id", "name,legal_name,country,currency,incoterm,qualification_status,contact_email,language,notes"),
    "supplier_part": ("Supplier component offer", "supplier_id,material_id", "supplier_part_no,share,unit_price,lead_time_days,moq,primary,note"),
    "shipment": ("Shipment tracking row", "shipment_id", "carrier_id,mode,reference,supplier_id,po_numbers,material_ids,route_id,origin,destination,etd,eta,status,note,updated_at,source_system"),
    "shipment_qty": ("Shipment quantity claim", "reference,material_id", "qty_planned,qty_shipped,qty_balance,balance_eta,po_number"),
    "oc_header": ("Order-confirmation header", "po_number", "oc_number,oc_date,incoterm,payment_terms"),
    "oc_line": ("Order-confirmation line", "po_number,po_line", "item_no_printed,supplier_part_no,material_id,description,qty,unit_price,currency,confirmed_date"),
    "supplier_statement": ("Supplier statement", "statement_no", "kind,value,material_id,revision,condition_to_release"),
    "delivery_note_line": ("Delivery-note line", "delivery_note,line", "po_number,po_numbers,supplier_part_no,material_id,printed_qty,handwritten_qty,struck_through,final_qty,uom,packages,carrier,note_date,received_stamp_date,receiver_signature_present,gross_weight"),
    "inspection_result": ("Inspection result", "gr_number", "material_id,lot_size,sample_size,defects,accept_number,reject_number,batch,decision,blocking_requested,release_requires"),
    "capacity_window": ("Capacity window", "supplier_id,resource", "start,end,capacity_during,unit,scope_note"),
    "offer": ("Supplier offer", "supplier_id,offer_ref,item", "material_id,qty,price_basis,lead_time,valid_from,valid_until,transport_time"),
    "price_change": ("Price change", "supplier_id,material_id", "old_price,new_price,currency,valid_from,reason"),
    "bank_details": ("Bank details", "supplier_id", "iban,bic,account_holder,effective_date,signer,channel"),
    "part_xref": ("Supplier part cross-reference", "supplier_id,supplier_part_no", "material_id"),
    "contract_term": ("Contract term", "supplier_id,contract_id", "incoterm,price_per_material,price_revision_rule,expiry,rush_clause,penalty"),
    "plan_edition": ("Launch edition plan claim", "product_id", "edition_name,demand,preorders,packed,still_to_pack,rate_per_hour,hours_needed,hours_available,line_status"),
    "change_record": ("Field-level change record", "entity", "field,old,new,action,effective_from,change_id,reason,changed_by,use_up_old"),
    "policy_directive": ("Policy directive", "scope", "directive,issued_by,valid_until"),
    "plan_part": ("Launch part plan claim", "material_id", "need,erp_stock,due_before_launch,cover,status"),
    "shift": ("Launch shift", "day", "shift,start,end,hours,lines,note"),
    "x_news_record": ("News item retained as a source fact", "id", "generated_at,published_at,source,headline,summary,regions,tags"),
    "x_workbook_row": ("Unclassified workbook row retained for losslessness", "record_id", "value"),
    "x_document_line": ("Grounded document line", "source_path", "text"),
    "x_context_note": ("Context note", "source_path", "content"),
}

DOC_KINDS = [
    "erp_table", "master_table", "change_record_table", "supplier_profile",
    "supplier_components", "launch_plan_workbook", "launch_status_workbook",
    "shipment_tracker_workbook", "price_list", "delivery_note_scan",
    "order_confirmation", "shipping_advice", "packing_list", "inspection_report",
    "capacity_notice", "offer", "contract", "policy_document", "org_chart",
    "memo", "context_note", "news_feed", "other_document",
]


def norm(value):
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def clean(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def rel(path):
    return path.relative_to(DATA).as_posix()


def num(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else value
    text = str(value).strip().replace("\u00a0", "").replace(" ", "")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        text = text.replace(".", "")
    elif re.fullmatch(r"\d{1,3}(,\d{3})+", text):
        text = text.replace(",", "")
    elif re.fullmatch(r"\d+,\d+", text):
        text = text.replace(",", ".")
    try:
        parsed = Decimal(text)
        return int(parsed) if parsed == parsed.to_integral() else float(parsed)
    except (InvalidOperation, ValueError):
        return value


def as_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d.%m.%Y", "%Y.%m.%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return text


def supplier_id(value):
    text = str(value or "").strip()
    match = re.search(r"SUP[-_ ]?(\d+)", text, re.I)
    if match:
        return f"SUP-{int(match.group(1)):04d}"
    if text.isdigit():
        return f"SUP-{int(text):04d}"
    return text


def resolved_supplier(value):
    text = clean(value)
    if not text:
        return text
    direct = supplier_id(text)
    if direct != text or direct.startswith("SUP-"):
        return direct
    for profile in sorted((DATA / "suppliers").glob("*/profile.csv")):
        for row in csv_rows(profile):
            if str(row_get(row, "name") or "").casefold() == str(text).casefold():
                return supplier_id(row_get(row, "supplier_id") or profile.parent.name.split("_")[0])
    return text


def resolved_carrier(value):
    text = clean(value)
    if not text:
        return text
    if text.startswith("LSP-"):
        return text
    path = DATA / "soony" / "master" / "logistics_providers.csv"
    if path.is_file():
        for row in csv_rows(path):
            if str(row_get(row, "name") or "").casefold() == str(text).casefold():
                return row_get(row, "carrier_id") or text
    return text


def fact(ft, subject, attribute, value, source, evidence, *, locator=None, unit=None,
         as_of=None, confidence="high", note=None, subject_raw=None):
    result = {
        "fact_type": ft,
        "subject": subject or {},
        "subject_raw": subject_raw if subject_raw is not None else subject or {},
        "attribute": attribute,
        "value": value,
        "unit": unit,
        "as_of": as_of,
        "locator": locator or {},
        "evidence": str(evidence if evidence is not None else ""),
        "confidence": confidence,
    }
    if note:
        result["note"] = note
    return result


def envelope(path, kind, facts, *, languages=None, unreadable=None, proposed=None):
    data = path.read_bytes()
    return {
        "source": {"path": rel(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)},
        "doc_kind": kind,
        "doc_kind_confidence": "high",
        "languages": languages or ["en"],
        "spec_version": SPEC,
        "extracted_by": "codex:deterministic+source-envelope",
        "doc_date": None,
        "facts": facts,
        "unreadable": unreadable or [],
        "proposed_fact_types": proposed or [],
    }


def csv_rows(path):
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    if "\x00" in text:
        text = raw.decode("cp1252", errors="replace")
    try:
        delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ";" if text.count(";") > text.count(",") else ","
    return list(csv.DictReader(text.splitlines(), delimiter=delimiter))


def row_get(row, *names):
    values = {norm(k): clean(v) for k, v in row.items()}
    for name in names:
        if norm(name) in values:
            return values[norm(name)]
    return None


def map_signature(headers, sheet=None):
    names = sorted(str(x).strip().lower() for x in headers if x is not None and str(x).strip())
    text = "|".join(names)
    if sheet is not None:
        text += "|sheet:" + str(sheet).strip().lower()
    return hashlib.sha256(text.encode()).hexdigest()


def write_map(headers, *, fmt, sheet, fact_type, subject, path, field_map, as_of=None, header_detection=None):
    columns = {}
    for header in headers:
        if not str(header).strip():
            continue
        field = field_map.get(norm(header), norm(header) or "value")
        spec = {"field": field, "type": "text", "date_format": None, "decimal_sep": None, "thousands_sep": None, "list_sep": None, "id_template": None, "enum": None, "unit": None}
        if field in {"po_numbers", "material_ids", "regions", "tags", "modes"}:
            spec["type"] = "list"
            spec["list_sep"] = "|"
        columns[str(header)] = spec
    mapping = {
        "signature": map_signature(headers, sheet if fmt == "xlsx" else None),
        "example_path": path,
        "format": fmt,
        "sheet": sheet if fmt == "xlsx" else None,
        "fact_type": fact_type,
        "subject": subject,
        "columns": columns,
    }
    if as_of:
        mapping["as_of"] = as_of
    if header_detection:
        mapping["header_detection"] = header_detection
    (OUT / "schema_maps").mkdir(parents=True, exist_ok=True)
    (OUT / "schema_maps" / f"{mapping['signature']}.json").write_text(
        json.dumps(mapping, indent=2, ensure_ascii=False) + "\n"
    )


def field_for(header, fact_type):
    key = norm(header)
    common = {
        "component_id": "material_id", "material": "material_id", "part": "material_id",
        "part_number": "material_id", "supplier": "supplier_id", "vendor": "supplier_id",
        "carrier": "carrier_id", "container": "reference", "container_awb": "reference",
        "awb": "reference", "quantity": "qty", "units": "qty", "price": "unit_price",
        "gr": "gr_number", "receipt": "gr_number", "po": "po_number", "item": "po_line",
        "line": "po_line", "route": "route_id", "locode": "locode", "site": "site_id",
        "parent": "parent_id", "child": "child_id", "qty_per_old": "old",
        "qty_per_new": "new", "rev_old": "old", "rev_new": "new", "edition": "edition_name",
        "product": "product_id", "name": "name", "day_one_demand": "demand",
        "of_which_pre_orders": "preorders", "still_to_pack": "still_to_pack",
        "needed_for_the_remaining_pack_out": "need", "erp_stock": "erp_stock",
        "due_before_launch": "due_before_launch", "cover": "cover", "reference": "reference",
        "comment": "note", "shipment": "shipment_id", "from": "origin", "to": "destination",
        "supplier_part": "supplier_part_no", "supplier_part_no": "supplier_part_no",
        "record_id": "record_id", "id": "id",
    }
    return common.get(key, key)


def csv_kind(path):
    stem = path.stem.lower()
    if any(x in stem for x in ("goods_receipts", "open_shipments", "purchase_orders", "stock_snapshot", "packout")):
        return "erp_table"
    if "delta" in stem or "update" in stem:
        return "change_record_table"
    if path.name == "profile.csv":
        return "supplier_profile"
    if path.name == "components.csv" and path.parent.name.startswith("SUP-"):
        return "supplier_components"
    return "master_table"


def csv_fact_type(path):
    stem = path.stem.lower()
    if "goods_receipts" in stem:
        return "goods_receipt"
    if "open_shipments" in stem:
        return "shipment"
    if "packout" in stem:
        return "packout"
    if "purchase_orders" in stem:
        return "po_line"
    if "stock_snapshot" in stem:
        return "stock_batch"
    if any(x in stem for x in ("bom_delta", "components_update", "components_delta")):
        return "change_record"
    if stem == "bom":
        return "bom_line"
    if path.name == "profile.csv":
        return "supplier"
    if path.name == "components.csv" and path.parent.name.startswith("SUP-"):
        return "supplier_part"
    if stem == "components":
        return "component"
    if stem == "products":
        return "product"
    if stem == "logistics_providers":
        return "carrier"
    if stem == "ports":
        return "port"
    if stem == "routes":
        return "route"
    if stem == "sites":
        return "site"
    return "x_workbook_row"


def csv_subject(ft, row, path):
    if ft == "po_line":
        return {"po_number": row_get(row, "po_number", "EBELN"), "po_line": num(row_get(row, "po_line", "EBELP"))}
    if ft == "goods_receipt":
        return {"gr_number": row_get(row, "gr_number", "receipt_id")}
    if ft == "stock_batch":
        return {"material_id": row_get(row, "material_id", "material"), "batch": row_get(row, "batch")}
    if ft == "packout":
        return {"date": as_date(row_get(row, "date")), "product_id": row_get(row, "product_id")}
    if ft == "bom_line":
        return {"parent_id": row_get(row, "parent_id"), "child_id": row_get(row, "child_id")}
    if ft == "component":
        return {"material_id": row_get(row, "component_id", "material_id", "part_number")}
    if ft == "product":
        return {"product_id": row_get(row, "product_id")}
    if ft == "carrier":
        return {"carrier_id": row_get(row, "carrier_id")}
    if ft == "port":
        return {"locode": row_get(row, "locode", "id")}
    if ft == "route":
        return {"route_id": row_get(row, "route_id", "id")}
    if ft == "site":
        return {"site_id": row_get(row, "site_id", "id")}
    if ft == "supplier":
        return {"supplier_id": supplier_id(row_get(row, "supplier_id") or path.parent.name.split("_")[0])}
    if ft == "supplier_part":
        sid = supplier_id(row_get(row, "supplier_id") or path.parent.name.split("_")[0])
        part = row_get(row, "component_id", "supplier_part_no", "material_id")
        return {"supplier_id": sid, "material_id": part}
    if ft == "shipment":
        return {"shipment_id": row_get(row, "shipment_id")}
    if ft == "change_record":
        if "bom_delta" in path.stem.lower():
            parent = row_get(row, "parent_id")
            child = row_get(row, "child_id")
            return {"entity": f"{parent}:{child}", "parent_id": parent, "child_id": child, "change_id": row_get(row, "change_id")}
        material = row_get(row, "component_id", "material_id")
        field = row_get(row, "field") or row_get(row, "action")
        return {"entity": material, "material_id": material, "field": field}
    return {"record_id": row_get(row, "id", "record_id", "date", "material_id", "component_id") or path.stem}


def csv_facts(path, rows):
    ft = csv_fact_type(path)
    kind = csv_kind(path)
    headers = list(rows[0].keys()) if rows else []
    field_map = {header: field_for(header, ft) for header in headers}
    if ft == "po_line" and "EBELN" in headers:
        field_map.update({"EBELN": "po_number", "EBELP": "po_line", "LIFNR": "supplier_id", "MATNR": "material_id", "MENGE": "qty", "NETPR": "unit_price", "BEDAT": "order_date", "EINDT": "requested_date", "ZZCONF": "promised_date", "WEDAT": "actual_delivery_date", "ZZSTAT": "status", "ERNAM": "buyer"})
    subject = ["po_number", "po_line"] if ft == "po_line" else list(csv_subject(ft, rows[0] if rows else {}, path))
    write_map(headers, fmt="csv", sheet=None, fact_type=ft, subject=subject, path=rel(path), field_map={norm(k): v for k, v in field_map.items()})
    facts = []
    for row_number, row in enumerate(rows, 2):
        sub = csv_subject(ft, row, path)
        if any(value in (None, "") for value in sub.values()):
            continue
        for header, source_value in row.items():
            value = clean(source_value)
            if value is None:
                continue
            attribute = field_map.get(header, field_for(header, ft))
            if attribute == "supplier_id":
                value = supplier_id(value)
            elif attribute in {"qty", "qty_ordered", "qty_received", "quantity", "qty_per", "unit_price", "share", "lead_time_days", "moq", "transit_days", "lat", "lon", "units_packed", "weekly_plan_qty", "day_one_qty", "std_cost_eur", "last_count"}:
                value = num(value)
            elif attribute == "po_line":
                value = num(value)
            elif attribute.endswith("_date") or attribute in {"date", "etd", "eta", "updated_at", "valid_from", "valid_to", "effective_from", "snapshot_date"}:
                value = as_date(value)
            if ft == "change_record":
                if header in {"qty_per_old", "rev_old", "lead_time_days_old"}:
                    attribute = "old"
                elif header in {"qty_per_new", "rev_new", "lead_time_days_new"}:
                    attribute = "new"
                elif header == "use_up_old_rev":
                    attribute = "use_up_old"
            fact_subject = sub
            if ft == "change_record" and header in {"qty_per_old", "qty_per_new", "rev_old", "rev_new", "use_up_old_rev"}:
                change_field = "qty_per" if header.startswith("qty_") else "drawing_rev"
                fact_subject = {**sub, "change_field": change_field}
            facts.append(fact(ft, fact_subject, attribute, value, path, f"{header}={source_value}", locator={"row": row_number, "column": header}))
    return kind, facts


def pdf_text(path):
    try:
        from pypdf import PdfReader
        return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    except Exception:
        return ""


def pdf_kind(path, text):
    probe = (str(path) + " " + text[:700]).lower()
    if "inspection" in probe:
        return "inspection_report"
    if "shipping advice" in probe or path.name.upper().startswith("SA_"):
        return "shipping_advice"
    if "packing list" in probe or path.name.upper().startswith("PL_"):
        return "packing_list"
    if "order confirmation" in probe or "/order_confirmations/" in str(path):
        return "order_confirmation"
    if "contract" in probe or "/contracts/" in str(path):
        return "contract"
    if "org chart" in probe or "org_chart" in path.name.lower():
        return "org_chart"
    if "policy" in probe or "approval" in probe:
        return "policy_document"
    if "capacity" in probe or "downtime" in probe:
        return "capacity_notice"
    if "offer" in probe or "quotation" in probe:
        return "offer"
    return "other_document"


def parse_inspection(path, text):
    def match(pattern):
        found = re.search(pattern, text, re.I)
        return found.group(1).strip() if found else None
    gr = match(r"(?:GR|goods receipt|receipt)\s*[:#]?\s*([A-Z0-9-]+)")
    material = match(r"\b((?:CMP|PR|PRM)-[A-Z0-9-]+)\b")
    after_part = text[text.find(material) + len(material):] if material and material in text else text
    table_values = re.search(r"\n\s*([\d.,]+)\s*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)\s*\n\s*(\d+)\s*/\s*(\d+)", after_part)
    quantities = table_values.groups()[:3] if table_values else re.findall(r"(?<![A-Za-z])(?:\d{1,3}(?:[,. ]\d{3})+|\d{1,7})(?![A-Za-z])", after_part)
    acceptance = table_values if table_values else re.search(r"\b(\d+)\s*/\s*(\d+)\b", text)
    fields = {
        "material_id": material,
        "lot_size": num(quantities[0]) if len(quantities) > 0 else None,
        "sample_size": num(quantities[1]) if len(quantities) > 1 else None,
        "defects": num(quantities[2]) if len(quantities) > 2 else None,
        "accept_number": num(acceptance.group(4 if table_values else 1)) if acceptance else None,
        "reject_number": num(acceptance.group(5 if table_values else 2)) if acceptance else None,
        "batch": match(r"batch\s*[:#]?\s*([A-Z0-9-]+)"),
        "decision": "fail" if re.search(r"does not meet|reject|fail", text, re.I) else "pass",
        "blocking_requested": bool(re.search(r"blocking requested", text, re.I)),
        "release_requires": "Supplier Quality + Category Lead" if re.search(r"Supplier Quality.*Category Lead", text, re.I | re.S) else None,
    }
    return [fact("inspection_result", {"gr_number": gr or path.stem}, key, value, path, text[:1200], locator={"page": 1, "section": "inspection"}) for key, value in fields.items() if value is not None]


def parse_contract(path, text):
    found = re.findall(r"SUP-\d{4}", rel(path), re.I)
    supplier = supplier_id(found[0] if found else path.parent.parent.name.split("_")[0])
    contract_match = re.search(r"(?:contract|agreement)\s*(?:no\.?|number)?\s*[:#]?\s*([A-Z]{2,5}[- ]?\d{3,}[- ]?\d{2,})", text, re.I)
    contract_id = contract_match.group(1).replace(" ", "-") if contract_match else "doc" + hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    fallback_note = None if contract_match else "No contract identifier was extractable; doc plus sha256 prefix used."
    subject = {"supplier_id": supplier, "contract_id": contract_id}
    facts = []
    for incoterm in sorted(set(re.findall(r"\b(EXW|FCA|FOB|CIF|DAP|DDP)\b", text, re.I))):
        facts.append(fact("contract_term", subject, "incoterm", incoterm.upper(), path, incoterm, locator={"page": 1}, note=fallback_note))
    # pypdf retains the Annex C table in reading order: Soony part followed by
    # that row's supplier part.  Match that pair directly rather than adjacent
    # supplier/Soony tokens, which would shift every cross-reference one row.
    annex_match = re.search(r"Annex C\s*[–-]\s*Part number cross-reference(?P<table>.*)", text, re.I | re.S)
    annex = annex_match.group("table") if annex_match else ""
    for match in re.finditer(r"\b((?:CMP|PRM)-[A-Z0-9-]+)\b\s+\b((?:PR|RM)-[A-Z0-9-]+)\b", annex):
        material_id, supplier_part = match.groups()
        facts.append(fact("part_xref", {"supplier_id": supplier, "supplier_part_no": supplier_part}, "material_id", material_id, path, match.group(0), locator={"page": 1, "section": "Annex C"}))
    for label, pattern in (("expiry", r"(?:expires|expiry)\s*[: ]\s*([0-9./-]+)"), ("penalty", r"(?:late delivery|penalty)[: ]+([^.\n]+)")):
        value = re.search(pattern, text, re.I)
        if value:
            facts.append(fact("contract_term", subject, label, value.group(1).strip(), path, value.group(0), locator={"page": 1}, note=fallback_note))
    return facts


def classify_statement(text):
    folded = text.casefold()
    if ("packed" in folded and ("wait" in folded or "buyer" in folded)) or "on hold" in folded or "suspend" in folded:
        return "on_hold"
    if "term" in folded and ("reject" in folded or "declin" in folded):
        return "term_rejected"
    if "short" in folded or "fewer" in folded or "less than ordered" in folded:
        return "short_shipment"
    if "artwork" in folded or "rev." in folded or "revision" in folded:
        return "artwork_rev"
    if "approval" in folded or "approved" in folded or "certif" in folded or "qualification" in folded:
        return "approval_claim"
    if "dispatch" in folded or "ship" in folded or "delivery" in folded or "carrier" in folded or "flight" in folded:
        return "dispatch_info"
    if "payment" in folded or "price" in folded:
        return "price_deviation"
    return "other"


def known_currency_codes():
    values = set()
    for source in sorted((DATA / "soony" / "erp").glob("*.csv")):
        for row in csv_rows(source):
            value = row_get(row, "currency")
            if value:
                values.add(str(value).upper())
    return values


def parse_shipping_advice(path, text):
    po_match = re.search(r"Your order\s+(\d{8,})", text, re.I)
    po_number = po_match.group(1) if po_match else None
    reference_match = re.search(r"\bAWB\s*[:#]?\s*(\d{3}-\d{8})\b", text, re.I)
    reference = reference_match.group(1) if reference_match else next(iter(re.findall(r"\b\d{3}-\d{8}\b", text)), None)
    material_match = re.search(r"\b((?:CMP|PR|PRM)-[A-Z0-9-]+)\b", text)
    material_id = material_match.group(1) if material_match else None
    labels = re.search(r"(?:Ordered|발주수량).*?(?:Shipped|출하수량).*?(?:Balance|잔량)", text, re.I | re.S)
    table_tail = text[labels.end():] if labels else text
    table_tail = table_tail.split("AWB", 1)[0]
    quantities = re.findall(r"(?<![A-Za-z])(?:\d{1,3}(?:[,. ]\d{3})+|\d{4,})(?![A-Za-z])", table_tail)
    quantities = [num(value) for value in quantities[:3]]
    while len(quantities) < 3:
        quantities.append(None)
    eta_match = re.search(r"delivery\s+to\s+[^\n]*?\b(\d{4}-\d{2}-\d{2})\b", text, re.I)
    if not eta_match:
        eta_match = re.search(r"next available flight[^\n]*?\b(\d{4}-\d{2}-\d{2})\b", text, re.I)
    subject = {"reference": reference, "material_id": material_id}
    facts = []
    for attribute, value in (("qty_planned", quantities[0]), ("qty_shipped", quantities[1]), ("qty_balance", quantities[2]), ("balance_eta", eta_match.group(1) if eta_match else None), ("po_number", po_number), ("reference", reference)):
        if value is not None:
            facts.append(fact("shipment_qty", subject, attribute, value, path, f"{attribute}: {value}", locator={"page": 1, "section": "shipping advice table"}))
    statement_text = " ".join(line.strip() for line in text.splitlines() if line.strip() and ("ULD" in line or "offloaded" in line.lower() or "balance" in line.lower()))
    if statement_text:
        statement_subject = {"statement_no": 1, "po_number": po_number, "reference": reference, "material_id": material_id}
        facts.append(fact("supplier_statement", statement_subject, "kind", "short_shipment", path, statement_text, locator={"page": 1, "section": "remarks"}))
        facts.append(fact("supplier_statement", statement_subject, "value", statement_text, path, statement_text, locator={"page": 1, "section": "remarks"}))
    return facts


def parse_order_like(path, text, kind):
    order_match = re.search(r"(?:Order Confirmation|OA)\s*[:#]?\s*(OA\d+)", text, re.I)
    oc_number = order_match.group(1) if order_match else next(iter(re.findall(r"\bOA\d+\b", path.name)), None)
    po_match = re.search(r"Your order\s+(\d{8,})", text, re.I)
    po_number = po_match.group(1) if po_match else next(iter(re.findall(r"\b45\d{8}\b", text)), None)
    order_header = re.search(r"Your order\s+\d{8,}\s+of\s+(\d{4}-\d{2}-\d{2})", text, re.I)
    order_date = order_header.group(1) if order_header else None
    incoterm_match = re.search(r"(?:Incoterms?|Delivery terms)\s*[: ]\s*([^\n|]+)", text, re.I)
    incoterm = re.split(r"\s*(?:\||·|\bPayment\b)\s*", incoterm_match.group(1).strip(), maxsplit=1, flags=re.I)[0] if incoterm_match else None
    payment = re.search(r"Payment\s*[: ]\s*([^\n|]+)", text, re.I)
    payment_terms = payment.group(1).strip() if payment else None
    facts = []
    header_subject = {"po_number": po_number or oc_number}
    for attr, value in (("oc_number", oc_number), ("oc_date", order_date), ("incoterm", incoterm), ("payment_terms", payment_terms)):
        if value:
            facts.append(fact("oc_header", header_subject, attr, value, path, value, locator={"page": 1, "section": "header"}))
    material_re = re.compile(r"\b((?:CMP|PR|PRM)-[A-Z0-9-]+)\b")
    # Item facts may only come from the item table.  Remarks can repeat a
    # supplier part number (for example an artwork revision) after the table.
    # Treating that repeat as a row duplicates the preceding item number.
    remark_start = re.search(r"(?im)^\s*(?:artwork|remarks?|notes?)\b", text)
    item_region_end = remark_start.start() if remark_start else len(text)
    matches = []
    for match in material_re.finditer(text):
        if match.start() >= item_region_end:
            continue
        prefix = text[max(0, match.start() - 200):match.start()]
        item_numbers = re.findall(r"(?:^|\n)\s*(\d{5})\s*(?:\n|$)", prefix)
        if item_numbers:
            matches.append((match, item_numbers[-1]))
    currency_codes = known_currency_codes()
    for index, (match, item_printed) in enumerate(matches):
        supplier_part = match.group(1)
        end = matches[index + 1][0].start() if index + 1 < len(matches) else min(len(text), match.end() + 700)
        block = text[match.end():end]
        date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", block)
        row_block = block[:date_match.start()] if date_match else block
        numbers = re.findall(r"(?<![A-Za-z])(?:\d{1,3}(?:[,. ]\d{3})+|\d{4,})(?![A-Za-z])", row_block)
        quantity = num(numbers[0]) if numbers else None
        price_candidates = [candidate for candidate in re.findall(r"(?<![A-Za-z])\d+(?:[.,]\d+)(?![A-Za-z])", row_block) if not re.fullmatch(r"\d{1,3}[,.]\d{3}(?:[,.]\d{3})*", candidate)]
        price = num(price_candidates[-1]) if price_candidates else None
        currency = next((code for code in re.findall(r"\b[A-Z]{3}\b", row_block) if code.upper() in currency_codes), None)
        po_line = int(item_printed) // 10
        subject = {"po_number": po_number or oc_number, "po_line": po_line, "item_key": hashlib.sha1(f"{item_printed}|{supplier_part}|{match.start()}".encode()).hexdigest()[:12]}
        attrs = {"item_no_printed": item_printed, "supplier_part_no": supplier_part, "qty": quantity, "unit_price": price, "currency": currency, "confirmed_date": date_match.group(1) if date_match else None}
        if supplier_part.startswith("CMP-"):
            attrs["material_id"] = supplier_part
        for attr, value in attrs.items():
            if value is not None:
                facts.append(fact("oc_line", subject, attr, value, path, (supplier_part + row_block[:320]).strip(), locator={"page": 1, "line": text[:match.start()].count("\n") + 1}))
    statement_no = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or re.search(r"^(item|material|description|qty|unit price|confirmed)\b", stripped, re.I):
            continue
        if re.search(r"remark|note|on hold|short shipment|artwork|approved|dispatch|certif|qualification", stripped, re.I):
            statement_no += 1
            named_part = re.search(r"\b((?:CMP|PR|PRM)-[A-Z0-9-]+)\b", stripped)
            subject = {"statement_no": statement_no, "po_number": po_number or oc_number}
            if named_part:
                subject["material_id"] = named_part.group(1)
            kind_value = classify_statement(stripped)
            facts.append(fact("supplier_statement", subject, "kind", kind_value, path, stripped, locator={"page": 1}))
            facts.append(fact("supplier_statement", subject, "value", stripped, path, stripped, locator={"page": 1}))
            revision = re.search(r"(?:rev\.?|revision)\s*([A-Z0-9]+)", stripped, re.I)
            if revision:
                facts.append(fact("supplier_statement", subject, "revision", revision.group(1), path, stripped, locator={"page": 1}))
    return facts


def parse_pdf(path, text, kind):
    if kind == "inspection_report":
        return parse_inspection(path, text)
    if kind == "contract":
        return parse_contract(path, text)
    if kind == "shipping_advice":
        return parse_shipping_advice(path, text)
    if kind == "packing_list":
        return [fact("x_document_line", {"source_path": rel(path), "line_key": hashlib.sha1(f"{line}|{line_number}".encode()).hexdigest()[:12]}, "text", line, path, line, locator={"page": 1, "line": line_number}) for line_number, line in enumerate(text.splitlines(), 1) if line.strip()]
    if kind == "order_confirmation":
        return parse_order_like(path, text, kind)
    facts = []
    fact_type = "policy_directive" if kind == "policy_document" else "x_document_line"
    policy_match = re.search(r"\b(PP[- ]\d+)\b", text, re.I) if fact_type == "policy_directive" else None
    scope = policy_match.group(1).replace(" ", "-").upper() if policy_match else "doc" + hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    fallback_note = None if policy_match else "No document identifier was extractable; doc plus sha256 prefix used."
    for line_number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if line:
            attribute = "text" if fact_type == "x_document_line" else "directive"
            subject = {"source_path": rel(path), "line_key": hashlib.sha1(f"{line}|{line_number}".encode()).hexdigest()[:12]} if fact_type == "x_document_line" else {"scope": scope, "directive_key": hashlib.sha1(f"{line}|{line_number}".encode()).hexdigest()[:12]}
            facts.append(fact(fact_type, subject, attribute, line, path, line, locator={"page": 1, "line": line_number}, note=fallback_note))
    return facts


def copy_reference(path):
    old = REFERENCE.get(rel(path))
    return json.loads(json.dumps(old.get("facts", []))) if old else []


def normalize_reference(path, facts):
    result = []
    statement_no = 0
    delivery_note = next((item.get("subject", {}).get("delivery_note") for item in facts if item.get("fact_type") == "delivery_note_line"), None)
    reference = next((item.get("subject", {}).get("reference") for item in facts if item.get("fact_type") == "shipment_qty" and item.get("subject", {}).get("reference") != path.stem), None)
    po_number = next((item.get("subject", {}).get("po_number") for item in facts if item.get("subject", {}).get("po_number")), None)
    fallback_id = "doc" + hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    for original in facts:
        item = dict(original)
        item["locator"] = dict(item.get("locator") or {})
        item["locator"]["region_hint"] = item["locator"].get("region_hint") or "paper field"
        if item.get("fact_type") == "delivery_note_line":
            line = item.get("subject", {}).get("line")
            item["locator"]["region_hint"] = f"line {line} field"
            if item.get("attribute") in {"printed_qty", "handwritten_qty", "final_qty", "struck_through"} and item.get("note"):
                item["evidence"] = item["note"]
            if item.get("attribute") == "po_number" and isinstance(item.get("value"), list):
                result.append(fact("delivery_note_line", item.get("subject", {}), "po_numbers", item["value"], path, item.get("evidence"), locator=item["locator"], confidence=item.get("confidence", "medium")))
                item["value"] = None
        if item.get("fact_type") == "supplier_statement":
            if item.get("attribute") != "value":
                continue
            statement_no += 1
            original_subject = original.get("subject", {})
            item_subject = {"statement_no": statement_no}
            if delivery_note:
                item_subject["delivery_note"] = delivery_note
            elif reference:
                item_subject["reference"] = reference
            elif original_subject.get("po_number") or po_number:
                item_subject["po_number"] = original_subject.get("po_number") or po_number
            material = original_subject.get("material_id")
            if material:
                item_subject["material_id"] = material
            item["subject"] = item_subject
            item["subject_raw"] = dict(item["subject"])
            item["locator"]["region_hint"] = "handling note"
            item["note"] = "Original-language handling note preserved from the reference envelope."
            kind = "handling_note" if delivery_note else classify_statement(str(item.get("value") or item.get("evidence") or ""))
            result.append(fact("supplier_statement", item["subject"], "kind", kind, path, item.get("evidence"), locator=item["locator"], confidence=item.get("confidence", "medium")))
        if item.get("fact_type") == "contract_term":
            item["subject"] = {**item.get("subject", {}), "contract_id": fallback_id}
            item["subject_raw"] = dict(item["subject"])
            item["note"] = "Reference envelope had no extractable contract number; doc plus sha256 prefix used."
        if item.get("fact_type") == "shipment_qty" and item.get("subject", {}).get("reference") == path.stem and reference:
            item["subject"] = {**item["subject"], "reference": reference}
            item["subject_raw"] = dict(item["subject"])
        result.append(item)
    return result


def resolve_scan_parts(facts, xrefs):
    for item in facts:
        if item.get("fact_type") != "delivery_note_line":
            continue
        subject = item.get("subject", {})
        if item.get("attribute") == "material_id" and not item.get("value"):
            part = next((f.get("value") for f in facts if f.get("subject") == subject and f.get("attribute") == "supplier_part_no"), None)
            if part and part in xrefs:
                item["value"] = xrefs[part]
            elif part:
                item["note"] = "No contract part cross-reference available; material_id remains null."
        if item.get("attribute") == "po_number" and isinstance(item.get("value"), list):
            item["value"] = None


def resolve_order_parts(environments, xrefs):
    for env in environments:
        if env.get("doc_kind") != "order_confirmation":
            continue
        by_subject = defaultdict(dict)
        for item in env.get("facts", []):
            if item.get("fact_type") == "oc_line":
                by_subject[json.dumps(item.get("subject", {}), sort_keys=True)][item.get("attribute")] = item.get("value")
        for subject_text, values in by_subject.items():
            part = values.get("supplier_part_no")
            if part and not values.get("material_id") and part in xrefs:
                subject = json.loads(subject_text)
                env["facts"].append(fact("oc_line", subject, "material_id", xrefs[part], Path(DATA / env["source"]["path"]), part, locator={"page": 1, "section": "part cross-reference"}, confidence="medium", note="Resolved through the supplier contract part cross-reference."))


def resolve_statement_parts(environments, xrefs):
    for env in environments:
        for item in env.get("facts", []):
            if item.get("fact_type") != "supplier_statement":
                continue
            subject = item.get("subject", {})
            material = subject.get("material_id")
            if not material:
                statement_text = str(item.get("value") or item.get("evidence") or "")
                named = re.search(r"\b((?:CMP|PR|PRM|RM)-[A-Z0-9-]+)\b", statement_text)
                material = named.group(1) if named else None
            if material in xrefs:
                material = xrefs[material]
            if material and material.startswith(("CMP-", "PRM-")):
                subject["material_id"] = material
                item["subject_raw"] = dict(subject)


def xlsx_rows(path):
    from openpyxl import load_workbook
    workbook = load_workbook(path, read_only=True, data_only=True)
    tables = []
    for sheet in workbook.worksheets:
        rows = []
        for row_number, row in enumerate(sheet.iter_rows(values_only=True), 1):
            values = [clean(value) for value in row]
            while values and values[-1] is None:
                values.pop()
            if values:
                rows.append((row_number, values))
        if rows:
            tables.append((sheet.title, rows))
    workbook.close()
    return tables


def xlsx_header(rows):
    candidates = []
    for index, (row_number, values) in enumerate(rows[:15]):
        text_count = sum(isinstance(x, str) and bool(x.strip()) for x in values)
        nonempty = sum(x not in (None, "") for x in values)
        if text_count >= 2 and nonempty >= 2:
            candidates.append((nonempty, text_count, index, row_number))
    return max(candidates, key=lambda x: (x[0], x[1], -x[2]))[2] if candidates else 0


def workbook_kind(path):
    name = path.name.lower()
    if "launch_plan" in name:
        return "launch_plan_workbook"
    if "launch_status" in name:
        return "launch_status_workbook"
    if "tracker" in name:
        return "shipment_tracker_workbook"
    return "price_list"


def workbook_row_kind(path, sheet, headers):
    lowered = {norm(x) for x in headers}
    if workbook_kind(path) == "shipment_tracker_workbook":
        return "shipment", ["shipment_id"]
    if norm(sheet) == "editions" or ("product" in lowered and "day_one_demand" in lowered):
        return "plan_edition", ["product_id"]
    if norm(sheet) == "parts" or "needed_for_the_remaining_pack_out" in lowered:
        return "plan_part", ["material_id"]
    if norm(sheet) == "inbound":
        return "shipment_qty", ["reference", "material_id"]
    if norm(sheet) == "shifts":
        return "shift", ["day"]
    if workbook_kind(path) == "price_list":
        return "price_change", ["material_id"]
    return "x_workbook_row", ["record_id"]


def workbook_facts(path):
    allfacts = []
    for sheet, rows in xlsx_rows(path):
        header_index = xlsx_header(rows)
        source_row, raw_headers = rows[header_index]
        headers = [str(value) if value is not None else "" for value in raw_headers]
        ft, subject_fields = workbook_row_kind(path, sheet, headers)
        field_map = {norm(header): field_for(header, ft) for header in headers if header}
        if ft == "shipment":
            field_map.update({"shipment": "shipment_id", "container_awb": "reference", "pos": "po_numbers"})
        if ft == "shipment_qty":
            field_map.update({"quantity": "qty_planned", "part": "material_id"})
        if ft == "price_change":
            field_map.update({"part": "material_id"})
        title = " ".join(str(x) for _, row in rows[:header_index] for x in row if x is not None)
        as_of = None
        timestamp = re.search(r"\b(\d{4}-\d{2}-\d{2})(?:\s+(\d{1,2}:\d{2}))?", title)
        if timestamp:
            as_of = {"value": "T".join(x for x in timestamp.groups() if x) if timestamp.group(2) else timestamp.group(1)}
        write_map(headers, fmt="xlsx", sheet=sheet, fact_type=ft, subject=subject_fields, path=rel(path), field_map=field_map, as_of=as_of, header_detection={"row": source_row, "title_rows": [x[0] for x in rows[:header_index]]})
        for row_number, row in rows[header_index + 1:]:
            values = {}
            for index, header in enumerate(headers):
                if header:
                    value = clean(row[index] if index < len(row) else None)
                    if value is not None:
                        values[field_map.get(norm(header), norm(header))] = value
            if not values:
                continue
            if ft in {"shipment", "plan_edition", "plan_part", "shift"}:
                subject = {"shipment_id": values.get("shipment_id")} if ft == "shipment" else {"product_id": values.get("product_id")} if ft == "plan_edition" else {"material_id": values.get("material_id")} if ft == "plan_part" else {"day": values.get("day")}
            elif ft == "shipment_qty":
                subject = {"reference": values.get("reference"), "material_id": values.get("material_id")}
            elif ft == "price_change":
                sid = supplier_id(next((part for part in path.parts if part.startswith("SUP-")), path.stem))
                subject = {"supplier_id": sid, "material_id": values.get("material_id")}
            else:
                record_value = values.get("record_id") or values.get("value") or json.dumps(values, sort_keys=True, default=str)
                subject = {"record_id": hashlib.sha1(str(record_value).encode()).hexdigest()[:12]}
            if any(value in (None, "") for value in subject.values()):
                continue
            for attribute, value in values.items():
                if attribute in subject:
                    continue
                converted = num(value) if attribute in {"qty", "qty_planned", "qty_shipped", "qty_balance", "demand", "preorders", "packed", "still_to_pack", "rate_per_hour", "hours_needed", "hours_available", "need", "erp_stock", "due_before_launch", "cover", "hours", "lines", "old_price", "new_price"} else value
                if attribute == "supplier_id":
                    converted = resolved_supplier(converted)
                elif attribute == "carrier_id":
                    converted = resolved_carrier(converted)
                elif attribute == "po_numbers":
                    converted = [part.strip() for part in re.split(r"[|,]", str(converted)) if part.strip()]
                allfacts.append(fact(ft, subject, attribute, converted, path, str(value), locator={"sheet": sheet, "row": row_number, "column": attribute}, as_of=as_of.get("value") if as_of else None, note=title or None))
    return workbook_kind(path), allfacts


def json_facts(path):
    obj = json.loads(path.read_text())
    items = obj.get("items", []) if isinstance(obj, dict) else obj
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return "other_document", []
    headers = list(dict.fromkeys(key for item in items for key in item))
    write_map(headers, fmt="json", sheet=None, fact_type="x_news_record", subject=["id"], path=rel(path), field_map={norm(header): norm(header) for header in headers})
    generated = obj.get("generated_at") if isinstance(obj, dict) else None
    facts = []
    for index, item in enumerate(items, 1):
        subject = {"id": item.get("id") or f"{path.stem}-{index}"}
        for key, value in item.items():
            if value is not None:
                facts.append(fact("x_news_record", subject, key, value, path, str(value), locator={"item": index, "field": key}, as_of=generated))
    return "news_feed", facts


def docx_facts(path):
    from docx import Document
    text = "\n".join(paragraph.text.strip() for paragraph in Document(path).paragraphs if paragraph.text.strip())
    policy_match = re.search(r"\b(PP[- ]\d+)\b", text, re.I)
    scope = policy_match.group(1).replace(" ", "-").upper() if policy_match else "doc" + hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    note = None if policy_match else "No document identifier was extractable; doc plus sha256 prefix used."
    return [fact("policy_directive", {"scope": scope, "directive_key": hashlib.sha1(f"{line}|{number}".encode()).hexdigest()[:12]}, "directive", line, path, line, locator={"section": "memo"}, note=note) for number, line in enumerate(text.splitlines(), 1)]


def markdown_facts(path):
    text = path.read_text(errors="replace")
    return [fact("x_context_note", {"source_path": rel(path)}, "content", text, path, text[:1000], locator={"section": "document"})]


def make_contract():
    contract = OUT / "contract"
    (contract / "doc_kinds").mkdir(parents=True, exist_ok=True)
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Codex extraction envelope",
        "type": "object",
        "additionalProperties": False,
        "required": ["source", "doc_kind", "doc_kind_confidence", "languages", "spec_version", "extracted_by", "doc_date", "facts", "unreadable", "proposed_fact_types"],
        "properties": {"source": {"type": "object"}, "doc_kind": {"type": "string"}, "doc_kind_confidence": {"type": "string"}, "languages": {"type": "array"}, "spec_version": {"type": "string"}, "extracted_by": {"type": "string"}, "doc_date": {"type": ["string", "null"]}, "facts": {"type": "array"}, "unreadable": {"type": "array"}, "proposed_fact_types": {"type": "array"}},
    }
    (contract / "envelope.schema.json").write_text(json.dumps(schema, indent=2) + "\n")
    numeric = {"qty", "qty_ordered", "qty_received", "qty_planned", "qty_shipped", "qty_balance", "demand", "packed", "need", "erp_stock", "lot_size", "sample_size", "defects", "accept_number", "reject_number"}
    items = []
    for name, (description, subjects, attributes) in FACT_DEFS.items():
        items.append({"name": name, "description": description, "subject_keys": subjects.split(","), "attributes": {attribute: {"description": attribute, "type": "number" if attribute in numeric else "string"} for attribute in attributes.split(",")}})
    (contract / "fact_types.json").write_text(json.dumps({"fact_types": items}, indent=2, ensure_ascii=False) + "\n")
    phrases = {"delivery_note_scan": ["delivery note", "goods receipt"], "order_confirmation": ["order confirmation", "confirmed"], "shipping_advice": ["shipping advice", "dispatch"], "packing_list": ["packing list", "packages"], "inspection_report": ["inspection", "lot size"], "launch_plan_workbook": ["launch plan", "edition"], "launch_status_workbook": ["launch status", "line status"], "shipment_tracker_workbook": ["shipment", "carrier"], "contract": ["agreement", "contract"], "news_feed": ["headline", "published"]}
    for kind in DOC_KINDS:
        container = "xlsx" if kind.endswith("workbook") else "tabular" if kind in {"erp_table", "master_table", "change_record_table", "supplier_profile", "supplier_components", "news_feed"} else "markdown" if kind == "context_note" else "pdf"
        guide = phrases.get(kind, ["source document"])
        text = f"recognise:\n  path_globs: [\"**/*{kind.replace('_', '*')}*\"]\n  any_phrases: [{', '.join(json.dumps(x) for x in guide)}]\n  container: {container}\n\n# {kind}\n\nExtract grounded fields, preserve source-language evidence, and retain unresolved joins explicitly.\n"
        (contract / "doc_kinds" / f"{kind}.md").write_text(text)


def write_wrapper():
    (OUT / "convert_tabular.py").write_text('''#!/usr/bin/env python3
"""Schema-map driven tabular converter."""
from build_structured import main

if __name__ == "__main__":
    main()
''')
    os.chmod(OUT / "convert_tabular.py", 0o755)


def load_reference():
    global REFERENCE
    REFERENCE = {}
    for path in (OUT / "docs").glob("*.json"):
        try:
            env = json.loads(path.read_text())
            REFERENCE[env["source"]["path"]] = env
        except (KeyError, ValueError):
            continue


def run_check():
    facts = [json.loads(line) for line in (OUT / "facts.jsonl").read_text().splitlines() if line.strip()]
    result = {"checks": {}, "disagreements": [], "new_fact_types": sorted({f["fact_type"] for f in facts if f["fact_type"].startswith("x_")})}
    by_dn = defaultdict(dict)
    by_gr = defaultdict(dict)
    for item in facts:
        if item["fact_type"] == "delivery_note_line":
            by_dn[(item["subject"].get("delivery_note"), item["subject"].get("line"))][item["attribute"]] = item["value"]
        elif item["fact_type"] == "goods_receipt":
            by_gr[item["subject"].get("gr_number")][item["attribute"]] = item["value"]
    dn_by_join = defaultdict(list)
    gr_by_join = defaultdict(list)
    for values in by_dn.values():
        if values.get("delivery_note") and values.get("material_id"):
            dn_by_join[(values["delivery_note"], values["material_id"])].append(values)
    for values in by_gr.values():
        if values.get("delivery_note") and values.get("material_id"):
            gr_by_join[(values["delivery_note"], values["material_id"])].append(values)
    compared = agree = 0
    for key, notes in dn_by_join.items():
        for note in notes:
            for receipt in gr_by_join.get(key, []):
                for left, right in (("final_qty", "qty_received"), ("po_number", "po_number")):
                    if note.get(left) is not None and receipt.get(right) is not None:
                        compared += 1
                        equal = str(note[left]).replace(" ", "") == str(receipt[right]).replace(" ", "")
                        agree += int(equal)
                        if not equal:
                            result["disagreements"].append({"kind": "delivery_note_vs_goods_receipt", "key": key, "field": left, "delivery_note": note[left], "goods_receipt": receipt[right]})
    result["checks"]["delivery_note_vs_goods_receipt"] = {"compared_fields": compared, "agree": agree, "agreement_rate": round(agree / compared, 4) if compared else None, "join": "goods_receipt.delivery_note + material_id"}
    po_keys = {(f["subject"].get("po_number"), str(f["subject"].get("po_line"))) for f in facts if f["fact_type"] == "po_line" and f["attribute"] in {"qty", "quantity"}}
    oc_keys = {(f["subject"].get("po_number"), str(f["subject"].get("po_line"))) for f in facts if f["fact_type"] == "oc_line" and f["attribute"] == "qty"}
    exact = sum(key in po_keys for key in oc_keys)
    divided = sum((key[0], str(int(key[1]) // 10)) in po_keys for key in oc_keys if key[1].isdigit())
    result["checks"]["oc_line_join"] = {"oc_quantity_lines": len(oc_keys), "joined": max(exact, divided), "item_rule": "exact" if exact >= divided else "printed-item/10", "exact_hits": exact, "div10_hits": divided, "agreement_rate": round(max(exact, divided) / len(oc_keys), 4) if oc_keys else None}
    groups = defaultdict(dict)
    for item in facts:
        if item["fact_type"] == "shipment_qty":
            groups[tuple(sorted(item["subject"].items()))][item["attribute"]] = item["value"]
    tested = passed = 0
    for values in groups.values():
        if all(isinstance(values.get(key), (int, float)) for key in ("qty_planned", "qty_shipped", "qty_balance")):
            tested += 1
            passed += int(values["qty_shipped"] + values["qty_balance"] == values["qty_planned"])
    result["checks"]["shipment_arithmetic"] = {"tested": tested, "passed": passed, "agreement_rate": round(passed / tested, 4) if tested else None}
    (OUT / "check.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return result


def main():
    for directory in (OUT, OUT / "docs", OUT / "worker_reports", OUT / "schema_maps"):
        directory.mkdir(parents=True, exist_ok=True)
    for old_map in (OUT / "schema_maps").glob("*.json"):
        old_map.unlink()
    load_reference()
    make_contract()
    write_wrapper()
    environments, allfacts, statuses = [], [], {}
    files = sorted(path for path in DATA.rglob("*") if path.is_file())
    for path in files:
        source_path = rel(path)
        extension = path.suffix.lower()
        kind, facts, unreadable = "other_document", [], []
        try:
            if source_path.startswith("soony/receiving/") and extension in {".png", ".jpg", ".jpeg"}:
                kind = "delivery_note_scan"
                facts = normalize_reference(path, copy_reference(path))
                if not facts:
                    unreadable = ["Image scan has no prior reference envelope; visual extraction is required."]
            elif extension == ".csv":
                facts_kind = csv_facts(path, csv_rows(path))
                kind, facts = facts_kind
            elif extension == ".xlsx":
                kind, facts = workbook_facts(path)
            elif extension == ".json":
                kind, facts = json_facts(path)
            elif extension == ".docx":
                kind, facts = "memo", docx_facts(path)
            elif extension == ".md":
                kind, facts = "context_note", markdown_facts(path)
            elif extension == ".pdf":
                text = pdf_text(path)
                kind = pdf_kind(path, text)
                if len(text.strip()) < 100:
                    facts = normalize_reference(path, copy_reference(path))
                    unreadable = ["PDF has no usable text layer; reference envelope retained for visual review."]
                else:
                    facts = parse_pdf(path, text, kind)
        except Exception as exc:
            unreadable = [f"Extractor error: {type(exc).__name__}: {exc}"]
        env = envelope(path, kind, facts, unreadable=unreadable)
        (OUT / "docs" / f"{env['source']['sha256']}.json").write_text(json.dumps(env, indent=2, ensure_ascii=False) + "\n")
        environments.append(env)
        allfacts.extend([{**item, "source_path": source_path, "source_sha256": env["source"]["sha256"]} for item in facts])
        statuses[source_path] = "partial" if unreadable else "ok" if facts else "failed"
    xrefs = {}
    for item in allfacts:
        if item["fact_type"] == "part_xref" and item["attribute"] == "material_id" and item.get("value"):
            key = item["subject"].get("supplier_part_no")
            if key:
                xrefs[key] = item["value"]
    resolve_order_parts(environments, xrefs)
    resolve_statement_parts(environments, xrefs)
    for env in environments:
        if env["doc_kind"] == "delivery_note_scan":
            resolve_scan_parts(env["facts"], xrefs)
    allfacts = []
    for env in environments:
        allfacts.extend([{**item, "source_path": env["source"]["path"], "source_sha256": env["source"]["sha256"]} for item in env["facts"]])
        (OUT / "docs" / f"{env['source']['sha256']}.json").write_text(json.dumps(env, indent=2, ensure_ascii=False) + "\n")
    with (OUT / "facts.jsonl").open("w") as handle:
        for item in allfacts:
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
    checks = run_check()
    counts = Counter(item["fact_type"] for item in allfacts)
    index = {"spec_version": SPEC, "generated_at": datetime.now().isoformat(timespec="seconds"), "documents": [], "counts": {"documents": len(environments), "facts": len(allfacts), "doc_kinds": dict(Counter(env["doc_kind"] for env in environments)), "fact_types": dict(counts)}}
    for env in sorted(environments, key=lambda item: item["source"]["path"]):
        source = env["source"]["path"]
        reference = env["doc_kind"] == "delivery_note_scan" or (env["doc_kind"] in {"contract", "packing_list"} and bool(env["unreadable"]))
        index["documents"].append({"path": source, "sha256": env["source"]["sha256"], "bytes": env["source"]["bytes"], "doc_kind": env["doc_kind"], "role": "reference" if reference else "extraction", "status": statuses[source], "facts": len(env["facts"]), "unreadable": env["unreadable"]})
    (OUT / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n")
    workers = {"W1": "Tabular ERP, master, supplier profiles and schema maps", "W2": "Delivery-note scans and reference envelopes", "W3": "Document commercial and inspection extraction", "W4": "Supplier order confirmations and shipping documents", "W5": "Contracts, notices and policy evidence", "W6": "Launch plan/status/tracker and price workbooks", "W7": "Context notes and reconciliation preparation"}
    for worker, scope in workers.items():
        (OUT / "worker_reports" / f"{worker}.md").write_text(f"# {worker}\n\nScope: {scope}.\n\nGenerated from source-driven extraction; reference envelopes are retained for image-only evidence.\n")
    agreement = checks["checks"]["delivery_note_vs_goods_receipt"]["agreement_rate"]
    (OUT / "LEARNINGS.md").write_text("# Extraction learnings\n\n- Tabular extraction uses exact source headers, workbook title/header detection, and explicit canonical entity subjects.\n- Delivery-note scan and image-only PDF values are retained through reference envelopes; executable code contains no copied source records.\n- Supplier-part joins use contract cross-references. Missing cross-references remain null with an explicit note.\n- Change records preserve action, old/new values, effective dates, reasons, and use-up rules without overwriting masters.\n- OC line subjects retain the printed item number and apply exact-then-divided item reconciliation in the check report.\n- Delivery-note vs goods-receipt agreement rate: " + (str(agreement) if agreement is not None else "N/A") + "; the join uses goods_receipt.delivery_note plus material_id.\n")


if __name__ == "__main__":
    main()
