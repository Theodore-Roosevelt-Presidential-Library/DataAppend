#!/usr/bin/env python3
"""Match a contact list against the DataAppend person-level files.

Run this privately (your Mac, homelab, or a private repo's Actions). It
reads a CSV of contacts, fetches only the public shard files it needs, and
writes matches to a local CSV. Contact data never leaves the machine: the
requests ask for a ZIP or a two-letter surname prefix, not for a person.

Input CSV columns (header names are matched loosely):
    id (optional), first_name, last_name, zip, email (optional)

Usage:
    python3 pipeline/match_people.py contacts.csv matches.csv
        [--base https://append.labs.trlibrary.com | --base site]
        [--min-confidence medium] [--sources faa,fec,sec,irs990]

Never commit contacts.csv or matches.csv.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path

TOOL_UA = ("TRPL-DataAppend/1.0 "
           "(+https://github.com/Theodore-Roosevelt-Presidential-Library/DataAppend)")

# Common English given-name equivalents. Each set is one family.
NICKNAMES = [
    {"WILLIAM", "BILL", "BILLY", "WILL", "WILLIE", "LIAM"},
    {"ROBERT", "BOB", "BOBBY", "ROB", "ROBBIE", "BERT"},
    {"RICHARD", "RICK", "RICKY", "DICK", "RICH", "RICHIE"},
    {"JAMES", "JIM", "JIMMY", "JAMIE"},
    {"JOHN", "JACK", "JOHNNY", "JON"},
    {"JOSEPH", "JOE", "JOEY"},
    {"THOMAS", "TOM", "TOMMY"},
    {"CHARLES", "CHUCK", "CHARLIE", "CHAS"},
    {"MICHAEL", "MIKE", "MICKEY"},
    {"DAVID", "DAVE", "DAVY"},
    {"DANIEL", "DAN", "DANNY"},
    {"STEPHEN", "STEVEN", "STEVE"},
    {"EDWARD", "ED", "EDDIE", "TED", "TEDDY", "NED"},
    {"THEODORE", "TED", "TEDDY", "THEO"},
    {"ANTHONY", "TONY"},
    {"CHRISTOPHER", "CHRIS", "KIT"},
    {"MATTHEW", "MATT"},
    {"NICHOLAS", "NICK", "NICKY"},
    {"GREGORY", "GREG"},
    {"RONALD", "RON", "RONNIE"},
    {"DONALD", "DON", "DONNIE"},
    {"KENNETH", "KEN", "KENNY"},
    {"TIMOTHY", "TIM", "TIMMY"},
    {"GERALD", "GERRY", "JERRY"},
    {"LAWRENCE", "LARRY"},
    {"PATRICK", "PAT"},
    {"PETER", "PETE"},
    {"SAMUEL", "SAM", "SAMMY"},
    {"BENJAMIN", "BEN", "BENNY"},
    {"ALEXANDER", "ALEX", "AL", "SANDY"},
    {"ALBERT", "AL", "BERT"},
    {"ALFRED", "AL", "ALFIE"},
    {"FREDERICK", "FRED", "FREDDIE"},
    {"HENRY", "HANK", "HARRY"},
    {"HAROLD", "HAL", "HARRY"},
    {"RAYMOND", "RAY"},
    {"DOUGLAS", "DOUG"},
    {"PHILIP", "PHILLIP", "PHIL"},
    {"JEFFREY", "JEFFERY", "GEOFFREY", "JEFF"},
    {"LEONARD", "LEN", "LENNY", "LEO"},
    {"ELIZABETH", "LIZ", "BETH", "BETTY", "BETSY", "ELIZA", "LIZZIE"},
    {"MARGARET", "MAGGIE", "PEGGY", "MEG", "MARGE", "PEG"},
    {"KATHERINE", "CATHERINE", "KATHRYN", "KATE", "KATHY", "CATHY", "KATIE", "KAT"},
    {"PATRICIA", "PAT", "PATTY", "TRISH"},
    {"JENNIFER", "JEN", "JENNY"},
    {"SUSAN", "SUE", "SUSIE", "SUZANNE"},
    {"DEBORAH", "DEBRA", "DEB", "DEBBIE"},
    {"BARBARA", "BARB", "BARBIE"},
    {"CHRISTINE", "CHRISTINA", "CHRIS", "TINA"},
    {"REBECCA", "BECKY", "BECCA"},
    {"VICTORIA", "VICKI", "VICKY", "TORI"},
    {"KIMBERLY", "KIM"},
    {"PAMELA", "PAM"},
    {"SAMANTHA", "SAM"},
    {"ALEXANDRA", "ALEX", "SANDY", "SANDRA"},
    {"JACQUELINE", "JACKIE"},
    {"CYNTHIA", "CINDY"},
    {"JUDITH", "JUDY"},
    {"DOROTHY", "DOT", "DOTTIE"},
]
NICK: dict[str, set] = {}
for fam in NICKNAMES:
    for n in fam:
        NICK.setdefault(n, set()).update(fam)

FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
             "icloud.com", "me.com", "mac.com", "msn.com", "live.com",
             "comcast.net", "att.net", "verizon.net", "sbcglobal.net",
             "protonmail.com", "proton.me", "ymail.com", "midco.net",
             "bis.midco.net", "q.com", "charter.net", "cox.net"}
SUFFIXES = {"JR", "SR", "II", "III", "IV", "V", "MD", "DDS", "PHD", "ESQ", "CPA"}
TITLES = {"DR", "MR", "MRS", "MS", "MISS", "REV", "HON", "PROF"}
LEVEL = {"low": 0, "medium": 1, "high": 2}
ALL_SOURCES = ("faa", "fec", "sec", "irs990")


def tok(s: str | None) -> str:
    return re.sub(r"[^A-Z]", "", (s or "").upper())


def zip5(z: str | None) -> str | None:
    d = re.sub(r"\D", "", z or "")
    if len(d) in (3, 4):          # leading zeros lost in a spreadsheet
        d = d.zfill(5)
    return d[:5] if len(d) >= 5 else None


def parse_comma_name(name: str) -> dict:
    """'LAST, FIRST MIDDLE' (FEC) -> parts."""
    last, _, rest = name.partition(",")
    toks = [tok(t) for t in rest.split() if tok(t) and tok(t) not in TITLES | SUFFIXES]
    return {"last": tok(last), "first": toks[0] if toks else None,
            "middle": toks[1] if len(toks) > 1 else None}


def first_name_match(a: str | None, b: str | None) -> str | None:
    """'exact', 'nickname', 'initial' or None."""
    a, b = tok(a), tok(b)
    if not a or not b:
        return None
    if a == b:
        return "exact"
    if b in NICK.get(a, ()):
        return "nickname"
    if (len(a) == 1 or len(b) == 1) and a[0] == b[0]:
        return "initial"
    return None


class Site:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.local = not re.match(r"https?://", self.base)

    @lru_cache(maxsize=8192)
    def doc(self, path: str) -> dict | None:
        if self.local:
            p = Path(self.base) / path
            return json.loads(p.read_text()) if p.exists() else None
        req = urllib.request.Request(f"{self.base}/{path}",
                                     headers={"User-Agent": TOOL_UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def records(self, path: str) -> list[dict]:
        d = self.doc(path)
        if not d:
            return []
        if "fields" in d:
            f = d["fields"]
            return [dict(zip(f, row)) for row in d["rows"]]
        return d.get("records", [])

    def org(self, ein: str) -> dict:
        for o in self.records(f"irs990/orgs/{ein[:3]}.json"):
            if o["ein"] == ein:
                return o
        return {}


def score(c: dict, rec: dict, same_zip: bool, state: str | None,
          employer_text: str = "") -> tuple[str, list[str]] | None:
    """Confidence for one candidate whose surname already matches."""
    fm = first_name_match(c["first"], rec.get("first"))
    if not fm:
        return None
    cm, rm = tok(c.get("middle"))[:1], tok(rec.get("middle"))[:1]
    if cm and rm and cm != rm:
        return None  # different middle initials: different people
    why = [f"first name {fm}"]
    pts = {"exact": 3, "nickname": 2, "initial": 1}[fm]
    if same_zip:
        pts += 2
        why.append("same ZIP")
    elif c.get("state") and state and c["state"] == state:
        pts += 1
        why.append("same state")
    else:
        return None  # name alone, in another state, is not a match
    dom = c.get("domain")
    if dom and dom not in FREE_MAIL and employer_text:
        stem = dom.rsplit(".", 2)[-2] if "." in dom else dom
        if len(stem) >= 3 and stem.upper() in re.sub(r"[^A-Z]", "", employer_text.upper()):
            pts += 2
            why.append(f"email domain matches {stem}")
    if cm and rm == cm:
        pts += 1
        why.append("middle initial matches")
    conf = "high" if pts >= 5 else "medium" if pts >= 3 else "low"
    return conf, why


def read_contacts(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        cols = {re.sub(r"[^a-z]", "", c.lower()): c for c in rd.fieldnames or []}
        pick = lambda *names: next((cols[n] for n in names if n in cols), None)
        c_id = pick("id", "contactid")
        c_first = pick("firstname", "first", "givenname")
        c_last = pick("lastname", "last", "surname", "familyname")
        c_zip = pick("zip", "zipcode", "postalcode", "postal")
        c_mail = pick("email", "emailaddress")
        if not (c_first and c_last and c_zip):
            sys.exit("Contacts CSV needs first name, last name and ZIP columns.")
        out = []
        for i, r in enumerate(rd, 1):
            mail = (r.get(c_mail) or "").strip().lower() if c_mail else ""
            first_parts = (r.get(c_first) or "").split()
            out.append({
                "id": (r.get(c_id) or "").strip() if c_id else (mail or str(i)),
                "first": first_parts[0] if first_parts else "",
                "middle": first_parts[1] if len(first_parts) > 1 else "",
                "last": tok(r.get(c_last)),
                "zip": zip5(r.get(c_zip)),
                "domain": mail.split("@")[-1] if "@" in mail else None,
            })
        return out


def find(site: Site, c: dict, sources: set) -> list[tuple]:
    """All (source, record, (confidence, why), detail) candidates for a contact."""
    found = []
    last, z = c["last"], c["zip"]
    key = last[:2] if len(last) >= 2 else None

    if "faa" in sources and z:
        for r in site.records(f"faa/aircraft/{z}.json"):
            if tok(r.get("last")) == last:
                s = score(c, r, True, r.get("state"))
                if s:
                    found.append(("faa_aircraft", r, s,
                                  " ".join(str(x) for x in (r.get("year"), r.get("make"),
                                                             r.get("model")) if x)
                                  + f" ({r['n_number']}, {r.get('engine_type') or 'engine n/a'})"))
    if "fec" in sources and z:
        for r in site.records(f"fec/donors/{z}.json"):
            nm = parse_comma_name(r["name"])
            if nm["last"] == last:
                r = {**r, **nm}
                s = score(c, r, True, c.get("state"), r.get("employer") or "")
                if s:
                    found.append(("fec_donor", r, s,
                                  f"${r['total']:,} in {r['gift_records']} gift records "
                                  f"({r.get('first_date')} to {r.get('last_date')}); "
                                  f"{r.get('occupation') or 'occupation n/a'}"
                                  + (f", {r['employer']}" if r.get("employer") else "")))
    if "sec" in sources and key:
        for r in site.records(f"sec/insiders/{key}.json"):
            if tok(r.get("last")) == last:
                issuers = " ".join(x.get("issuer", "") for x in r.get("roles", []))
                s = score(c, r, r.get("zip") == z, r.get("state"), issuers)
                if s:
                    roles = "; ".join(
                        f"{x.get('title') or x.get('relationship') or 'insider'}, "
                        f"{x['issuer']}" + (f" ({x['ticker']})" if x.get("ticker") else "")
                        for x in r.get("roles", []))
                    found.append(("sec_insider", r, s,
                                  f"{roles}; last filed {r.get('last_filed')}"))
    if "irs990" in sources and key:
        for r in site.records(f"irs990/people/{key}.json"):
            if tok(r.get("last")) == last:
                org = site.org(r["ein"])
                state = r.get("state") or org.get("state")
                s = score(c, r, bool(r.get("zip")) and r.get("zip") == z, state,
                          org.get("name") or "")
                if s:
                    money = ", ".join(
                        f"{label} ${org[k]:,}" for k, label in
                        (("total_assets", "assets"), ("grants_paid", "grants paid"))
                        if org.get(k))
                    found.append(("irs990_" + ("foundation" if org.get("form") == "990PF"
                                               else "board"), r, s,
                                  f"{r.get('title') or 'Officer'}, {org.get('name')} "
                                  f"({org.get('state')}, Form {org.get('form')}, "
                                  f"{org.get('tax_year')}"
                                  + (f"; {money}" if money else "") + ")"))
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("contacts", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--base", default="https://append.labs.trlibrary.com",
                    help="site URL or local build folder")
    ap.add_argument("--min-confidence", default="medium", choices=list(LEVEL))
    ap.add_argument("--sources", default=",".join(ALL_SOURCES))
    a = ap.parse_args()
    site = Site(a.base)
    sources = {s.strip() for s in a.sources.split(",")}

    contacts = read_contacts(a.contacts)
    for c in contacts:
        zdoc = site.doc(f"zip/{c['zip']}.json") if c["zip"] else None
        c["state"] = (zdoc or {}).get("state")

    rows = []
    for n, c in enumerate(contacts, 1):
        if c["last"]:
            for source, rec, (conf, why), detail in find(site, c, sources):
                if LEVEL[conf] >= LEVEL[a.min_confidence]:
                    rows.append({"contact_id": c["id"], "source": source,
                                 "confidence": conf, "matched_name": rec["name"],
                                 "matched_city": rec.get("city"),
                                 "matched_state": rec.get("state"),
                                 "detail": detail, "why": "; ".join(why)})
        if n % 500 == 0:
            print(f"  {n:,} contacts checked", flush=True)

    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["contact_id", "source", "confidence",
                                          "matched_name", "matched_city",
                                          "matched_state", "detail", "why"])
        w.writeheader()
        w.writerows(rows)
    matched = len({r["contact_id"] for r in rows})
    print(f"{len(contacts):,} contacts, {matched:,} with at least one match, "
          f"{len(rows):,} match rows → {a.out}")


if __name__ == "__main__":
    sys.exit(main())
