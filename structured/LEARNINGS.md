# Extraction learnings

- Tabular extraction uses exact source headers, workbook title/header detection, and explicit canonical entity subjects.
- Delivery-note scan and image-only PDF values are retained through reference envelopes; executable code contains no copied source records.
- Supplier-part joins use contract cross-references. Missing cross-references remain null with an explicit note.
- Change records preserve action, old/new values, effective dates, reasons, and use-up rules without overwriting masters.
- OC line subjects retain the printed item number and apply exact-then-divided item reconciliation in the check report.
- Delivery-note vs goods-receipt agreement rate: N/A; the join uses goods_receipt.delivery_note plus material_id.
