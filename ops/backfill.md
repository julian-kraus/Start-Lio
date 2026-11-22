# Backfill: drain the live extraction queue on L1

For a human on L1. Agents never run this: it writes to the live database and pays for model calls. It does not send
email and does not touch approvals or the send mode.

Run everything in one bash or zsh shell, from the repo root of the clone that holds `data/`, `mail_data/` and the
credential files.

## 0. Preconditions

- The checkout has #7 (the extraction adapter) and #65 (kb reads mail). Before #65, `kb sync` deletes mail documents
  and `kb extract` loses their sender label. Both imports must work:

```sh
git pull --ff-only
python -m pip install -r requirements.txt
python -c "import kb.mail_source, kb.llm.anthropic; print('ok')"
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

Continue only if the output shows `"done": 1` or `"needs_review": 1`. With `"failed": 1`, read the warning and fix
the cause (key, network, missing package) first: every failed run uses up one of the document's three attempts,
and no command puts a failed document back into the queue.

## 5. Drain

`--limit` must be above `queue.pending`. Documents no kind fits stay at the head of the queue (#66), so with a small
limit the same ones are read again and the rest are never reached.

```sh
for i in 1 2 3; do kbl extract --adapter "$ADAPTER" --limit 1000; done
kbl status
```

Three runs, because a document that errors is tried three times. Answers are cached, so a document that was
answered is not paid for again.

## 6. Read the result

Done means that `queue` in `kbl status` has no `pending`. Every document is then facts or a named issue:

| `queue` state | meaning | where to look |
|---|---|---|
| `done` | facts passed the gate | `kbl status`, `fact_types` |
| `review` | the answer failed the gate, its facts do not count | `kbl issues --kind review_needed`, `kbl review` |
| `failed` | three errors, or mail that fits no kind | `kbl issues --kind extraction_failed` |
| `pending` | no known kind fits | `kbl issues --kind pending_extraction` |

Tables never enter the queue. A new or renamed layout is a `needs_schema_map` document:

```sh
kbl issues --kind schema_map_needed
kbl issues --kind promotion_needed     # structures the designer proposed
kbl explain <issue_key>                # the detail behind one line
```

If `pending` is left after step 5, do not loop on it: every run pays for another classification. Post the count on
the backfill issue (#36).

Known gap: a JSON file without a list of records ends `loaded` with no facts and no issue (#79). It shows up in
neither list.

## 7. Restart the worker, one instance

```sh
mkdir -p exports/logs
kbl run >> exports/logs/kb-run.log 2>&1 &                                  # no adapter: new documents queue
KB_LLM_ADAPTER="$ADAPTER" kbl run >> exports/logs/kb-run.log 2>&1 &        # or: extracts as they arrive
```

Run one of the two lines, not both. Use the second only when step 6 left nothing pending (#66: it classifies an
unroutable document again on every poll, every 5 seconds).

Until #78 is fixed, a running worker does not see new portal files. After each drop run:

```sh
kbl sync && kbl extract --adapter "$ADAPTER" --limit 1000
```

## 8. Report

Post on #36 the `queue` counts from `kbl status` and the result of its spot check. Counts only, no values from the
data.
