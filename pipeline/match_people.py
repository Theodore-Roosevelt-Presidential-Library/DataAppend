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
        [--min-confidence medium]

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
NICK = {}
for fam in NICKNAMES:
    for n in fam:
        NICK.setdefault(n, set()).update(fam)

FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
             "icloud.com", "me.com", "mac.com", "msn.com", "live.com",
             "comcast.net", "att.net", "verizon.net", "sbcglobal.net",
             "protonmail.com", "proton.me", "ymail.com", "midco.net",
             "bis.midco.net", "q.com", "charter.net", "cox.net"}

LEVEL = {"low": 0, "medium": 1, "high": 2}


def tok(s: str | None) -> str:
    return re.sub(r"[^A-Z]", "", (s or "").upper())


def zip5(z: str | None) -> str | None:
    d = re.sub(r"\D", "", z or "")
    if len(d) in (3, 4):          # leading zeros lost in a spreadsheet
        d = d.zfill(5)
    return d[:5] if len(d) >= 5 else None


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


class Source:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.local = not re.match(r"https?://", self.base)

    @lru_cache(maxsize=4096)
    def get(self, path: str) -> list:
        if self.local:
            p = Path(self.base) / path
            if not p.exists():
                return []
            return json.loads(p.read_text())["records"]
        req = urllib.request.Request(f"{self.base}/{path}",
                                     headers={"User-Agent": TOOL_UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)["records"]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            raise


def score(contact: dict, rec: dict, same_zip: bool) -> tuple[str, list[str]] | None:
    fm = first_name_match(contact["first"], rec.get("first"))
    if not fm:
        return None
    why = [f"first name {fm}"]
    pts = {"exact": 3, "nickname": 2, "initial": 1}[fm]
    if same_zip:
        pts += 2
        why.append("same ZIP")
    elif contact.get("state") and contact["state"] == rec.get("state"):
        pts += 1
        why.append("same state")
    dom = contact.get("domain")
    if dom and dom not in FREE_MAIL:
        stem = dom.split(".")[-2] if "." in dom else dom
        issuers = " ".join(r.get("issuer", "") for r in rec.get("roles", []))
        if len(stem) >= 3 and stem.upper() in re.sub(r"[^A-Z]", "", issuers.upper()):
            pts += 2
            why.append(f"email domain matches {stem}")
    conf = "high" if pts >= 5 else "medium" if pts >= 3 else "low"
    return conf, why


def read_contacts(path: Path, zip_state: dict) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        cols = {re.sub(r"[^a-z]", "", c.lower()): c for c in rd.fieldnames or []}
        pick = lambda *names: next((cols[n] for n in names if n in cols), None)
        c_id = pick("id", "contactid", "emailaddress")
        c_first = pick("firstname", "first", "givenname")
        c_last = pick("lastname", "last", "surname", "familyname")
        c_zip = pick("zip", "zipcode", "postalcode", "postal")
        c_mail = pick("email", "emailaddress")
        if not (c_first and c_last and c_zip):
            sys.exit("Contacts CSV needs first name, last name and ZIP columns.")
        out = []
        for i, r in enumerate(rd, 1):
            z = zip5(r.get(c_zip))
            mail = (r.get(c_mail) or "").strip().lower() if c_mail else ""
            out.append({
                "id": (r.get(c_id) or "").strip() if c_id else str(i),
                "first": r.get(c_first, ""), "last": tok(r.get(c_last)),
                "zip": z, "state": zip_state.get(z),
                "domain": mail.split("@")[-1] if "@" in mail else None,
            })
        return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("contacts", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--base", default="https://append.labs.trlibrary.com",
                    help="site URL or local build folder")
    ap.add_argument("--min-confidence", default="medium", choices=list(LEVEL))
    a = ap.parse_args()
    src = Source(a.base)

    # State for each ZIP, from the public ZIP layer (used for SEC matching).
    zip_state = {}
    def state_of(z):
        if z and z not in zip_state:
            try:
                p = (Path(a.base) / "zip" / f"{z}.json") if src.local else None
                d = json.loads(p.read_text()) if p and p.exists() else None
                if d is None and not src.local:
                    req = urllib.request.Request(f"{src.base}/zip/{z}.json",
                                                 headers={"User-Agent": TOOL_UA})
                    with urllib.request.urlopen(req, timeout=60) as r:
                        d = json.load(r)
                zip_state[z] = (d or {}).get("state")
            except (urllib.error.HTTPError, OSError, ValueError):
                zip_state[z] = None
        return zip_state.get(z)

    contacts = read_contacts(a.contacts, {})
    for c in contacts:
        c["state"] = state_of(c["zip"])

    rows = []
    for c in contacts:
        if not c["last"]:
            continue
        found = []
        if c["zip"]:
            for rec in src.get(f"faa/aircraft/{c['zip']}.json"):
                if tok(rec.get("last")) == c["last"]:
                    s = score(c, rec, True)
                    if s:
                        found.append(("faa_aircraft", rec, s,
                                      f"{rec.get('year') or ''} {rec.get('make') or ''} "
                                      f"{rec.get('model') or ''} ({rec['n_number']}, "
                                      f"{rec.get('engine_type') or ''})".replace("  ", " ").strip()))
        key = c["last"][:2]
        if len(key) == 2:
            for rec in src.get(f"sec/insiders/{key}.json"):
                if tok(rec.get("last")) == c["last"]:
                    s = score(c, rec, rec.get("zip") == c["zip"])
                    if s:
                        roles = "; ".join(
                            f"{r.get('title') or r.get('relationship') or 'insider'}, "
                            f"{r['issuer']}" + (f" ({r['ticker']})" if r.get("ticker") else "")
                            for r in rec.get("roles", []))
                        found.append(("sec_insider", rec, s,
                                      f"{roles}; last filed {rec.get('last_filed')}"))
        for source, rec, (conf, why), detail in found:
            if LEVEL[conf] >= LEVEL[a.min_confidence]:
                rows.append({"contact_id": c["id"], "source": source,
                             "confidence": conf, "matched_name": rec["name"],
                             "matched_city": rec.get("city"),
                             "matched_state": rec.get("state"),
                             "detail": detail, "why": "; ".join(why)})

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
