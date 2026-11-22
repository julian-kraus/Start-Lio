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


def test_the_statement_that_made_the_promise_does_not_settle_it():
    # The citing statement is in every ledger run (read_statements returns every current statement). A confirm
    # that made the promise must not also prove it, or a later cancellation could never break it (demo path).
    said = Statement(kind="confirm", at=MADE, supplier_id="SUP-A", part_id="PART-A", fact_id="fact-1")
    cancel = Statement(kind="cancel", at="2030-01-11T19:00:00+00:00", supplier_id="SUP-A", part_id="PART-A",
                       fact_id="fact-2")
    p = a_promise(sources=[{"fact_id": "fact-1"}])                  # rests on the confirm, fact-1
    [upd] = promises.reconcile([p], [said, cancel])
    assert upd["state"] == "broken" and upd["source"] == {"fact_id": "fact-2"}
    assert promises.reconcile([p], [said]) == []                   # the confirm alone proves nothing
    assert promises.review([p], [said]) == []                      # nor is it flagged for a human


def test_the_citing_fact_is_recognised_across_source_shapes():
    # A decisive statement (dispatch_info) that made the promise still must not settle it, however its citation
    # was stored by the register tool.
    said = Statement(kind="dispatch_info", at=MADE, supplier_id="SUP-A", part_id="PART-A", fact_id="fact-1")
    for shape in ("fact-1", {"fact_id": "fact-1"}, {"fact_id": "fact-1", "label": "supplier_statement"}):
        p = a_promise(sources=[shape])
        assert promises.reconcile([p], [said]) == []               # the maker never settles, however it is stored


def test_citing_another_fact_of_the_same_email_does_not_self_prove():
    # An email yields several facts (the kind row and, say, a value row). A promise registered against one fact of
    # the email must not be proven by the dispatch statement of that same email cited through another of its facts
    # (finding: the self-proof returned when another fact of the statement was cited).
    said = Statement(kind="dispatch_info", at=MADE, supplier_id="SUP-A", part_id="PART-A",
                     fact_id="kind-fact", fact_ids=("kind-fact", "value-fact"))
    p = a_promise(sources=[{"fact_id": "value-fact"}])             # rests on another fact of the same email
    assert promises.reconcile([p], [said]) == []
    assert promises.review([p], [said]) == []


def test_a_confirm_is_not_proof_of_loading():
    # A confirm restates the commitment; it is not evidence the goods loaded, so it leaves the promise waiting and
    # a later cancellation still breaks it (finding: a check-in confirm closed the promise before a cancellation).
    confirm1 = Statement(kind="confirm", at=MADE, supplier_id="SUP-A", part_id="PART-A", fact_id="f1")
    confirm2 = Statement(kind="confirm", at="2030-01-11T18:00:00+00:00", supplier_id="SUP-A",
                         part_id="PART-A", fact_id="f2")
    cancel = Statement(kind="cancel", at="2030-01-12T05:00:00+00:00", supplier_id="SUP-A",
                       part_id="PART-A", fact_id="f3")
    p = a_promise(sources=[{"fact_id": "f1"}])
    [upd] = promises.reconcile([p], [confirm1, confirm2, cancel])
    assert upd["state"] == "broken" and upd["source"] == {"fact_id": "f3"}
    assert promises.reconcile([p], [confirm2]) == []              # a confirm alone proves nothing


def test_a_supplier_less_statement_does_not_settle_and_is_flagged():
    # A statement with no supplier could be any supplier's, so it must not settle a promise on a shared part; it is
    # surfaced for a human instead (finding: a supplier-less statement broke an unrelated supplier's promise).
    p = a_promise()
    st = Statement(kind="dispatch_info", at="2030-01-11T17:00:00+00:00", part_id="PART-A", fact_id="f")
    assert promises.reconcile([p], [st]) == []
    [flag] = promises.review([p], [st])
    assert flag["reason"] == "supplier_unknown" and flag["candidates"] == ["PR-1"]
    assert flag["source"] == {"fact_id": "f"}


def test_a_statement_that_shares_no_key_never_matches():
    p = a_promise()
    st = Statement(kind="dispatch_info", at="2030-01-11T17:00:00+00:00", fact_id="f")   # no supplier, no part, no lane
    assert promises.reconcile([p], [st]) == []


def test_a_supplier_only_statement_over_several_parts_settles_none_and_is_flagged():
    ps = [a_promise(promise_id="PR-A", part_id="PART-A"),
          a_promise(promise_id="PR-B", part_id="PART-B"),
          a_promise(promise_id="PR-C", part_id="PART-C")]
    st = Statement(kind="dispatch_info", at="2030-01-11T17:00:00+00:00", supplier_id="SUP-A", fact_id="f")
    assert promises.reconcile(ps, [st]) == []                      # ambiguous: it does not prove all three
    [flag] = promises.review(ps, [st])
    assert flag["reason"] == "ambiguous" and set(flag["candidates"]) == {"PR-A", "PR-B", "PR-C"}


def test_a_part_only_statement_over_two_suppliers_settles_none():
    ps = [a_promise(promise_id="PR-A", supplier_id="SUP-A", part_id="PART-X"),
          a_promise(promise_id="PR-B", supplier_id="SUP-B", part_id="PART-X")]
    st = Statement(kind="on_hold", at="2030-01-11T16:00:00+00:00", part_id="PART-X", fact_id="f")
    assert promises.reconcile(ps, [st]) == []
    [flag] = promises.review(ps, [st])
    # The statement names no supplier, so it is not applied; a human decides which supplier's promise it bears on.
    assert flag["reason"] == "supplier_unknown" and set(flag["candidates"]) == {"PR-A", "PR-B"}


def test_a_proof_after_the_due_time_is_marked_late():
    p = a_promise()                                                # due 2030-01-11 18:00
    st = Statement(kind="dispatch_info", at="2030-01-13T17:00:00+00:00", supplier_id="SUP-A",
                   part_id="PART-A", fact_id="f")
    [upd] = promises.reconcile([p], [st])
    assert upd["state"] == "proven" and upd["late"] is True
    on_time = Statement(kind="dispatch_info", at="2030-01-11T17:00:00+00:00", supplier_id="SUP-A",
                        part_id="PART-A", fact_id="g")
    assert "late" not in promises.reconcile([p], [on_time])[0]


def test_an_unreadable_statement_time_is_isolated_not_raised():
    p = a_promise()
    good = Statement(kind="dispatch_info", at="2030-01-11T17:00:00+00:00", supplier_id="SUP-A",
                     part_id="PART-A", fact_id="good")
    bad = Statement(kind="on_hold", at="2030-01-11", supplier_id="SUP-A", part_id="PART-A", fact_id="bad")
    [upd] = promises.reconcile([p], [bad, good])                   # does not raise on the date-only time
    assert upd["state"] == "proven" and upd["source"] == {"fact_id": "good"}
    [flag] = promises.review([p], [bad, good])
    assert flag["reason"] == "unreadable_time" and flag["source"] == {"fact_id": "bad"}


def test_watch_gaps_flags_unwatched_open_promises_only():
    good = a_promise()
    no_check = {"promise_id": "PR-2", "state": "waiting", "fallback": "x"}
    no_fallback = {"promise_id": "PR-3", "state": "waiting", "next_check": MADE, "fallback": " "}
    settled = {"promise_id": "PR-4", "state": "proven"}                    # not open: not a gap
    assert promises.watch_gaps([good, no_check, no_fallback, settled]) == ["PR-2", "PR-3"]


@pytest.mark.parametrize("kind", ["cancellation: our press is not available", "Cancelled", "CANCEL - press down",
                                  "cancellation"])
def test_a_cancellation_in_the_models_own_words_breaks_the_promise(kind):
    # Rehearsal run 4: the model wrote the kind as a sentence, the ledger read it as informational and the promise
    # was never broken. The kind is normalised in code by its leading word.
    said = Statement(kind="confirm", at=MADE, supplier_id="SUP-A", part_id="PART-A", fact_id="fact-1")
    cancel = Statement(kind=kind, at="2030-01-11T19:00:00+00:00", supplier_id="SUP-A", part_id="PART-A",
                       fact_id="fact-2")
    [upd] = promises.reconcile([a_promise(sources=[{"fact_id": "fact-1"}])], [said, cancel])
    assert upd["state"] == "broken"


@pytest.mark.parametrize("kind,canonical", [("Confirmation: we load within 4 h", "confirm"),
                                            ("delay - two days", "delay"), ("on hold", "on_hold"),
                                            ("price question", "price question")])
def test_kinds_are_normalised_to_the_closed_set_and_unknown_kinds_stay_informational(kind, canonical):
    assert Statement(kind=kind, at=MADE).kind == canonical
    assert promises.reconcile([a_promise()], [Statement(kind="price question", at="2030-01-11T19:00:00+00:00",
                                                        supplier_id="SUP-A", fact_id="f-9")]) == []
