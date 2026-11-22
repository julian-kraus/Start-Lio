"""Projector: Lookahead records computed from kb facts and sourcing/ models, plus labelled scenario inputs.

    python -m projector artbook --out data/lookahead/artbook.jsonl     # needs DATABASE_URL to a seeded kb
    python -m tracker.web --rehearse data/lookahead/artbook.jsonl      # the demo, every number computed

Seed a local kb from the data mirror first with `python -m projector seed` (no tracker, no LLM).
"""
