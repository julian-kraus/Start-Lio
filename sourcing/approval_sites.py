"""Which seat releases deliveries to a site.

A seat belongs to a site if its title names exactly the site that was passed, or that site's id or name in the kb
site facts. Because charts shorten names, it also does if the title carries every part of the id and the last
word of the name, adds nothing that the name and id do not have, and fits no other site and no other seat. A company-wide seat
covers every known site where the role is not held per site.
Nothing else matches: part of a name never selects a holder.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sourcing.approval_docs import MIN_CONFIDENCE, ours, words


@dataclass
class Site:
    site_id: str
    names: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)

    def spellings(self) -> list[list[str]]:
        return [words(text) for text in (self.site_id, *self.names)]


def read_sites(rows: list[dict]) -> list[Site]:
    """The sites of our own records, with their names."""
    sites: dict[str, Site] = {}
    for row in rows:
        site_id = (row.get("subject") or {}).get("site_id")
        if row.get("fact_type") != "site" or not site_id or row.get("attribute") not in ("site_id", "name"):
            continue
        if not ours(row) or (row.get("confidence") is not None and float(row["confidence"]) < MIN_CONFIDENCE):
            continue
        site = sites.setdefault(str(site_id), Site(str(site_id)))
        if row["attribute"] == "name" and isinstance(row.get("value"), str):
            site.names.append(row["value"])
        site.ids.append(str(row["fact_id"]))
    return list(sites.values())


def _covered(part: list[str], whole: list[str]) -> bool:
    """Every word of `part` is in `whole`: as is, as the initials of adjacent words or as a three letter code."""
    initials = "".join(w[0] for w in whole)
    return all(w in whole or (len(w) == 2 and w in initials) or (len(w) == 3 and any(x.startswith(w) for x in whole))
               for w in part)


def _short_for(scope: list[str], site: Site) -> bool:
    site_id = words(site.site_id)
    for name in map(words, site.names):
        if name and name[-1] in scope and _covered(site_id, scope) and _covered(scope, name + site_id):
            return True
    return False


def seat_scopes(scopes: set[str], wanted: str, sites: list[Site]) -> tuple[set[str], list[str]]:
    """The scopes of the seats that cover this site ("" is the whole company) and the site facts used."""
    want = words(wanted)
    if not want:
        return set(), []
    known = [s for s in sites if want in s.spellings()]
    if len(known) > 1:
        return set(), [i for s in known for i in s.ids]     # one spelling, two sites
    accepted = known[0].spellings() if known else [want]
    cited = known[0].ids if known else []
    exact = {s for s in scopes if s and words(s) in accepted}
    if not known:
        return exact, cited
    if exact:
        shared = any(words(e) in o.spellings() for e in exact for o in sites if o is not known[0])
        return (set() if shared else exact), cited
    near = {s for s in scopes if s and _short_for(words(s), known[0])}
    if near:
        scope = words(next(iter(near)))
        others = [s for s in sites if s is not known[0] and (scope in s.spellings() or _short_for(scope, s))]
        return (near if len(near) == 1 and not others else set()), cited
    # a company-wide seat covers a known site, unless the role is held per site and this site's seat is unreadable
    return ({""} if scopes == {""} else set()), cited
