"""Supplier records straight from the promise history.

`sourcing.reliability` turns PO lines, goods receipts and inspection reports into one Lookahead `supplier`
record per supplier, with every count and the fact ids behind the score. Deterministic: the same facts give
the same records, and no value is typed here.
"""

from __future__ import annotations

from collections.abc import Iterable

from sourcing import reliability


def records(current_facts: Iterable[dict], believed: Iterable[dict] = (),
            promises: Iterable[dict] = (), scenario_priors: dict | None = None) -> list[dict]:
    """A `supplier` record for every supplier known to the history, the promise ledger or a labelled prior.

    `current_facts` are raw `kb.current_facts` rows; `believed` are the `received_qty` believed values (a signed
    delivery note correcting a booking). `promises` are promise-ledger entries that add kept/broken outcomes;
    `scenario_priors` are labelled starting points for a supplier without history.
    """
    history = reliability.history(list(current_facts), list(believed))
    return reliability.cards(history, list(promises), scenario_priors)
