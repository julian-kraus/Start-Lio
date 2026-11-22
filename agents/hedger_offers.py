"""Which offers the hedger refuses before it prices them. A refused offer never reaches a signer or a payment."""

from __future__ import annotations

from typing import Collection, Mapping

from sourcing.hedge import ModelError


def check(spot: Mapping, offer: Mapping, unqualified: Collection[str] = ()) -> None:
    """Raise with the reason. A backup that is not qualified fails closed: the facts need not hold the date its
    release ran out, and the policy holds an order placed after that date. `unqualified` adds the suppliers the
    weak spots of other parts list that way, as qualification is stated for the supplier."""
    supplier = str(offer["supplier_id"])
    if supplier in {str(s) for s in [*spot["detail"]["not_qualified"], *unqualified]}:
        raise ModelError(f"{supplier} is not qualified on file, so a person must settle its qualification or "
                         "release before anyone signs or pays")
    if offer.get("fixed_cost_eur"):
        raise ModelError("the hedge rule cannot price an offer with a fixed cost")
    sourced = any(isinstance(ref, dict) for ref in offer.get("sources") or [])
    if not sourced or offer.get("quantity") is None or offer.get("unit_price_eur") is None:
        raise ModelError("the offer needs a quantity, a price and their source")
    if offer["quantity"] < (offer.get("min_quantity") or 0):
        raise ModelError("the offer is below its own minimum quantity")
