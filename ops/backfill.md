# Backfill: drain the live extraction queue on L1

For a human on L1. Agents never run this: it writes to the live database and pays for model calls. It does not send
email and does not touch approvals or the send mode.

Run everything in one bash or zsh shell, from the repo root of the clone that holds `data/`, `mail_data/` and the
credential files.

## 0. Preconditions

- The checkout has #7 (the extraction adapter), #65 (kb reads mail), #78 (kb run follows portal events), #89 (kb run
  reconciles a failure) and #66 (a document of no kind is tried once). Before #65, `kb sync` deletes mail documents
  and `kb extract` loses their sender label. Before #78, a running worker does not see new portal files. Before #66,
  the steps below pay again for every document of no kind. All three checks must print ok:

```sh
git pull --ff-only
python -m pip install -r requirements.txt
python -c "import kb.mail_source; print('ok')"
python -c "import kb.llm.anthropic; print('ok')"
git merge-base --is-ancestor 504b7b7 HEAD && echo ok     # #78, #89, #66
```

- The API key is in its file as described in AGENTS.md. The adapter reads it itself: do not export it or print it.
- The commands below are for kb running as a local process against the shared database. The compose `kb` service
  uses the local `db` service, because `docker-compose.supabase.yml` overrides only tracker and web.

## 1. Define the command

`kbl` is `python -m kb` on the shared database. It does not print the connection string.

```sh
kbl() {
  python -c 'import runpy; from pathlib import Path; from tracker.config import load_env; p = Path("secrets/supabase.env"); p.is_file() or exit(f"missing {p}"); load_env(p); runpy.run_module("kb", run_name="__main__", alter_sys=True)' "$@"
}
ADAPTER=kb.llm.anthropic:AnthropicClient
```

A `DATABASE_URL` that is already set in the shell wins over the file. Run `unset DATABASE_URL` first.

## 2. Stop the kb worker

`kb extract` does not take the worker lock. Two drains at once pay twice for the same document.

```sh
pgrep -fl "kb.* run"     # the worker's pid
kill <pid>               # SIGTERM: it finishes its round and exits
pgrep -fl "kb.* run"     # must print nothing
```

## 3. Queue what is missing

```sh
kbl migrate
kbl sync                 # portal files kb has not processed; mail documents are left alone
kbl reconcile --by "<your name>"
kbl status               # note queue.pending
```

## 4. One document first

```sh
kbl extract --adapter "$ADAPTER" --limit 1
kbl status
```

Read the first block of the output. The first line that matches says what to do:

| output | next |
|---|---|
| `failed` above 0 | read the warning, fix the cause (key, network, missing package), run again with the same limit |
| `done` or `needs_review` above 0 | go on to step 5 |
| `mail_parked` above 0 | mail that fits no kind left the queue: run again with the same limit |
| `unrouted` above 0 | no kind fits that document, it is now set aside: run again with the same limit |
| every count 0 | nothing is left to try: go to step 6 |

Every failed run uses up one of the document's three attempts, and no command puts a failed document back into the
queue. If every count is 0 while `queue.pending` is above 0, no queued document can be routed: report that on #36.

## 5. Drain

Keep `--limit` above `queue.pending`, so that each run reaches every document that is still to try. A document no
kind fits is read once and then skipped until the doc kinds or the adapter change.

```sh
for i in 1 2 3; do kbl extract --adapter "$ADAPTER" --limit 1000; done
kbl status
```

Three runs, because a document that errors is tried three times. Answers are cached, so a document that was
answered is not paid for again.

## 6. Read the result

The drain is over when `queue` in `kbl status` has no `pending`. **That does not prove that every document became
facts or an issue** (see the known gaps below).

| `queue` state | meaning | where to look |
|---|---|---|
| `done` | the answer passed the gate, it can hold 0 facts | `kbl status`, `fact_types` |
| `review` | the answer failed the gate, its facts do not count | `kbl issues --kind review_needed --limit 1000`, `kbl review` |
| `failed` | three errors, or mail that fits no kind | `kbl issues --kind extraction_failed --limit 1000` |
| `pending` | no known kind fits | `kbl issues --kind pending_extraction --limit 1000` |

Tables never enter the queue. A new or renamed layout is a `needs_schema_map` document:

```sh
kbl issues --kind schema_map_needed --limit 1000
kbl issues --kind ingest_failed --limit 1000        # files kb could not read
kbl issues --kind promotion_needed --limit 1000     # structures the designer proposed
kbl explain <issue_key>                             # the detail behind one line
```

If `pending` is left after step 5 and one more run prints every count 0, do not loop on it: those documents fit no
kind and wait for one. Post the count on the backfill issue (#36).

Known gaps. These end with no facts and no issue, also after a reconcile. No list above shows them and no kb
command finds them, so an empty queue does not prove them handled:

| input | document status | queue |
|---|---|---|
| JSON without a list of records: one object, `[]`, `{"items": []}`, a list of strings (#79) | `loaded` | none |
| empty CSV (#126) | `loaded` | none |
| CSV of a known layout with only a header (#126) | `parsed` | none |
| CSV of a known layout where no row has the subject column's value (#126) | `parsed` | none |
| document whose answer or reference envelope holds 0 facts (#126) | `loaded` | `done`, or none with an envelope |

When you report (step 8), say that these were not checked.

## 7. Restart the worker, one instance

```sh
mkdir -p exports/logs
kbl run >> exports/logs/kb-run.log 2>&1 &                                  # no adapter: new documents queue
KB_LLM_ADAPTER="$ADAPTER" kbl run >> exports/logs/kb-run.log 2>&1 &        # or: extracts as they arrive
```

Run one of the two lines, not both. The second does not pay again on every poll for what step 6 left pending: it
tries such a document again only after the doc kinds or the adapter change.

Either worker sees new portal files by itself, so a drop needs no `kbl sync`. The first only queues what needs the
model: drain it with steps 2, 5 and 7. The second extracts it, so do not run `kbl extract` next to it (step 2).

## 8. Report

Post on #36 the `queue` counts from `kbl status` and the result of its spot check. Counts only, no values from the
data.
