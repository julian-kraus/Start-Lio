# Extraction learnings

- The corpus contains 313 current files and three image-only PDFs. The image-only contracts and packing list were rendered and reviewed visually; their extracted facts are marked medium confidence and the envelope retains the partial/unreadable note.
- Delivery-note scans require a distinct printed/handwritten/struck-through/final quantity model. The 2026-11-16 Karton note is a real contradiction: printed 80,000 is struck through and handwritten 8,000 is the bookable final quantity.
- Numeric conventions are source-specific: Polish scans use dot thousands separators, Chinese/English source tables use comma separators, and the legacy SAP CSV uses decimal commas. Normalization is applied only after the source format is identified.
- Workbook title rows and footer notes are provenance-bearing; sheet and row locators are retained in facts and schema maps.
- Change-record CSVs are emitted as `change_record` facts and do not overwrite master facts.
- OC item-number normalization remains an explicit reconciliation rule (exact, then printed item number divided by ten); unmatched joins remain visible.
- Same-delivery-note scan-to-goods-receipt comparison is not computable for the current corpus because no 2026 scanned delivery-note number joins to the ERP goods-receipt export. This is reported as N/A, not as agreement.

## Reconciliation summary

- Delivery note vs goods receipt: None
- OC line join: 1.0
- Text-PDF numeric grounding: 1.0
- Shipment arithmetic: 1.0

New open-vocabulary fact types: `x_context_note, x_document_line, x_org_chart_entry, x_workbook_row`.
