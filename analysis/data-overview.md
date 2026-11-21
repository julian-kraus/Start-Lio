# Soony / Grand Heist 6: data overview and launch risk

Snapshot: `soony-data.zip` as of 2026-11-21, unzipped to `data/snapshot-zip/soony-data/`. All file paths below are relative to that folder. The portal changes during the hack, so re-check any number here against the live files before using it in a pitch.

Conventions: "Data says" means stated in a file. **Inference** marks my own conclusion or assumption. Quantities are pieces (PC) unless stated.

## Executive summary (ranked by launch impact)

1. **Game cards are short by 6,800, and no source in the data can close the gap before Sunday 20:00.** Need 153,200, ERP stock 118,400, the only in-time inbound is a Hwaseong air shipment that left short: 28,000 instead of 48,000 (`suppliers/SUP-0010_Hwaseong_Memory/order_confirmations/Shipping_advice_020-48113256_4500318735.pdf`). The launch plan still counts 48,000 (`soony/context/launch_plan_GH6.xlsx`, sheet Inbound row 2). Boise's 36,000 are on hold at Boise over an Incoterm dispute and would land after launch anyway.
2. **Art book stock is overstated by 72,000. Real cover is 19,300 against 25,200 needed (short 5,900).** GR 5000413065 booked 80,000 (`soony/erp/goods_receipts.csv`), the scanned delivery note shows 80,000 struck through and "8 000" handwritten (`soony/receiving/DC-HAM/20261118_WZ-261116-473.jpg`). Fixable today: Karton Nowak offers 8,000 overrun art books, loaded within 4 h, about 9 h by truck, offer valid 24 h from 21.11 (`suppliers/SUP-0028_Karton_Nowak/notices/Oferta_tryb_ekspresowy_2026-11-21.pdf`).
3. **Statues: 5,600 of the 27,400 in ERP failed incoming inspection.** Without them, 21,800 against 25,200 needed (short 3,400). The lot is booked "unrestricted" despite failing AQL; use needs a concession signed by Supplier Quality and the Category Lead (`soony/receiving/DC-HAM/inspection_report_5000413063.pdf`), and the Category Lead Collector's Items position is vacant (`soony/context/org_chart_procurement.pdf`, `soony/context/onboarding_notes.md`).
4. **Two cover-sleeve artworks are in stock, and the master data names the older one.** Karton Nowak prints rev. C "USK2026" received from Soony Marketing (e.g. `suppliers/SUP-0028_Karton_Nowak/order_confirmations/OA26293887_4500318775.pdf`); PackRight prints rev. B "per Soony drawing on file" (`.../SUP-0029_Guangzhou_PackRight/order_confirmations/OA26182346_4500318738.pdf`); `soony/master/components.csv` says CMP-GAM-004 drawing_rev B. Stock is 122,000 rev. C plus 64,000 rev. B. If rev. B is superseded, Standard is fine only thanks to Karton's rev. C. If rev. C is the unapproved one, Standard is short 64,000. This is a legal labelling question (USK age rating on the box), not a logistics one.
5. **Standard Edition depends on 350,000 cases sitting at the Hamburg container terminal.** ERP stock 96,000 against 128,000 needed. Container NHSU4471921 is "discharged at CTA Hamburg, available for pick-up" (`soony/erp/open_shipments.csv`, SHP-2026-04276) while the shared tracker still says "in transit, RP: call terminal" (`soony/logistics/open_shipments_tracker_v37_FINAL_final2.xlsx`, row 2). Saturday's full line capacity (16 h x 6,000 = 96,000) uses up every case in stock, so the container must be at the dock by Sunday 06:00.
6. **The "missing lid magnets" alarm is a false alarm.** The launch plan shows CMP-MAG-005 at -100,800 and "CHECK" (`launch_plan_GH6.xlsx`, Parts row 13). The magnets are fitted by PackRight into the Collector's box and are never ordered or stocked separately (`suppliers/SUP-0029_Guangzhou_PackRight/components.csv`, contract Annex A/B). The BOM line `FG-GH6-CE,CMP-MAG-005,4` in `soony/master/bom.csv` is what creates the phantom shortage.
7. **The Collector's cell has never packed close to the rate the plan assumes.** Plan: 900/h, 28 of 30 h, i.e. about 12,600 per day. Best day in the pack-out log: 6,800 (`soony/erp/packout_log.csv`, 2026-11-19). **Inference:** earlier days may have been parts-limited, but the 900/h rate is unproven.
8. **Supplier-side warnings were sent in languages nobody in procurement reads.** Karton Nowak announced on 09.11 (in Polish) that its offset press and bindery stop from 21.11 14:00 to 23.11 06:00, with rush capacity cut to 15,000 sleeves or leaflets per 36 h on the digital line (`suppliers/SUP-0028_Karton_Nowak/notices/Zawiadomienie_przestoj_techniczny_2026-11.pdf`). Any rush print ordered now is ready Sunday night at the earliest, too late.
9. **Contracts are being bent in ways that cost money and cause holds:** Boise ships EXW by contract while all 81 Soony POs say DDP Hamburg; Dongguan and PackRight raised prices without a contractual basis; Hwaseong's LTA expires 2026-12-31 with USD 5.4 m of open POs promised for 2027; Pune's conditional release expired on 2026-10-30. Details in section 4.

Bottom line of the launch math (section 3): with no action, Soony ships about 66,100 of 72,000 Collector's Editions and about 639,100 of 640,000 Standard. With the art book rush and a statue concession, all 72,000 Collector's can be packed, and Standard ends 6,800 short, which is still far above the 415,000 Standard pre-orders. Each Collector's Edition packed takes a game card away from Standard.

---

## 1. Overview: files and how they link

### 1.1 File inventory (308 files)

| Path | Format | Content |
|---|---|---|
| `soony/context/onboarding_notes.md` | Markdown | Kickoff notes 2026-11-11: launch 22.11 20:00, editions, crisis mode, open questions (one-source parts, missing lid magnets, cost of a missing copy, air-freight sign-off). |
| `soony/context/launch_plan_GH6.xlsx` | XLSX, 5 sheets | Status 2026-11-20 18:00. Editions (demand, packed, rate/h, hours), Parts (need vs ERP stock vs due before launch), Inbound (7 shipments), Shifts (Sat 16 h, Sun 14 h, no night shift), Notes. |
| `soony/context/approval_policy_PP-07.pdf` | PDF | Approval limits per PO, dock slot rule, air freight limits, conditional supplier release (90 days, one 90-day extension). |
| `soony/context/org_chart_procurement.pdf` | PDF | Roles. Category Lead Collector's Items = TBD. Supplier Quality Lead Daniel Okafor, Quality & Compliance Clara Weiss, CFO Henrik Dahl, Site Lead DC-HAM Katrin Jansen. |
| `soony/master/products.csv` | CSV | FG-GH5-STD (running), FG-GH6-STD, FG-GH6-CE (launch 2026-11-22). |
| `soony/master/bom.csv` | CSV | Parent FG to child CMP with qty_per. |
| `soony/master/components.csv` | CSV | 12 components, category, uom, ABC, critical flag, drawing_rev. |
| `soony/master/sites.csv` | CSV, CRLF | One site, DC-HAM. |
| `soony/master/routes.csv`, `ports.csv`, `logistics_providers.csv` | CSV | 11 routes with transit days, ports and airports, 4 carriers (LSP-01 sea, LSP-02 air, LSP-03 sea India, LSP-04 road Poland). |
| `soony/erp/purchase_orders_2025H2-2026.csv` | CSV, comma, ISO dates | 318 PO lines, statuses delivered / open / cancelled / awaiting_approval. |
| `soony/erp/purchase_orders_legacy_2024-2025H1.csv` | CSV, `;`, CRLF, decimal comma, US dates MM/DD/YYYY | 129 lines, SAP field names (EBELN, EBELP, LIFNR, MATNR, MENGE, NETPR, BEDAT, EINDT, ZZCONF, WEDAT, ZZSTAT, ERNAM). LIFNR is numeric (19 = SUP-0019), items as 00010, status GL/LO, user short names. |
| `soony/erp/goods_receipts.csv` | CSV | 392 GRs 2024-10-30 to 2026-11-18, with delivery note number and remarks. |
| `soony/erp/stock_snapshot.csv` | CSV | Stock per batch at DC-HAM on 2026-11-20. Batch = GR number. |
| `soony/erp/packout_log.csv` | CSV | Daily packed units 2026-11-02 to 2026-11-19, sums exactly to 512,000 STD and 46,800 CE. |
| `soony/erp/open_shipments.csv` | CSV | 25 shipments with PO, material, route, ETD/ETA, status, last position. |
| `soony/logistics/open_shipments_tracker_v37_FINAL_final2.xlsx` | XLSX | Manual tracker, 14 shipments, sea only. |
| `soony/receiving/DC-HAM/*.jpg, *.png` | 60 scanned delivery notes | File name = receipt date + delivery note no. Prefix DN = Hwaseong/Boise/Pune, SN = Chinese suppliers, WZ = Karton Nowak (Polish "Dokument WZ"). |
| `soony/receiving/DC-HAM/inspection_report_5000413063.pdf` | PDF | Failed AQL inspection of Pune statues. |
| `suppliers/<ID>_<Name>/profile.csv` | CSV | Supplier master: country, currency, terms, Incoterm, qualification status, contact, language. |
| `suppliers/<ID>_<Name>/components.csv` | CSV | Supplier's parts, share, price, lead time, MOQ, primary flag, notes. |
| `suppliers/<ID>_<Name>/contracts/*.pdf` | PDF | One agreement per supplier. Dongguan and Karton Nowak are image-only scans (no text layer). |
| `suppliers/<ID>_<Name>/order_confirmations/*.pdf` | 224 PDFs | OCs named `OA<no>_<PO>.pdf`, plus one Hwaseong shipping advice and one PackRight packing list (image-only). Bilingual Korean/Chinese/Polish + English. |
| `suppliers/<ID>_<Name>/notices/*` | XLSX, PDF | Price lists (Boise, Dongguan, PackRight), Karton Nowak downtime notice (Polish) and rush offer (Polish/English). |

### 1.2 Suppliers

| ID | Name | Country, lang | Parts (Soony share) | Contract | Status |
|---|---|---|---|---|---|
| SUP-0010 | Hwaseong Memory | KR, ko | CMP-GAM-001 GH6 card (70 %, primary), CMP-GAM-002 GH5 card (70 %) | LTA PC-0010-2022, FOB Busan, fixed prices, expires 2026-12-31 | qualified |
| SUP-0011 | Boise Semi | US, en | CMP-GAM-001 (30 %), CMP-GAM-002 (30 %) | Sales Agreement PC-0011-2023, EXW Boise, Idaho law | qualified |
| SUP-0018 | Pune Diecast Works | IN, en | CMP-GAM-010 statue (20 %, second source) | Framework PC-0018-2026, FOB Nhava Sheva | **conditional** |
| SUP-0019 | Dongguan Precision Housings | CN, zh | CMP-GAM-003 case (40 %), CMP-GAM-006 steel case (100 %), CMP-GAM-010 statue (80 %) | Supply Agreement PC-0019-2018 (scan), FOB Yantian, expires 2027-02-28 | qualified |
| SUP-0028 | Karton Nowak | PL, pl | CMP-GAM-004 GH6 sleeve (60 %), 005 GH5 sleeve, 008 art book, 009 poster, 011 leaflet (each 100 %) | Supply Agreement PC-0028-2021 (scan), DAP Hamburg, rush clause § 3.2 | qualified |
| SUP-0029 | Guangzhou PackRight | CN, zh | CMP-GAM-003 case (60 %), 004 sleeve (40 %), 007 Collector's box (100 %), MAG-005 magnet (fitted) | Framework PC-0029-2023, FOB Yantian, own part numbers (Annex C) | qualified |

Sources: `suppliers/*/profile.csv`, `suppliers/*/components.csv`, `suppliers/*/contracts/*`.

### 1.3 Entity model

```
FG-GH6-STD ──bom──> CMP-GAM-001 card, 003 case, 004 sleeve, 011 leaflet          (1 each)
FG-GH6-CE  ──bom──> CMP-GAM-001 card, 006 steel case, 007 box, 008 art book,
                    009 poster, 010 statue, 011 leaflet (1 each), CMP-MAG-005 (4, fitted inside 007)
FG-GH5-STD ──bom──> CMP-GAM-002, 003, 005, 011   (running product, shares case and leaflet with GH6)

component ──> supplier            via suppliers/*/components.csv (share, primary)
PO line (po_number + po_line) ──> supplier_id, material_id, plant DC-HAM
OC item 00010/00020 ──> PO line 1/2   (OC file name carries the PO number)
PackRight part no. ──> Soony part     via PackRight contract Annex C (PR-CC-0312=003, PR-SL-0604=004, PR-RB-2207=007, RM-N42-D0401=MAG-005)
shipment ──> po_numbers (pipe-separated), material_ids, route_id ──> routes.csv ──> carrier, transit days
GR ──> po_number/po_line, delivery_note ──> scan in soony/receiving/DC-HAM/<date>_<delivery_note>
stock batch = GR number (e.g. batch 5000413065 = GR 5000413065)
packout_log ──> FG stock; ERP component stock = sum(GR) − BOM × units packed (holds exactly for every GH6-only part)
```

Link notes:
- Legacy PO item 00010 maps to new-export line 1; cancellations are negative quantity lines numbered 51 (new) or 00510 (legacy). No PO number appears in both exports.
- Every GH6-only component satisfies `ERP stock = total GR − BOM qty × packed units` to the piece (my reconciliation of `goods_receipts.csv`, `packout_log.csv`, `stock_snapshot.csv`). So ERP stock is a computed book figure. The last physical counts are 2026-11-01 to 2026-11-16 (`stock_snapshot.csv`, column last_count). Any booking error flows straight into "available".

---

## 2. Contradictions between sources

| # | Topic | Side A | Side B | Impact |
|---|---|---|---|---|
| C1 | Art book quantity | `soony/erp/goods_receipts.csv` GR 5000413065: qty_ordered 8,000, qty_received 80,000, "over delivery accepted"; `stock_snapshot.csv` batch 5000413065 = 80,000 | `soony/receiving/DC-HAM/20261118_WZ-261116-473.jpg`: printed 80.000 struck through, handwritten "8 000" | Stock overstated by 72,000. Launch plan shows art book cover +66,100; real is -5,900. |
| C2 | Hwaseong air shipment | `launch_plan_GH6.xlsx` Inbound row 2 and `open_shipments.csv` SHP-2026-04311: 48,000, booked | `.../SUP-0010_Hwaseong_Memory/order_confirmations/Shipping_advice_020-48113256_4500318735.pdf`: 28,000 flown, 2 of 7 ULDs offloaded, balance 20,000 to Hamburg 2026-11-24 | Card gap 6,800 instead of +13,200. |
| C3 | Statue usability | `stock_snapshot.csv` batch 5000413063 (5,600, Pune) counted as available; GR 5000413063 has no remark | `soony/receiving/DC-HAM/inspection_report_5000413063.pdf`: 9 of 125 defective, reject number 4, "Lot does not meet AQL", booked unrestricted by mistake, blocking requested | 5,600 statues not usable without concession. |
| C4 | Sleeve artwork | `soony/master/components.csv` CMP-GAM-004 drawing_rev B; PackRight OCs and packing list: rev. B (`OA26182346_4500318738.pdf`, `OA26557315_4500318752.pdf`, `OA26869796_4500318765.pdf`, `OA26985895_4500318774.pdf`, `OA26540657_4500318786.pdf`, `PL_NHMU7718206_4500318752.pdf`) | Karton Nowak OCs: rev. C "GH6_sleeve_revC_USK2026.pdf" from Soony Marketing 2026-08-07 (`OA26293887_4500318775.pdf`, `OA26324887_4500318740.pdf`, `OA26344059_4500318754.pdf`, `OA26377328_4500318722.pdf`, `OA26670171_4500318793.pdf`, `OA26765704_4500318731.pdf`, `OA26808441_4500318792.pdf`, `OA26834129_4500318708.pdf`, `OA26837842_4500318768.pdf`, `OA26968662_4500318712.pdf`) | Two versions of the age-rating sleeve in stock and on order. Master data was not updated after Marketing released rev. C (**inference** from the file name and date). |
| C5 | Lid magnets | `launch_plan_GH6.xlsx` Parts row 13: need 100,800, stock 0, cover -100,800, "CHECK"; `soony/master/bom.csv` FG-GH6-CE → CMP-MAG-005 qty 4 | `suppliers/SUP-0029_Guangzhou_PackRight/components.csv` note "fitted by supplier into CMP-GAM-007; not ordered, received or stocked separately"; PackRight contract Annex A/B; PackRight OCs e.g. `OA26144355_4500318747.pdf` | False shortage. BOM should treat the magnet as part of the box (phantom item). |
| C6 | Boise Incoterm | All 81 Boise PO lines say "DDP Hamburg" (`soony/erp/purchase_orders_2025H2-2026.csv`, column incoterm) | Contract PC-0011-2023 § 4 / 4.1: EXW Boise, freight and duties for buyer (`suppliers/SUP-0011_Boise_Semi/contracts/SUP-0011_Sales_Agreement_2023.pdf`); every Boise OC: "DDP is not agreed. Freight, insurance and duties are prepaid by Seller and added to the invoice"; `OA26144214_4500318769.pdf`: shipment packed and on hold until Soony confirms EXW in writing | 36,000 cards on hold. Hidden freight cost on every Boise invoice. |
| C7 | Case container status | `soony/erp/open_shipments.csv` SHP-2026-04276: arrived_port, discharged at CTA Hamburg, updated 2026-11-20 10:00 | `open_shipments_tracker_v37_FINAL_final2.xlsx` row 2: "in transit", "RP: call terminal" | Tracker is stale; nobody may own the pick-up. |
| C8 | Shipment lists | `open_shipments.csv`: 25 shipments incl. air and road | Tracker: 14 sea shipments only; SHP-2026-04310, -04311, -04313, -04314, -04316, -04317, -04320, -04322, -04323, -04325, -04326 missing; SHP-2026-04281 "in transit" vs ERP "in_delivery" | Onboarding notes say "about 20 open shipments" tracked in the spreadsheet; the spreadsheet misses all air and road moves, i.e. exactly the launch-critical ones. |
| C9 | Air freight ETA to the DC | `launch_plan_GH6.xlsx` Inbound row 2: ETA 2026-11-21 | `open_shipments.csv` SHP-2026-04311 destination FRA, `routes.csv` RT-009 FRA → DC-HAM 1 day by road | **Inference:** the 28,000 cards reach Hamburg on 22.11, usable only on Sunday. Still in time because Saturday's packing is covered by stock (see 3.3). |
| C10 | Pune release status | `suppliers/SUP-0018_Pune_Diecast_Works/profile.csv`: qualification_status conditional, "Conditional release, see PP-07"; contract § 2.2: conditional release of 1 August 2026, serial supply needs PPAP and written release | Every Pune OC: "approved for serial supply to Soony (PPAP documents submitted 2026-09-14)" (e.g. `OA26218533_4500318760.pdf`) | Supplier claims an approval that does not exist. Submitted is not approved. |
| C11 | Pune conditional release expiry | PP-07 § 5: conditional release limited to 90 days; release 2026-08-01, so expired 2026-10-30 (**my date arithmetic**) | POs 4500318784 (2026-11-04) and 4500318791 (2026-11-12) correctly "awaiting_approval"; but 5,000 Pune statues are still at sea (SHP-2026-04303, ETA 2026-11-28) | Extension needs the Category Lead Collector's Items, which is vacant. |
| C12 | Price: Dongguan | Contract PC-0019-2018 Annex A: case 1.52, steel case 21.01, statue 83.31 CNY; no price-revision clause (`.../SUP-0019_.../contracts/SUP-0019_Supply_Agreement_2018.pdf`, page 2) | Price lists `notices/Price_list_20260701.xlsx` (+3 %) and `Price_list_20260824.xlsx` (+3 %); open POs at 1.61 / 22.29 / 88.38 | About CNY 867,000 above contract on open Dongguan PO lines (my calculation). |
| C13 | Price: PackRight | Contract PC-0029-2023 § 5: prices "fixed for the calendar year"; Annex A 1.38 / 0.3984 / 24.63 CNY | `notices/Price_list_20250624.xlsx` raised all three by 4 % from 2025-06-24, mid-year; all later POs at 1.44 / 0.4143 / 25.62 | About CNY 184,000 above Annex A on open lines. |
| C14 | Price: Hwaseong | LTA § 5.3: prices fixed until expiry; PO prices 5.40 USD | OCs at higher prices: `OA25863304_4500318555.pdf` 5.62, `OA26860304_4500318705.pdf` 5.56, `OA26876649_4500318641.pdf` 5.51 (GH5 cards) | Invoices may differ from PO; LTA breach. |
| C15 | Price: Boise | Contract Annex A 10.20 / 5.70 USD; increases need 60 days' written notice (§ 3) | POs at 10.93 / 6.11 after two price lists (`notices/Price_list_20250301.xlsx`, `Price_list_20250701.xlsx`); `OA25717212_4500318533.pdf` even quotes 6.29 vs PO 6.11 | Notice date not in data; cannot verify the 60 days. |
| C16 | PO promised vs OC date | PO 4500318744 line 1 promised 2026-12-21 | `OA26378326_4500318744.pdf` confirms 2026-12-28 | ERP promised date not updated from OC. Also PO 4500318636 promised 2026-05-26 vs OC 2026-06-09. |
| C17 | Weekend packing | `launch_plan_GH6.xlsx` Notes: "The team has worked through every weekend since." | `soony/erp/packout_log.csv`: no rows for 07./08.11 and 14./15.11 | Either no weekend packing or it was not logged. If unlogged, component stock is overstated by the unlogged consumption (**inference**). |
| C18 | Shipment positions | `open_shipments.csv` sea shipments Busan/Yantian → Hamburg via Singapore (SGSIN) | last_lat/last_lon put them on land, e.g. SHP-2026-04289 at 50.65 N 29.98 E (near Kyiv), SHP-2026-04284 at 51.60 N 22.51 E (eastern Poland) | **Inference:** positions are straight-line interpolations, not vessel tracking. Do not use them for ETA. |
| C19 | Delivery notes | `goods_receipts.csv` remarks and stock | scanned notes in `soony/receiving/DC-HAM/` | See section 2.1 for the full scan-by-scan comparison. |
| C20 | Unordered goods | No PO exists for PRM-GH6-PIN (not in `components.csv`, `bom.csv` or any PO export) | GRs 5000412998, 5000413025, 5000413052: 24,000 + 24,000 + 30,000 = 78,000 enamel "Pre-order Pin GH6" from Dongguan, booked to "misc merchandise"; scan `20261111_SN-261008-884.jpg` shows the PO field "采购订单" left empty (dash) | Someone promised a pre-order pin outside procurement, or Dongguan ships unordered goods. 78,000 is close to the 72,000 Collector's pre-orders, far below 487,000 total pre-orders (**inference**). |

### 2.1 Delivery notes vs goods receipts

I checked all 60 scans in `soony/receiving/DC-HAM/` against `soony/erp/goods_receipts.csv`, matching on delivery_note. The 60 notes cover 69 GR rows.

- 59 of 60 notes match their GR rows on material, quantity, PO, PO line and date. The exception is WZ-261116-473 (C1). Every note uses PCS. PackRight part numbers map correctly through Annex C. Every scan has a GR row.
- **11 GH6 GR rows since 2026-09-01 have no scan** (I checked this by delivery note number). The biggest: game cards DN-261102-986 (88,000, GR 5000413044) and DN-261106-613 (54,000, GR 5000413050); cases SN-260810-719 (195,000) and SN-260730-216 (134,000); sleeves WZ-261110-805 (74,000, which is still stock batch 5000413060), WZ-260829-339 (65,000) and WZ-261007-399 (22,600); steel cases SN-260730-216 (23,000) and SN-261007-130 (20,200, still stock batch 5000413048); art book WZ-260902-594 (10,500); poster WZ-261007-399 (34,000). For these rows the ERP quantity cannot be checked against paper. The onboarding notes say "Receiving books what is on the paper", but for these rows there is no paper in the data.
- **Hwaseong carrier and dates do not fit.** DN-261023-398, DN-261025-827, DN-261101-479 and DN-261112-486 name the sea line Nordhav Shipping. Yet GRs 5000413031, 5000413035, 5000413041 and 5000413062 were booked 4 to 5 days after the note date. For example, `20261116_DN-261112-486.jpg` is dated 2026.11.12, carrier Nordhav Shipping A/S, and was received 2026-11-16. `routes.csv` RT-001 Busan to Hamburg by sea takes 36 days. **Inference:** these went by air after the notes were printed (matching "freight rebooked from sea to air" in `onboarding_notes.md`). If so, no document shows the air-freight cost or who approved it.
- 14 notes have no receiver signature, but their GR rows still name an inspector (subagent count, not re-verified one by one).
- GR 5000413002 (SN-260820-788) records "carton wet, contents ok". The note itself has no damage remark, so this was a dock observation and is plausible.
- Gross weights on notes for the same part are not consistent, so they cannot be used to cross-check quantities.
- A small GH5 over-delivery: DN-261109-802, 5,640 received against 5,500 ordered (GR 5000413053).

---

## 3. Launch math

### 3.1 Inputs

- Need for the rest of the pack-out: 128,000 Standard and 25,200 Collector's (`launch_plan_GH6.xlsx`, Editions rows 4-5; `packout_log.csv` sums to 512,000 / 46,800).
- Per-unit usage from `soony/master/bom.csv`.
- On hand: `soony/erp/stock_snapshot.csv` (2026-11-20), corrected where a document contradicts it.
- In time: anything physically at DC-HAM before Sunday 20:00. Sea arrivals at DEHAM need one more day by road (`routes.csv` RT-011), air to FRA one more day (RT-009).
- Pack window: Saturday 06:00-22:00 (16 h) and Sunday 06:00-20:00 (14 h), no night shift (`launch_plan_GH6.xlsx`, Shifts).

### 3.2 Per component

| Component | Need | ERP stock | Correction (source) | In time (source) | Usable | Gap |
|---|---|---|---|---|---|---|
| CMP-GAM-001 game card | 128,000 + 25,200 = **153,200** | 118,400 | none known | +28,000 Hwaseong air, short-shipped (`Shipping_advice_020-48113256_4500318735.pdf`) | 146,400 | **-6,800** |
| CMP-GAM-003 case (STD) | 128,000 | 96,000 | none | +350,000 at CTA Hamburg, needs pick-up (SHP-2026-04276) | 446,000 if picked up, 96,000 if not | +318,000 / **-32,000** |
| CMP-GAM-004 sleeve (STD) | 128,000 | 186,000 = 122,000 rev. C (batches 5000413040, 5000413060, Karton) + 64,000 rev. B (batch 5000413064, PackRight) | artwork validity open (C4) | +20,000 rev. C Karton, dispatch 21.11, delivery same evening (`OA26293887_4500318775.pdf`, SHP-2026-04313) | rev. C only: 142,000; rev. B only: 64,000; both: 206,000 | +14,000 / **-64,000** / +78,000 |
| CMP-GAM-011 leaflet | 153,200 | 420,000 | none | none needed | 420,000 | +266,800 |
| CMP-GAM-006 steel case (CE) | 25,200 | 31,400 | none | none | 31,400 | +6,200 |
| CMP-GAM-007 Collector's box (CE) | 25,200 | 27,500 | none | none | 27,500 | +2,300 |
| CMP-MAG-005 magnet (CE) | 0 (fitted in 007) | 0 | BOM line is a phantom (C5) | n/a | n/a | 0 |
| CMP-GAM-008 art book (CE) | 25,200 | 91,300 | -72,000 (C1) → 19,300 | +8,000 only if Soony accepts the Karton offer today | 19,300 / 27,300 | **-5,900** / +2,100 |
| CMP-GAM-009 poster (CE) | 25,200 | 40,000 | none | none | 40,000 | +14,800 |
| CMP-GAM-010 statue (CE) | 25,200 | 27,400 | -5,600 failed AQL (C3) → 21,800 | none: Dongguan 10,000 ETA 27.11 (SHP-2026-04292), Pune 5,000 ETA 28.11 (SHP-2026-04303) | 21,800; about 27,000 with a sorted concession lot | **-3,400** / about +1,800 |

Statue concession estimate (**inference**): the sample had 9 defects in 125 (7.2 %). If the 5,600 lot is 100 % sorted and the same rate holds, about 5,200 good statues remain, giving about 27,000 usable.

Not in time for launch (all after Sunday 20:00 per the data): Boise 36,000 cards on hold, flight ETA FRA 23.11 (SHP-2026-04314, `OA26144214_4500318769.pdf`); Hwaseong balance 20,000, Hamburg 24.11 (shipping advice); Hwaseong sea 240,000, ETA DEHAM 24.11 (SHP-2026-04284); PackRight sleeves 150,000 rev. B, ETA 03.12 (SHP-2026-04299).

### 3.3 What can actually be packed

Cards are shared. Every Collector's Edition packed takes one card from Standard. Collector's is the binding constraint on the CE side; cards on the Standard side (assuming the case container arrives and rev. C sleeves are valid).

| Scenario | CE extra possible | CE total (of 72,000) | Cards left for STD | STD total (of 640,000) |
|---|---|---|---|---|
| A. No action | min(art book 19,300, statue 21,800, box 27,500, steel 31,400, poster 40,000) = 19,300 | 66,100 (-5,900) | 146,400 − 19,300 = 127,100 | 639,100 (-900) |
| B. Accept Karton art-book offer | statue limits: 21,800 | 68,600 (-3,400) | 124,600 | 636,600 (-3,400) |
| C. B + statue concession (sorted Pune lot) | 25,200 | **72,000** | 121,200 | **633,200 (-6,800)** |

Scenario C ships every numbered, pre-ordered Collector's Edition and leaves Standard 218,200 above its 415,000 pre-orders (`launch_plan_GH6.xlsx`, Editions row 4). **Inference:** C is the right priority call, but it is Dr. Vogt's decision.

Timing check:
- Saturday 16 h: Standard 6,000/h × 16 = 96,000 units, CE 900/h × 16 = 14,400. Cards needed 110,400 ≤ 118,400 in stock. Cases needed 96,000 = 96,000 in stock, zero margin. Art books needed 14,400 ≤ 19,300. Statues needed 14,400 ≤ 21,800. Saturday works on stock alone.
- Sunday 14 h: Standard needs 32,000 more (5.3 h), CE 10,800 more (12 h). Needs the case container, the 28,000 air cards, the Karton art books and the statue concession by Sunday morning.
- Capacity: CE needs 25,200 / 900 = 28 h of 30 h (`launch_plan_GH6.xlsx`, Editions row 5). The best CE day so far was 6,800 (`packout_log.csv`, 2026-11-19). Two hours of slack cover 1,800 units.

### 3.4 Bottleneck

- **Supplier: Hwaseong Memory** (CMP-GAM-001). Primary card source, short shipment on the last in-time flight, and the worst delivery record in the PO data: 34 of 68 deliveries since mid-2025 late, average 4.2 days, worst 17 days (my calculation from `purchase_orders_2025H2-2026.csv`, actual_delivery_date vs promised_date).
- **Collector's side: art book (data error, fixable today) then statue (quality, fixable by concession).** Both blocked by people and approvals, not by physical supply.

---

## 4. Hidden issues and risks

### Launch-critical (next 30 hours)

1. **Art book phantom stock** (C1). Fix: accept Karton offer item 2 (8,000 at Annex A price 2.10 EUR, about EUR 16,800, within the buyer limit < EUR 50,000 per PP-07 § 2). Needs a booked dock slot from Katrin Jansen or Raj Patel (PP-07 § 3). Offer valid 24 h from 21.11. Karton's bindery is down, but shipping from its finished-goods store is allowed (`Zawiadomienie_przestoj_techniczny_2026-11.pdf`, row Introligatornia).
2. **Statue concession needs a signature the org chart cannot provide.** Inspection report requires Supplier Quality (Daniel Okafor) and the Category Lead; the Category Lead Collector's Items is TBD. PP-07 § 6: exceptions go to the Head of Procurement. **Inference:** Dr. Vogt has to sign together with Okafor.
3. **Boise hold** (C6). Even if released now, the cards land after launch. It matters for day-two replenishment and every future Boise order.
4. **Case container pick-up** (C7). FOB Yantian means Soony handles import clearance (**inference** from Incoterm; customs status is not in the data). Needs a truck (RT-011, Nordhav) and a dock slot.
5. **Rush printing is not a rescue.** Contract § 3.2 promises 50,000 in 36 h, but the downtime notice limits rush to 15,000 on the digital line from 21.11 14:00, and offset rush orders closed 21.11 09:00. 36 h from now plus 9 h of driving lands after 20:00 Sunday.
6. **Sleeve artwork / age rating** (C4). Packing rev. B and rev. C side by side ships two versions of the USK/PEGI sleeve. Karton's contract § 6.2 rejects parts printed to superseded artwork. PackRight's contract § 6.2 binds it to the drawing revision in the master data at PO date, which is still B. **Inference:** if rev. C is the current rating artwork, 64,000 rev. B in stock plus 675,000 rev. B on open PackRight POs (4500318752, 4500318765, 4500318774, 4500318786) are scrap, and PackRight is contractually not at fault.

### Structural (after launch, but real)

7. **Single sources.** Steel case (Dongguan, 100 %), Collector's box (PackRight, 100 %), art book, poster and leaflet (Karton Nowak, 100 % each), per `suppliers/*/components.csv`. The statue's second source (Pune) is conditional and its first lot failed. Game cards: two sources, but Boise is blocked.
8. **Rare-earth magnet sub-supplier.** All lid magnets come from Jiangxi RareMag Materials, Ganzhou (PackRight contract Annex B). PackRight's force-majeure clause explicitly covers "governmental export restrictions on rare-earth materials" (§ 8). So an export restriction stops every Collector's box and PackRight owes nothing.
9. **Hwaseong LTA expiry.** Expires 2026-12-31; renewal needs a signed amendment by 2026-12-15; without it, deliveries switch to list prices, NET 30, no capacity reservation (`SUP-0010_LTA_2022.pdf`, § 12). Open Hwaseong lines promised for 2027: 9 lines, USD 5.41 m (my sum from `purchase_orders_2025H2-2026.csv`).
10. **Chinese New Year.** Dongguan may close for up to 12 days and must announce the schedule by 30 November, then shifts confirmed dates (§ 3.2 of the scanned contract). Open Dongguan statue and steel-case lines run into January and February 2027. **Inference** from general knowledge: Chinese New Year 2027 falls in early February.
11. **Penalty clauses are weak where it hurts.** Hwaseong 2 % per week, capped at 10 %; Dongguan, Karton, PackRight and Pune 0.5 % per week, capped at 5 %; Boise liability limited to the price of the delayed goods, and its force majeure explicitly includes "shortages of materials, carrier delays" (`SUP-0011_Sales_Agreement_2023.pdf`, §§ 7-8). Nothing covers a missed launch.
12. **Approval limits vs PO values.** PP-07: buyer < EUR 50,000, Head of Procurement < 2 m, CPO < 5 m. Eleven open game-card PO lines are worth roughly EUR 0.5 m to 2.2 m each (e.g. 4500318733 line 5: 240,000 × 9.80 USD = USD 2.35 m; EUR figures use my assumed rate of 0.92 EUR/USD), all with buyer "Felix Brunner" in the export. The exports have no approver field. **Inference:** either approvals live outside SAP or POs are released above limit; ask.
13. **Air-freight approvals.** Unplanned air freight up to EUR 200,000 per month and category needs the Category Lead (PP-07 § 4). For Collector's Items that role is vacant.
14. **Supplier statements that contradict Soony's own records:** Pune claims serial approval (C10); Hwaseong, Dongguan and Karton contracts each say "no sub-supplier provides a critical material" (§ 9), while PackRight names one.
15. **Data quality traps.** Two PO exports with different delimiters, decimal separators, date formats, supplier ID formats and status codes; negative cancellation lines; ERP stock is a computed figure that inherits booking errors (C1); GR remarks such as "over delivery accepted" hide typos; safety stock and weeks of cover in `stock_snapshot.csv` use plan_weekly_units from `products.csv` and say nothing about launch need; gaps in GR numbering (5000413010, 5000413036, 5000413058, 5000413061 missing) suggest reversals not in the export (**inference**); two contracts and one packing list have no text layer; PackRight uses its own part numbers; shipment positions are synthetic (C18).

---

## 5. "The system says we are fine, my gut says we are not"

The gut is right, and the data shows why.

- The launch plan marks every part "ok" except magnets (`launch_plan_GH6.xlsx`, Parts). The one "CHECK" is false (C5). Three of the "ok" rows are wrong: cards (+13,200 shown, -6,800 real), art books (+66,100 shown, -5,900 real), statues (+2,200 shown, -3,400 real).
- The system is not lying on purpose. It is a book calculation (receipts minus BOM backflush) that only knows what someone typed. Every error found here came from paper or e-mail that never reached SAP: a handwritten correction on a delivery note, a short-shipment advice in Korean, an inspection report, a Polish downtime notice, a US supplier's refusal printed at the bottom of an acknowledgement.
- Maren's "we find out at the dock" (`onboarding_notes.md`) is literally what happens: the documents arrived, but nobody read them, joined them to the PO, and updated the plan.

Where AI agents could do real work (**inference**, for the product decision):
1. **Document reader and reconciler.** Read every OC, shipping advice, delivery note scan and notice in any language; map supplier part numbers to Soony IDs; compare to PO, GR and stock; raise a discrepancy with both sources cited. C1 to C4, C6 and C10 would all have surfaced on the day the document arrived.
2. **Launch-gap calculator.** Recompute the per-component table in section 3 every time a document lands, using corrected stock, true arrival at the dock (incl. last-mile legs), and quality blocks.
3. **Supplier chaser.** Draft and send (with approval) the follow-ups that are pending right now: confirm EXW to Boise, accept the Karton art-book offer, ask Hwaseong for the balance flight, ask Dongguan for the Chinese New Year schedule.
4. **Approval router.** Know who can sign what under PP-07, notice that a required signer is vacant, and route to the fallback (Head of Procurement under § 6).

---

## 6. Open questions for Soony

**Dr. Maren Vogt, Head of Procurement (money, priorities, approvals)**
1. If cards run short, do Collector's Editions take priority over Standard? (Scenario C costs 6,800 Standard units and ships all 72,000 Collector's.)
2. Will you sign the concession for the 5,600 Pune statues (GR 5000413063) with Daniel Okafor, given the Category Lead role is vacant?
3. May Paul Winter accept Karton Nowak's 8,000 art books today (about EUR 16,800)?
4. Will Soony confirm EXW Boise in writing to release 36,000 cards, and accept the freight Boise has been adding to invoices?
5. What does a missing copy cost on day one, per edition? Is there a retail penalty?
6. Who approved PO lines above EUR 2 m (e.g. 4500318733 line 5), and where is that approval recorded?
7. Is the Hwaseong LTA renewal (deadline 2026-12-15) in progress?
8. Who ordered 78,000 "Pre-order Pin GH6" from Dongguan, and do they go into a box?

**Felix Brunner, Senior Buyer (suppliers, trucks, stock)**
1. Who picks up container NHSU4471921 at CTA, is it cleared, and is a dock slot booked for Saturday?
2. Can Hwaseong get the 20,000 balance cards on any flight before Sunday morning, e.g. via another carrier or airport?
3. Has anyone physically counted game cards and art books since 2026-11-10/11? Counting 118,400 cards and 19,300 art books takes an hour and settles the plan.
4. Is the Karton truck with 20,000 sleeves (SHP-2026-04313) on the road, and was it loaded before the 14:00 shutdown?
5. Which sleeve is correct, rev. B or rev. C (USK2026)? Who at Marketing released rev. C on 2026-08-07, and was Clara Weiss involved?
6. Were the 64,000 rev. B sleeves (batch 5000413064) already used in packed Standard units?

**Nils Berger, IT lead (data and exports, precise questions)**
1. Is ERP component stock purely GR minus BOM backflush at pack-out, with no cycle-count adjustment since `last_count`? Which transaction posts the backflush?
2. Were any pack-outs on 07./08.11, 14./15.11 or 20.11 not posted to `packout_log`?
3. GR numbers 5000413010, 5000413036, 5000413058 and 5000413061 are missing from the export. Reversals?
4. Can the ERP record a quality block on a batch (stock type), and why is batch 5000413063 unrestricted?
5. Can CMP-MAG-005 be flagged as a phantom item in the GH6-CE BOM so it stops showing up as a shortage?
6. Can you export PO approvals (release strategy and approver) and OC dates as stored in SAP, if any?
7. Where do `last_lat` / `last_lon` in `open_shipments.csv` come from?
