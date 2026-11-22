# Scenario event catalog

Generated from the YAML files in this directory by `lab.catalog.render_readme`. Do not edit by hand;
run `python -m lab.catalog readme` to regenerate after changing a family file.

45 event types in 15 families. Every value shown at run time
is a reference resolved from kb facts on the seeded lab database; nothing here carries a portal value.

| Family | Types |
| --- | --- |
| `cancellations` | 3 |
| `capacity` | 3 |
| `confirmations` | 3 |
| `conflicts` | 3 |
| `delays` | 3 |
| `deliveries` | 3 |
| `demand` | 3 |
| `logistics` | 3 |
| `noise` | 3 |
| `offers` | 3 |
| `policy` | 3 |
| `proof` | 3 |
| `quality` | 3 |
| `silence` | 3 |
| `terms` | 3 |

## cancellations

| id | channel | references | description |
| --- | --- | --- | --- |
| `cancellations.line_cancelled` | mail | order_line, supplier | Supplier cancels a single order line. |
| `cancellations.order_cancelled` | mail | order_line, supplier | Supplier cancels every line on an order. |
| `cancellations.force_majeure` | mail | order_line, supplier | Supplier invokes force majeure and halts supply for a line. |

## capacity

| id | channel | references | description |
| --- | --- | --- | --- |
| `capacity.allocation_cut` | mail | part, supplier | Supplier reduces the quantity it can allocate to us for a part. |
| `capacity.stock_update` | table | part | A stock-level table shows a new on-hand quantity for a part. |
| `capacity.moq_change` | mail | part, supplier | Supplier raises the minimum order quantity for a part. |

## confirmations

| id | channel | references | description |
| --- | --- | --- | --- |
| `confirmations.order_confirmed` | mail | order_line, supplier | Supplier confirms the delivery date for an order line. |
| `confirmations.order_confirmed_doc` | document | order_line, supplier | Supplier sends an order confirmation document for a line. |
| `confirmations.partial_confirm` | mail | order_line, supplier | Supplier confirms only part of the ordered quantity for a line. |

## conflicts

| id | channel | references | description |
| --- | --- | --- | --- |
| `conflicts.email_vs_portal_date` | mail | kb_id, order_line, supplier | Supplier mail gives a delivery date that contradicts the date already on record for a line. |
| `conflicts.email_vs_portal_qty` | mail | kb_id, order_line, supplier | Supplier mail gives a shipped quantity that contradicts the delivery note on record. |
| `conflicts.price_disagreement` | mail | kb_id, part, supplier | Supplier mail states a unit price that contradicts the price fact on record. |

## delays

| id | channel | references | description |
| --- | --- | --- | --- |
| `delays.new_eta` | mail | order_line, supplier | Supplier announces a later arrival date for a confirmed order line. |
| `delays.eta_unknown` | mail | order_line, supplier | Supplier can no longer give an arrival date for a confirmed line. |
| `delays.recovery` | mail | order_line, supplier | A previously slipped line recovers to an earlier arrival date. |

## deliveries

| id | channel | references | description |
| --- | --- | --- | --- |
| `deliveries.partial_shipped` | document | order_line, supplier | Supplier ships fewer units than the line ordered and sends a delivery note. |
| `deliveries.short_delivery` | mail | order_line, supplier | Supplier warns that a shipment will be short against the order line. |
| `deliveries.split_shipment` | mail | order_line, supplier | Supplier splits a line into several shipments on different dates. |

## demand

| id | channel | references | description |
| --- | --- | --- | --- |
| `demand.preorders_up` | table | edition, part | Pre-orders for an edition rise, increasing the quantity of a part needed. |
| `demand.preorders_down` | table | edition, part | Pre-orders for an edition fall, reducing the quantity of a part needed. |
| `demand.forecast_revision` | document | edition, part | A revised demand forecast document changes the planned quantity for an edition. |

## logistics

| id | channel | references | description |
| --- | --- | --- | --- |
| `logistics.transport_booked` | mail | order_line, supplier | Supplier books a transport mode for a shipment on an order line. |
| `logistics.air_waybill` | document | order_line, supplier | Supplier sends an air waybill for an expedited shipment. |
| `logistics.customs_hold` | mail | order_line, supplier | A shipment is held at customs and will miss its planned arrival. |

## noise

| id | channel | references | description |
| --- | --- | --- | --- |
| `noise.duplicate_message` | mail | kb_id, supplier | Supplier resends a message already on record; ingestion is idempotent and adds nothing. |
| `noise.out_of_scope` | mail | supplier | Supplier sends an unrelated message (marketing or general enquiry) that yields no fact. |
| `noise.garbled_attachment` | document | supplier | Supplier sends an unreadable attachment that cannot be parsed into facts. |

## offers

| id | channel | references | description |
| --- | --- | --- | --- |
| `offers.quote` | mail | part, supplier | Supplier quotes a unit price and lead time for a part. |
| `offers.quote_sheet` | document | part, supplier | Supplier sends a quotation document listing parts and prices. |
| `offers.revised_quote` | mail | part, supplier | Supplier revises an earlier quote with a new price. |

## policy

| id | channel | references | description |
| --- | --- | --- | --- |
| `policy.signer_vacant` | table | none | The role that must sign a class of decisions becomes vacant. |
| `policy.approval_limit_change` | table | none | The approval limit for a signer role changes. |
| `policy.signer_appointed` | table | none | A role that was vacant has a signer appointed again. |

## proof

| id | channel | references | description |
| --- | --- | --- | --- |
| `proof.loading_photo` | document | order_line, supplier | Supplier sends a photo showing goods loaded for an order line. |
| `proof.cmr` | document | order_line, supplier | Supplier sends a signed CMR consignment note for a shipment. |
| `proof.goods_receipt` | table | order_line | A goods receipt is booked in the receiving table for a delivered line. |

## quality

| id | channel | references | description |
| --- | --- | --- | --- |
| `quality.inspection_pass` | document | part, supplier | An incoming inspection passes for a part. |
| `quality.inspection_fail` | document | part, supplier | An incoming inspection fails for a part with a recorded defect. |
| `quality.concession_request` | mail | part, supplier | Supplier asks to ship a part out of specification under a concession. |

## silence

| id | channel | references | description |
| --- | --- | --- | --- |
| `silence.no_reply` | clock | order_line | A promised reply deadline for an order line passes with no supplier message. |
| `silence.quote_lapsed` | clock | part | A quote validity date for a part passes with no renewal. |
| `silence.promise_due` | clock | order_line | A promised delivery date passes with no proof of shipment. |

## terms

| id | channel | references | description |
| --- | --- | --- | --- |
| `terms.price_increase` | mail | part, supplier | Supplier announces a higher unit price for a part. |
| `terms.price_list_update` | table | part, supplier | A new price list version changes the unit price row for a part. |
| `terms.payment_terms_change` | mail | supplier | Supplier changes the payment terms that apply to future orders. |
