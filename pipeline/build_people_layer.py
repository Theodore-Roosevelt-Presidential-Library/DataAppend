#!/usr/bin/env python3
"""Build the public person-level reference files for DataAppend.

Republishes public federal records as static JSON, one folder per source,
so a matcher can fetch only what it needs (by ZIP or surname) and compare
names locally. Each file mirrors what the agency itself publishes, minus
street addresses (matching needs only name, city, state and ZIP).

No contact data is read or written here. Matching against the Library's
contacts happens in pipeline/match_people.py, run privately.

Sources (each optional; missing inputs are skipped with a note):
  faa  FAA Aircraft Registry (ReleasableAircraft.zip)
  sec  SEC Insider Transactions Data Sets (Forms 3, 4, 5)

Usage:
    python3 pipeline/build_people_layer.py --out site
        [--faa-zip data/raw/faa/ReleasableAircraft.zip]
        [--sec-quarters 4]

Downloads identify themselves honestly. SEC requires a contact email in the
User-Agent: set SEC_USER_AGENT="TRPL DataAppend name@trlibrary.com".
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"

TOOL_UA = ("TRPL-DataAppend/1.0 "
           "(+https://github.com/Theodore-Roosevelt-Presidential-Library/DataAppend)")

FAA_URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"
SEC_URL = ("https://www.sec.gov/files/structureddata/data/"
           "insider-transactions-data-sets/{q}_form345.zip")

SUFFIXES = {"JR", "SR", "II", "III", "IV", "V", "MD", "DDS", "PHD", "ESQ", "CPA"}


# ---------------------------------------------------------------- helpers

def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def norm_token(s: str) -> str:
    return re.sub(r"[^A-Z]", "", s.upper())


def split_last_first(name: str) -> dict:
    """Parse 'LAST FIRST MIDDLE [SUFFIX]' (FAA and SEC order)."""
    toks = [t for t in re.split(r"[\s,]+", name.upper()) if t]
    suffix = None
    if toks and norm_token(toks[-1]) in SUFFIXES:
        suffix = norm_token(toks.pop())
    toks = [norm_token(t) for t in toks if norm_token(t)]
    if not toks:
        return {"last": None, "first": None, "middle": None, "suffix": suffix}
    return {"last": toks[0],
            "first": toks[1] if len(toks) > 1 else None,
            "middle": " ".join(toks[2:]) or None,
            "suffix": suffix}


def zip5(z: str | None) -> str | None:
    d = re.sub(r"\D", "", z or "")
    return d[:5].zfill(5) if len(d) >= 5 else None


def surname_key(last: str | None) -> str:
    """Shard key: first two letters of the surname (e.g. 'SM')."""
    k = norm_token(last or "")[:2]
    return k if len(k) == 2 else "_"


def fetch(url: str, dest: Path, ua: str) -> Path | None:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {url}", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f, length=1 << 20)
    except urllib.error.HTTPError as e:
        dest.unlink(missing_ok=True)
        print(f"  ! {url} returned HTTP {e.code}; skipping", flush=True)
        return None
    return dest


def write_shards(base: Path, shards: dict, meta: dict):
    base.mkdir(parents=True, exist_ok=True)
    for key, rows in shards.items():
        with open(base / f"{key}.json", "w") as f:
            json.dump({"key": key, "source": meta["source"], "as_of": meta["as_of"],
                       "records": rows}, f, separators=(",", ":"))
    meta = {**meta, "files": len(shards),
            "records": sum(len(v) for v in shards.values())}
    with open(base / "meta.json", "w") as f:
        json.dump(meta, f, indent=2)
    print(f"  wrote {meta['records']:,} records in {meta['files']:,} files "
          f"to {base}", flush=True)


# ---------------------------------------------------------------- FAA

AIRCRAFT_TYPE = {"1": "glider", "2": "balloon", "3": "blimp/dirigible",
                 "4": "fixed wing single engine", "5": "fixed wing multi engine",
                 "6": "rotorcraft", "7": "weight-shift control",
                 "8": "powered parachute", "9": "gyroplane", "H": "hybrid lift",
                 "O": "other"}
ENGINE_TYPE = {"0": "none", "1": "reciprocating", "2": "turbo-prop",
               "3": "turbo-shaft", "4": "turbo-jet", "5": "turbo-fan",
               "6": "ramjet", "7": "2 cycle", "8": "4 cycle", "9": "unknown",
               "10": "electric", "11": "rotary"}


def build_faa(out: Path, zpath: Path | None):
    print("FAA Aircraft Registry…", flush=True)
    zpath = zpath or fetch(FAA_URL, RAW / "faa" / "ReleasableAircraft.zip", TOOL_UA)
    if not zpath or not zpath.exists():
        print("  ! no FAA file. The FAA blocks automated downloads from cloud "
              "servers; download ReleasableAircraft.zip from "
              "https://registry.faa.gov/aircraftinquiry and pass --faa-zip.")
        return
    with zipfile.ZipFile(zpath) as z:
        as_of = dt.date(*z.getinfo("MASTER.txt").date_time[:3]).isoformat()
        ref = {}
        with z.open("ACFTREF.txt") as f:
            for r in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
                ref[r["CODE"].strip()] = r
        shards = defaultdict(list)
        with z.open("MASTER.txt") as f:
            for r in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
                rtype = r["TYPE REGISTRANT"].strip()
                # 1 = individual, 4 = co-owned. Companies, LLCs and
                # governments are skipped: their names aren't people.
                if rtype not in ("1", "4") or r["STATUS CODE"].strip() != "V":
                    continue
                z5 = zip5(r["ZIP CODE"])
                if not z5 or r["COUNTRY"].strip() != "US":
                    continue
                a = ref.get(r["MFR MDL CODE"].strip(), {})
                year = r["YEAR MFR"].strip()
                owners = [r["NAME"]] + [r[f"OTHER NAMES({i})"] for i in range(1, 6)]
                for i, nm in enumerate(o for o in map(clean, owners) if o):
                    rec = {
                        "name": nm,
                        **split_last_first(nm),
                        "city": clean(r["CITY"]).title(),
                        "state": r["STATE"].strip(),
                        "n_number": "N" + r["N-NUMBER"].strip(),
                        "make": clean(a.get("MFR")).title() or None,
                        "model": clean(a.get("MODEL")) or None,
                        "year": int(year) if year.isdigit() else None,
                        "aircraft_type": AIRCRAFT_TYPE.get(clean(r["TYPE AIRCRAFT"])),
                        "engine_type": ENGINE_TYPE.get(clean(r["TYPE ENGINE"])),
                        "engines": int(a["NO-ENG"]) if a.get("NO-ENG", "").strip().isdigit() else None,
                        "seats": int(a["NO-SEATS"]) if a.get("NO-SEATS", "").strip().isdigit() else None,
                        "co_owned": rtype == "4",
                        "owner_position": i + 1,
                    }
                    shards[z5].append(rec)
    write_shards(out / "faa" / "aircraft", shards, {
        "source": "FAA Aircraft Registry, releasable aircraft database",
        "source_url": "https://registry.faa.gov/aircraftinquiry",
        "as_of": as_of,
        "sharded_by": "ZIP5 of the registered address",
        "scope": "Valid registrations owned by individuals or co-owners; "
                 "street addresses omitted",
    })


# ---------------------------------------------------------------- SEC

def sec_quarters(n: int) -> list[str]:
    """The n most recent calendar quarters before today, newest first."""
    d = dt.date.today()
    y, q = d.year, (d.month - 1) // 3  # previous full quarter
    out = []
    while len(out) < n + 3:  # look a few back; recent ones may not be posted yet
        if q == 0:
            y, q = y - 1, 4
        out.append(f"{y}q{q}")
        q -= 1
    return out


def build_sec(out: Path, n_quarters: int):
    print("SEC insider filings (Forms 3, 4, 5)…", flush=True)
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if "@" not in ua:
        print("  ! SEC_USER_AGENT is not set. The SEC requires a real contact "
              "email in the User-Agent, e.g. "
              "SEC_USER_AGENT='TRPL DataAppend name@trlibrary.com'. Skipping.")
        return
    people: dict[str, dict] = {}
    got = []
    for q in sec_quarters(n_quarters):
        if len(got) >= n_quarters:
            break
        p = fetch(SEC_URL.format(q=q), RAW / "sec" / f"{q}_form345.zip", ua)
        if not p:
            continue
        got.append(q)
        with zipfile.ZipFile(p) as z:
            sub = {}
            with z.open("SUBMISSION.tsv") as f:
                for r in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"),
                                        delimiter="\t"):
                    sub[r["ACCESSION_NUMBER"]] = r
            with z.open("REPORTINGOWNER.tsv") as f:
                for r in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"),
                                        delimiter="\t"):
                    s = sub.get(r["ACCESSION_NUMBER"], {})
                    cik = r["RPTOWNERCIK"].strip()
                    rel = clean(r.get("RPTOWNER_RELATIONSHIP"))
                    if not cik or not rel or rel.lower() == "other":
                        pass
                    key = cik
                    filed = s.get("FILING_DATE", "")
                    try:
                        filed_iso = dt.datetime.strptime(filed, "%d-%b-%Y").date().isoformat()
                    except ValueError:
                        filed_iso = None
                    issuer = clean(s.get("ISSUERNAME"))
                    p_ = people.setdefault(key, {
                        "cik": cik, "name": clean(r["RPTOWNERNAME"]),
                        **split_last_first(r["RPTOWNERNAME"]),
                        "city": clean(r.get("RPTOWNER_CITY")).title() or None,
                        "state": clean(r.get("RPTOWNER_STATE")) or None,
                        "zip": zip5(r.get("RPTOWNER_ZIPCODE")),
                        "roles": {}, "last_filed": None})
                    if issuer:
                        role = p_["roles"].setdefault(issuer, {
                            "issuer": issuer,
                            "ticker": clean(s.get("ISSUERTRADINGSYMBOL")) or None,
                            "relationship": rel or None,
                            "title": clean(r.get("RPTOWNER_TITLE")) or None})
                        if rel and not role["relationship"]:
                            role["relationship"] = rel
                    if filed_iso and (not p_["last_filed"] or filed_iso > p_["last_filed"]):
                        p_["last_filed"] = filed_iso
    if not people:
        print("  ! no SEC data downloaded")
        return
    shards = defaultdict(list)
    for p_ in people.values():
        # Entities (funds, LLCs) file too; keep records that parse as a person.
        if not p_["first"] or re.search(r"\b(LLC|LP|INC|FUND|TRUST|CORP|LTD|PARTNERS|CAPITAL|HOLDINGS)\b",
                                        p_["name"].upper()):
            continue
        p_["roles"] = list(p_["roles"].values())
        shards[surname_key(p_["last"])].append(p_)
    write_shards(out / "sec" / "insiders", shards, {
        "source": "SEC Insider Transactions Data Sets (Forms 3, 4 and 5)",
        "source_url": "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets",
        "as_of": max(got),
        "quarters": sorted(got),
        "sharded_by": "first two letters of the surname (the filing address "
                      "is usually the company's, so ZIP is not a reliable key)",
        "scope": "Reporting owners who are individuals; street addresses omitted",
    })


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(ROOT / "site"))
    ap.add_argument("--sources", default="faa,sec")
    ap.add_argument("--faa-zip", type=Path)
    ap.add_argument("--sec-quarters", type=int, default=4)
    a = ap.parse_args()
    out = Path(a.out)
    src = {s.strip() for s in a.sources.split(",")}
    if "faa" in src:
        build_faa(out, a.faa_zip)
    if "sec" in src:
        build_sec(out, a.sec_quarters)


if __name__ == "__main__":
    sys.exit(main())
