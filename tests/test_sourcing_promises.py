"""The promise ledger as pure functions: a statement becomes a watched promise with a check and a fallback,
later statements prove or break it, and every open promise stays watchable. Synthetic values only."""

import pytest

from plan import contracts
from sourcing import promises
from sourcing.promises import PromiseError, Statement

MADE = "2030-01-11T14:00:00+00:00"


def a_promise(**over):
    kw = dict(promise_id="PR-1", supplier_id="SUP-A", what="loads the backup order", made_at=MADE,
              within="within 4 h", fallback="call the supplier contact", part_id="PART-A",
              sources=[{"fact_id": "fact-1"}])
    kw.update(over)
    return promises.promise_from_statement(**kw)


def test_statement_within_becomes_a_promise_with_check_and_fallback():
    p = a_promise()
    assert p["state"] == "waiting"
    assert p["due"] == "2030-01-11T18:00:00+00:00"          # made + 4 h, computed in code
    assert p["next_check"] == "2030-01-11T18:00:00+00:00"   # check at the due time (+4 h)
    assert p["fallback"] == "call the supplier contact"
    assert p["sources"] == [{"fact_id": "fact-1"}]
    contracts.validate("promise", p)


def test_grace_moves_the_check_after_due():
    p = a_promise(grace_h=1.5)
    assert p["due"] == "2030-01-11T18:00:00+00:00"
    assert p["next_check"] == "2030-01-11T19:30:00+00:00"


def test_absolute_due_is_accepted_instead_of_a_duration():
    p = a_promise(within=None, due="2030-01-12T09:00:00+00:00")
    assert p["due"] == "2030-01-12T09:00:00+00:00"
    assert p["next_check"] == "2030-01-12T09:00:00+00:00"


def test_a_promise_without_a_fallback_is_refused():
    with pytest.raises(PromiseError, match="fallback"):
        a_promise(fallback="  ")


def test_a_promise_needs_a_due_time():
    with pytest.raises(PromiseError, match="due time"):
        a_promise(within=None)


def test_a_dispatch_statement_proves_the_promise():
    p = a_promise()
    st = Statement(kind="dispatch_info", at="2030-01-11T17:00:00+00:00", supplier_id="SUP-A",
                   part_id="PART-A", fact_id="fact-9")
    [upd] = promises.reconcile([p], [st])
    assert upd["promise_id"] == "PR-1" and upd["state"] == "proven"
    assert upd["proven_at"] == "2030-01-11T17:00:00+00:00" and upd["source"] == {"fact_id": "fact-9"}
    after = promises.apply_update(p, upd)
    assert after["state"] == "proven" and after["sources"][-1] == {"fact_id": "fact-9"}


def test_a_hold_statement_breaks_the_promise():
    p = a_promise()
    st = Statement(kind="on_hold", at="2030-01-11T16:00:00+00:00", supplier_id="SUP-A", fact_id="fact-7")
    [upd] = promises.reconcile([p], [st])
    assert upd["state"] == "broken" and upd["broken_at"] == "2030-01-11T16:00:00+00:00"


def test_informational_wrong_supplier_and_stale_statements_are_ignored():
    p = a_promise()
    sts = [
        Statement(kind="price_deviation", at="2030-01-11T17:00:00+00:00", supplier_id="SUP-A"),  # informational
        Statement(kind="on_hold", at="2030-01-11T17:00:00+00:00", supplier_id="SUP-B"),          # other supplier
        Statement(kind="dispatch_info", at="2030-01-11T13:00:00+00:00", supplier_id="SUP-A"),    # before it was made
    ]
    assert promises.reconcile([p], sts) == []


def test_the_earliest_decisive_statement_wins():
    p = a_promise()
    sts = [
        Statement(kind="dispatch_info", at="2030-01-11T17:30:00+00:00", supplier_id="SUP-A", fact_id="late"),
        Statement(kind="on_hold", at="2030-01-11T15:00:00+00:00", supplier_id="SUP-A", fact_id="early"),
    ]
    [upd] = promises.reconcile([p], sts)
    assert upd["state"] == "broken" and upd["source"] == {"fact_id": "early"}


def test_a_statement_matches_on_part_when_the_supplier_is_unknown():
    p = a_promise()
    st = Statement(kind="dispatch_info", at="2030-01-11T17:00:00+00:00", part_id="PART-A", fact_id="f")
    [upd] = promises.reconcile([p], [st])
    assert upd["state"] == "proven"


def test_a_statement_that_shares_no_key_never_matches():
    p = a_promise()
    st = Statement(kind="dispatch_info", at="2030-01-11T17:00:00+00:00", fact_id="f")   # no supplier, no part, no lane
    assert promises.reconcile([p], [st]) == []


def test_watch_gaps_flags_unwatched_open_promises_only():
    good = a_promise()
    no_check = {"promise_id": "PR-2", "state": "waiting", "fallback": "x"}
    no_fallback = {"promise_id": "PR-3", "state": "waiting", "next_check": MADE, "fallback": " "}
    settled = {"promise_id": "PR-4", "state": "proven"}                    # not open: not a gap
    assert promises.watch_gaps([good, no_check, no_fallback, settled]) == ["PR-2", "PR-3"]
