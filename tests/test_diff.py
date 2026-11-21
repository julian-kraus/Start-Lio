import io
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from openpyxl import Workbook

from conftest import make_pdf
from tracker import diff
from tracker.config import StaleRule, parse_stale_rules
from tracker.detectors import find_stale, pair_renames
from tracker.diff import csv_diff
from tracker.diff.records import diff_tables


def xlsx(rows_by_sheet: dict[str, list[list]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in rows_by_sheet.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


OLD_CSV = (
    "shipment_id,eta,status,qty\n"
    "SHP-1,2026-11-19,in_transit,48000\n"
    "SHP-2,2026-11-20,booked,1200\n"
).encode()

# Same data after a 'format drift': semicolons, cp1252, CRLF, German dates, renamed column,
# plus one real change (SHP-1 eta and qty) and one new row.
NEW_CSV = (
    "Shipment ID;ETA;Status;Quantity\r\n"
    "SHP-1;23.11.2026;in_transit;28000\r\n"
    "SHP-2;20.11.2026;booked;1200\r\n"
    "SHP-3;21.11.2026;booked;500 – Lagerplatz Süd\r\n"
).encode("cp1252")


def test_csv_parse_detects_encoding_and_delimiter():
    table = csv_diff.parse(NEW_CSV)
    assert table.meta["encoding"] == "cp1252"
    assert table.meta["delimiter"] == ";"
    assert table.columns == ["shipment_id", "eta", "status", "quantity"]
    assert table.rows[2]["quantity"].endswith("Süd")


def test_csv_diff_field_level_with_format_drift():
    summary, detail = diff.compute("a.csv", OLD_CSV, "a.csv", NEW_CSV)
    assert detail["key"] == ["shipment_id"]
    assert detail["schema"]["columns_renamed"] == {"qty": "quantity"}
    assert detail["format"]["delimiter"] == [",", ";"]
    changed = {r["key"]: r["changes"] for r in detail["rows_changed"]}
    assert changed == {"SHP-1": {"eta": ["2026-11-19", "23.11.2026"], "quantity": ["48000", "28000"]}}
    assert [r["key"] for r in detail["rows_added"]] == ["SHP-3"]
    assert detail["format_only_changes"] == {"eta": 1}  # SHP-2: same date, new format
    assert "SHP-1" in summary and "1 rows changed" in summary


def test_csv_composite_key():
    old = b"EBELN;EBELP;MENGE\n4500;00010;100\n4500;00020;50\n4501;00010;10\n"
    new = b"EBELN;EBELP;MENGE\n4500;00010;100\n4500;00020;75\n4501;00010;10\n"
    _, detail = diff.compute("po.csv", old, "po.csv", new)
    assert detail["key"] == ["ebeln", "ebelp"]
    assert detail["rows_changed"] == [{"key": "4500 | 00020", "changes": {"menge": ["50", "75"]}}]


def test_csv_without_key_falls_back_to_positions():
    old = b"a,b\nx,1\nx,1\n"
    new = b"a,b\nx,1\nx,2\n"
    _, detail = diff.compute("t.csv", old, "t.csv", new)
    assert detail["key"] is None
    assert detail["rows_changed"][0]["changes"] == {"b": ["1", "2"]}


def test_diff_cap_truncates():
    old = "id,v\n" + "".join(f"{i},a\n" for i in range(50))
    new = "id,v\n" + "".join(f"{i},b\n" for i in range(50))
    _, detail = diff.compute("t.csv", old.encode(), "t.csv", new.encode(), max_items=10)
    assert len(detail["rows_changed"]) == 10 and detail["truncated"]


def test_xlsx_cell_diff_with_labels():
    old = xlsx({"Inbound": [["shipment", "qty"], ["SHP-1", 48000]], "Notes": [["x"]]})
    new = xlsx({"Inbound": [["shipment", "qty"], ["SHP-1", 28000]], "Extra": [["y"]]})
    summary, detail = diff.compute("plan.xlsx", old, "plan.xlsx", new)
    assert detail["kind"] == "xlsx"
    assert detail["sheets_added"] == ["Extra"] and detail["sheets_removed"] == ["Notes"]
    cell = detail["cells"]["Inbound"][0]
    assert cell == {"cell": "B2", "old": 48000, "new": 28000, "row_label": "SHP-1", "column_label": "qty"}
    assert "48000→28000" in summary


def test_pdf_text_diff_and_scan():
    old = make_pdf(["Delivery date 19.11.2026", "Quantity 48000"])
    new = make_pdf(["Delivery date 23.11.2026", "Quantity 48000"])
    summary, detail = diff.compute("oc.pdf", old, "oc.pdf", new)
    assert detail["lines_added"] == ["Delivery date 23.11.2026"]
    assert detail["lines_removed"] == ["Delivery date 19.11.2026"]
    summary, detail = diff.compute("scan.pdf", make_pdf([]), "scan.pdf", make_pdf([]))
    assert detail["scan"] is True and "no text layer" in summary


def test_json_tree_and_records():
    old = {"updated_at": "2026-11-20T10:00:00+01:00", "shipments": [
        {"id": "SHP-1", "status": "in_transit"}, {"id": "SHP-2", "status": "booked"}]}
    new = {"updated_at": "2026-11-21T08:00:00+01:00", "shipments": [
        {"id": "SHP-1", "status": "arrived_port"}, {"id": "SHP-2", "status": "booked"}]}
    summary, detail = diff.compute("s.json", json.dumps(old).encode(), "s.json", json.dumps(new).encode())
    assert detail["changes"][0]["path"] == "updated_at"
    table = detail["tables"]["shipments"]
    assert table["rows_changed"] == [{"key": "SHP-1", "changes": {"status": ["in_transit", "arrived_port"]}}]


def test_csv_to_json_format_switch():
    old = b"shipment_id,status\nSHP-1,in_transit\n"
    new = json.dumps([{"shipment_id": "SHP-1", "status": "arrived_port"}]).encode()
    summary, detail = diff.compute("open_shipments.csv", old, "open_shipments.json", new)
    assert detail["kind_changed"] == ["csv", "json"]
    assert detail["rows_changed"][0]["changes"] == {"status": ["in_transit", "arrived_port"]}


def test_detect_kind():
    assert diff.detect_kind("x.pdf", make_pdf(["a"])) == "pdf"
    assert diff.detect_kind("x.xlsx", xlsx({"S": [[1]]})) == "xlsx"
    assert diff.detect_kind("x.jpg", b"\xff\xd8\xff\xe0rest") == "image"
    assert diff.detect_kind("x.csv", b"a,b\n1,2\n") == "csv"
    assert diff.detect_kind("x.json", b'{"a": 1}') == "json"
    assert diff.detect_kind("notes.md", b"# hello") == "text"


def test_describe_added():
    summary, detail = diff.describe("notice.pdf", make_pdf(["Change of bank details", "IBAN DE00 1234"]))
    assert summary == "PDF, 1 pages" and "IBAN" in detail["text_preview"]
    summary, detail = diff.describe("x.csv", OLD_CSV)
    assert detail["rows"] == 2 and detail["columns"][0] == "shipment_id"


def test_parsers_never_raise():
    summary, detail = diff.compute("x.pdf", b"%PDF-broken", "x.pdf", b"%PDF-also broken")
    assert "diff failed" in summary


def test_pair_renames():
    assert pair_renames(["soony/erp/open_shipments.csv", "a/b.csv"], ["soony/erp/open_shipments.json", "c.csv"]) == {
        "soony/erp/open_shipments.json": "soony/erp/open_shipments.csv"}


def test_stale_rules():
    rules = parse_stale_rules("soony/erp/open_shipments.*=6h, soony/logistics/*=30m")
    assert rules == (StaleRule("soony/erp/open_shipments.*", 21600), StaleRule("soony/logistics/*", 1800))
    now = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
    last = {"soony/erp/open_shipments.json": now - timedelta(hours=7), "soony/logistics/t.xlsx": now - timedelta(minutes=5)}
    found = find_stale(rules, last, set(), now)
    assert [p for p, _, _ in found] == ["soony/erp/open_shipments.json"]
    assert find_stale(rules, last, {"soony/erp/open_shipments.json"}, now) == []


def test_ambiguous_values_are_never_hidden():
    old = b"id,date,qty,price\nA,2026-03-04,1500,1.5\nB,2026-04-20,7,2\n"
    new = b"id,date,qty,price\nA,03/04/2026,1.500,\"1,5\"\nB,20/04/2026,7,2\n"
    _, detail = diff.compute("t.csv", old, "t.csv", new)
    changed = {r["key"]: r["changes"] for r in detail["rows_changed"]}
    # 03/04/2026 could be 3 April or 4 March, 1.500 could be 1.5 or 1500: both reported.
    assert set(changed["A"]) == {"date", "qty"}
    # 20/04/2026 is unambiguous and '1,5' is clearly 1.5: format-only.
    assert detail["format_only_changes"] == {"date": 1, "price": 1}


def test_rename_needs_value_overlap():
    old = b"id,price\n1,10\n2,20\n"
    new = b"id,supplier\n1,ACME\n2,Nowak\n"
    _, detail = diff.compute("t.csv", old, "t.csv", new)
    assert detail["schema"]["columns_renamed"] == {}
    assert detail["schema"]["columns_added"] == ["supplier"] and detail["schema"]["columns_removed"] == ["price"]
    assert detail["rows_changed"] == []


def test_deep_json_does_not_raise():
    deep = ("[" * 50000 + "]" * 50000).encode()
    summary, detail = diff.compute("d.json", b"[]", "d.json", deep)
    assert isinstance(summary, str)
    summary, detail = diff.describe("d.json", deep)
    assert isinstance(summary, str)


def test_large_keyless_table_is_fast():
    rows = "".join(f"x,{i % 7}\n" for i in range(3000))
    old = ("a,b\n" + rows).encode()
    new = ("a,b\n" + rows + "x,99\n").encode()
    t = time.perf_counter()
    _, detail = diff.compute("big.csv", old, "big.csv", new)
    assert time.perf_counter() - t < 2
    assert [r["row"]["b"] for r in detail["rows_added"]] == ["99"]


def test_check_path():
    from tracker.store import UnsafePath, check_path
    assert check_path("suppliers/SUP-1/a.pdf") == "suppliers/SUP-1/a.pdf"
    for bad in ("../x", "a/../../x", "/etc/passwd", "a//b", "a/./b", "", "a\\b", "a\x00b", "a/"):
        with pytest.raises(UnsafePath):
            check_path(bad)


def test_diff_tables_identical():
    t = csv_diff.parse(OLD_CSV)
    detail = diff_tables(t, t)
    assert not detail["rows_changed"] and not detail["rows_added"] and not detail["rows_removed"]
