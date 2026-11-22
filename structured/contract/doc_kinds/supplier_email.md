recognise:
  path_globs: ["**/*supplier*email*", "**/mail/**", "**/*.eml"]
  any_phrases: ["from:", "subject:", "sent:", "dear"]
  container: text

# supplier_email

A message from a supplier. Extract grounded fields, preserve source-language evidence, and retain unresolved
joins explicitly. Keep quantities, prices and dates verbatim as written and parse them in code.

A supplier email usually carries one or both of:
- a promise, recorded as a `supplier_statement` (subject part, quantity, due_time, condition_to_release,
  promise_kind = confirm / delay / cancel / short-ship);
- an offer, recorded as an `offer` (quantity, unit_price, lot_size, ready_time, validity, deposit).
