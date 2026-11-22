# Lookahead

Lookahead is a procurement agent that places the backup bet before the failure. It learns the goal and the risk
appetite, ranks the parts that can break that goal, hedges unreliable sources while there is still time, watches
supplier promises and, when one breaks, raises one decision card for the person who signs.

Built at START Hack Munich 2026 for the Lio challenge.

## How it works

| Package | What it does | Schema |
|---|---|---|
| `tracker/` | Polls the customer portal, serves the screen | `public` |
| `kb/` | Turns files and supplier emails into facts with sources, believed values and issues | `kb` |
| `mail/` | Stores supplier email, sends only approved drafts | `mail` |
| `sourcing/` | Deterministic models: coverage, weak spots, reliability, hedge, approvals | none |
| `agents/` | Agent roles that call `sourcing/` and `mail/` as tools | `plan` |
| `plan/` | Goals, offers, promises, decisions, the monitor loop | `plan` |
| `projector/` | Computes a rehearsal of the screen from the knowledge base | none |

The maths is in code. A number on the screen is meant to lead back to a fact, a goal answer or a labelled scenario
input. The screen renders an append-only record log.

## Run the rehearsal

The rehearsal is a scripted replay. Its numbers are computed from the knowledge base and from labelled scenario
inputs. It sends no email and calls no model.

You need Python, Docker and the portal snapshot in `data/current/`. The data is not in this repo.

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

docker compose up -d lab-db
export DATABASE_URL=postgresql://lab:lab@127.0.0.1:5441/lab
python -m projector seed
python -m projector artbook --out data/lookahead/artbook.jsonl
python -m tracker.web --port 8802 --rehearse data/lookahead/artbook.jsonl
```

Open <http://127.0.0.1:8802/lookahead>, press `G` to set the goal and `J` for each next step.
<http://127.0.0.1:8802/lookahead/v2> shows the same records in the newer layout.

## Run it live

Settings live in `.env` and `secrets/` (both git-ignored): `SOONY_API_BASE_URL` and `SOONY_API_TOKEN` for the
portal, `secrets/db.env` for Postgres, `secrets/mail.env` for mail.

```sh
docker compose up -d db tracker kb mail web     # portal, knowledge base, mail, screen on :8787
python -m plan run                              # monitor: needs DATABASE_URL and a model key (or --no-model)
```

`MAIL_SEND_MODE` defaults to `dry_run`, so nothing is sent unless a person changes it.

## Limits

- This is a hackathon prototype. The path from a live email to a decision card is not yet reliable end to end.
- A decision can be accepted only when a signer is resolved. Without org seat facts in the knowledge base the
  card says `needs_human` and Accept stays disabled.
- Scenario suppliers, emails and events are simulated and are labelled as scenario inputs.

## Tests

```sh
pip install pytest
pytest -q     # database tests need TEST_DATABASE_URL on a local throwaway Postgres
```
