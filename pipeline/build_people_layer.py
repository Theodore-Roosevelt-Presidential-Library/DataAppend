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


def fetch(url: str, dest: Path, ua: str, max_age_days: float | None = None) -> Path | None:
    """Download url to dest unless a cached copy exists (and is fresh enough)."""
    if dest.exists() and dest.stat().st_size > 0:
        age = (dt.datetime.now().timestamp() - dest.stat().st_mtime) / 86400
        if max_age_days is None or age < max_age_days:
            return dest
        dest.unlink()
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
    """Write one JSON file per shard key.

    With meta["fields"], files use the compact layout
    {"fields": [...], "rows": [[...], ...]}; otherwise {"records": [{...}]}.
    """
    base.mkdir(parents=True, exist_ok=True)
    fields = meta.get("fields")
    for key, rows in shards.items():
        doc = {"key": key, "source": meta["source"], "as_of": meta["as_of"]}
        if meta.get("notice"):
            doc["notice"] = meta["notice"]
        if fields:
            doc["fields"] = fields
            doc["rows"] = rows
        else:
            doc["records"] = rows
        with open(base / f"{key}.json", "w") as f:
            json.dump(doc, f, separators=(",", ":"))
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
    if not zpath or not zpath.exists() or not zipfile.is_zipfile(zpath):
        size = zpath.stat().st_size if zpath and zpath.exists() else 0
        print("  ! no valid FAA file" + (f" ({zpath} is {size:,} bytes and not a "
              "zip; probably the FAA's block page saved under the zip's name)"
              if size else "") + ". Download ReleasableAircraft.zip in a "
              "browser and attach it to the faa-data release. Skipping FAA.")
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
        if not zipfile.is_zipfile(p):
            print(f"  ! {p.name} is not a zip ({p.stat().st_size:,} bytes); skipping")
            p.unlink()
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


# ---------------------------------------------------------------- FEC

FEC_URL = "https://www.fec.gov/files/bulk-downloads/{y}/indiv{yy}.zip"
FEC_COLS = ("CMTE_ID AMNDT_IND RPT_TP TRANSACTION_PGI IMAGE_NUM TRANSACTION_TP "
            "ENTITY_TP NAME CITY STATE ZIP_CODE EMPLOYER OCCUPATION TRANSACTION_DT "
            "TRANSACTION_AMT OTHER_ID TRAN_ID FILE_NUM MEMO_CD MEMO_TEXT SUB_ID").split()
FC = {c: i for i, c in enumerate(FEC_COLS)}
# Conduits report each earmarked gift once themselves and the recipient
# reports it again; their rows only count when a person has no other record.
CONDUITS = {"C00401224": "ActBlue", "C00694323": "WinRed"}
FEC_NOTICE = ("Contributor information from FEC reports may not be sold or used "
              "for the purpose of soliciting contributions or for commercial "
              "purposes (52 U.S.C. 30111(a)(4)).")


PLACEHOLDERS = {"", "N/A", "NA", "NONE", "NULL", "INFORMATION REQUESTED",
                "INFORMATION REQUESTED PER BEST EFFORTS", "REQUESTED",
                "NOT PROVIDED", "UNKNOWN", "-", "."}


def placeholder(v: str) -> str | None:
    v = clean(v)
    return None if v.upper() in PLACEHOLDERS else v.title()


def split_fec_name(name: str) -> dict:
    """FEC names are 'LAST, FIRST MIDDLE' (sometimes with titles)."""
    if "," in name:
        last, rest = name.split(",", 1)
        return split_last_first(f"{last.replace(' ', '')} {rest}")
    return split_last_first(name)


def build_fec(out: Path, n_cycles: int):
    print("FEC individual contributions…", flush=True)
    today = dt.date.today()
    cur = today.year if today.year % 2 == 0 else today.year + 1
    cycles = [cur - 2 * i for i in range(n_cycles)]
    # key (NAME, ZIP5) -> [direct, conduit, gifts, first, last, employer,
    #                       occupation, city, state, cycles(set)]
    agg: dict[tuple, list] = {}
    used = []
    for y in cycles:
        # The FEC refreshes bulk files weekly; re-download monthly.
        p = fetch(FEC_URL.format(y=y, yy=str(y)[2:]), RAW / "fec" / f"indiv{str(y)[2:]}.zip",
                  TOOL_UA, max_age_days=25)
        if not p or not zipfile.is_zipfile(p):
            print(f"  ! no valid file for the {y} cycle; skipping it")
            continue
        used.append(y)
        n = 0
        with zipfile.ZipFile(p) as z, z.open("itcont.txt") as f:
            for line in io.TextIOWrapper(f, encoding="latin-1"):
                c = line.rstrip("\n").split("|")
                if len(c) < 21 or c[FC["ENTITY_TP"]] != "IND" or c[FC["MEMO_CD"]] == "X":
                    continue
                z5 = zip5(c[FC["ZIP_CODE"]])
                name = clean(c[FC["NAME"]]).upper()
                if not z5 or not name:
                    continue
                try:
                    amt = float(c[FC["TRANSACTION_AMT"]] or 0)
                except ValueError:
                    amt = 0.0
                d = c[FC["TRANSACTION_DT"]]
                ymd = f"{d[4:8]}-{d[0:2]}-{d[2:4]}" if len(d) == 8 else ""
                conduit = c[FC["CMTE_ID"]] in CONDUITS
                k = (name, z5)
                a = agg.get(k)
                if a is None:
                    a = agg[k] = [0.0, 0.0, 0, ymd, ymd, "", "", "", "", set()]
                a[1 if conduit else 0] += amt
                a[2] += 1
                if ymd:
                    if not a[3] or ymd < a[3]:
                        a[3] = ymd
                    if ymd >= a[4]:
                        a[4] = ymd
                        a[5], a[6] = c[FC["EMPLOYER"]], c[FC["OCCUPATION"]]
                        a[7], a[8] = c[FC["CITY"]], c[FC["STATE"]]
                elif not a[5]:
                    a[5], a[6] = c[FC["EMPLOYER"]], c[FC["OCCUPATION"]]
                    a[7], a[8] = c[FC["CITY"]], c[FC["STATE"]]
                a[9].add(y)
                n += 1
        print(f"  {y} cycle: {n:,} gift records", flush=True)
    if not agg:
        print("  ! no FEC data")
        return
    # Name is kept as filed ("LAST, FIRST MIDDLE"); the matcher splits it.
    # State is implied by the ZIP, so it is not repeated on every row.
    fields = ["name", "city", "total", "gift_records", "first_date",
              "last_date", "employer", "occupation", "cycles"]
    shards = defaultdict(list)
    for (name, z5), a in agg.items():
        if "," not in name and len(name.split()) < 2:
            continue
        total = a[0] if a[0] > 0 else a[1]
        shards[z5].append([name, clean(a[7]).title() or None,
                           int(round(total)), a[2], a[3] or None, a[4] or None,
                           placeholder(a[5]), placeholder(a[6]), sorted(a[9])])
    agg.clear()
    write_shards(out / "fec" / "donors", shards, {
        "source": "Federal Election Commission, individual contributions bulk data",
        "source_url": "https://www.fec.gov/data/browse-data/?tab=bulk-data",
        "as_of": dt.date.today().isoformat(),
        "cycles": used,
        "sharded_by": "ZIP5 reported with the contribution",
        "scope": ("One row per contributor name and ZIP across the listed cycles. "
                  "Totals are approximate: memo entries are excluded, and gifts "
                  "through ActBlue or WinRed count only when the recipient's own "
                  "record is missing. Recipients are not included."),
        "notice": FEC_NOTICE,
        "fields": fields,
    })


# ---------------------------------------------------------------- IRS 990

IRS_XML = "https://apps.irs.gov/pub/epostcard/990/xml/{y}/{batch}.zip"
IRS_INDEX = "https://apps.irs.gov/pub/epostcard/990/xml/{y}/index_{y}.csv"
EFILE = "{http://www.irs.gov/efile}"
PERSON_GROUP = {"990": "Form990PartVIISectionAGrp",
                "990PF": "OfficerDirTrstKeyEmplGrp"}
# Size cut for full 990 filers: board members and officers of organizations
# with at least this much in assets or revenue. All 990-PF private-foundation
# trustees are kept. 990-EZ filers (small organizations) are left out.
MIN_990_SIZE = 1_000_000


def _x(el, *paths):
    """First non-empty text among slash-separated paths (efile namespace)."""
    for path in paths:
        node = el.find(".//" + "/".join(EFILE + p for p in path.split("/")))
        if node is not None and node.text and node.text.strip():
            return node.text.strip()
    return None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


ENTITY_WORDS = re.compile(
    r"(?:\sN(?:\s|\.\s?)A\.?$)|\b(BANK|KEYBANK|BANCORP|TRUST CO|TRUST COMPANY|COMPANY|CORP|"
    r"CORPORATION|INC|LLC|LLP|LTD|LP|PLLC|PC|FOUNDATION|ASSOCIATION|ASSN|FUND|"
    r"PARTNERS|HOLDINGS|GROUP|ADVISORS|WEALTH|CAPITAL|MANAGEMENT|SERVICES|"
    r"FIDUCIARY|TRUSTEES|COMMITTEE|BOARD|UNIVERSITY|COLLEGE|CHURCH|COUNTY|CITY OF)\b")


def is_entity(name: str) -> bool:
    return bool(ENTITY_WORDS.search(name.upper()))


def split_first_last(name: str) -> dict:
    """990 PersonNm is usually natural order: 'JOHN A SMITH JR'.
    A comma means 'SMITH, JOHN A'."""
    if "," in name:
        last, rest = name.split(",", 1)
        return split_last_first(f"{last.replace(' ', '')} {rest}")
    toks = [norm_token(t) for t in name.upper().split() if norm_token(t)]
    toks = [t for t in toks if t not in {"DR", "MR", "MRS", "MS", "REV", "HON", "PROF"}]
    while toks and toks[-1] in SUFFIXES:
        toks.pop()
    if len(toks) < 2:
        return {"last": toks[0] if toks else None, "first": None}
    return {"last": toks[-1], "first": toks[0]}


def build_irs990(out: Path, year: int | None):
    import xml.etree.ElementTree as ET
    print("IRS Form 990 officers, directors and trustees…", flush=True)
    today = dt.date.today()
    years = [year] if year else [today.year - 1, today.year - 2]
    idx_path = y = None
    for y in years:
        idx_path = fetch(IRS_INDEX.format(y=y), RAW / "irs990" / f"index_{y}.csv", TOOL_UA)
        if idx_path:
            break
    if not idx_path:
        print("  ! no IRS index found; skipping")
        return
    with open(idx_path, newline="", encoding="utf-8-sig") as f:
        batches = sorted({r["XML_BATCH_ID"].strip() for r in csv.DictReader(f)
                          if r.get("XML_BATCH_ID", "").strip()})
    print(f"  processing year {y}: {len(batches)} batches", flush=True)
    orgs = {}
    people: dict[tuple, list] = {}
    filings = 0
    for b in batches:
        p = fetch(IRS_XML.format(y=y, batch=b), RAW / "irs990" / f"{b}.zip", TOOL_UA)
        if not p or not zipfile.is_zipfile(p):
            print(f"  ! batch {b} unavailable; skipping")
            continue
        with zipfile.ZipFile(p) as z:
            for n in z.namelist():
                if not n.endswith(".xml"):
                    continue
                try:
                    root = ET.fromstring(z.read(n))
                except ET.ParseError:
                    continue
                form = _x(root, "ReturnHeader/ReturnTypeCd")
                grp = PERSON_GROUP.get(form)
                if not grp:
                    continue
                filings += 1
                ein = _x(root, "Filer/EIN")
                tax_year = _int(_x(root, "ReturnHeader/TaxYr"))
                if not ein:
                    continue
                prev = orgs.get(ein)
                if prev and (prev[4] or 0) > (tax_year or 0):
                    continue  # keep the newest filing per organization
                if form == "990PF":
                    assets = _int(_x(root, "FMVAssetsEOYAmt", "TotalAssetsEOYAmt"))
                    giving = _int(_x(root, "ContriPaidRevAndExpnssAmt",
                                     "TotalExpensesDsbrsChrtblAmt"))
                elif form == "990EZ":
                    assets = _int(_x(root, "Form990TotalAssetsGrp/EOYAmt"))
                    giving = None
                else:
                    assets = _int(_x(root, "TotalAssetsGrp/EOYAmt"))
                    giving = None
                revenue = _int(_x(root, "CYTotalRevenueAmt", "TotalRevenueAmt",
                                  "TotalRevAndExpnssAmt"))
                if form == "990" and max(assets or 0, revenue or 0) < MIN_990_SIZE:
                    continue
                orgs[ein] = [clean(_x(root, "Filer/BusinessName/BusinessNameLine1Txt")).title(),
                             _x(root, "Filer/USAddress/CityNm"),
                             _x(root, "Filer/USAddress/StateAbbreviationCd"),
                             form, tax_year, assets, revenue, giving]
                for g in root.iter(EFILE + grp):
                    name = clean(_x(g, "PersonNm"))
                    if not name or is_entity(name):
                        continue  # banks, trust companies and other entities
                    if form == "990" and not (_x(g, "IndividualTrusteeOrDirectorInd")
                                              or _x(g, "InstitutionalTrusteeInd")
                                              or _x(g, "OfficerInd")):
                        continue  # staff listed only as key or top-paid employees
                    nm = split_first_last(name)
                    if not nm["last"] or not nm["first"]:
                        continue
                    title = clean(_x(g, "TitleTxt")).title() or None
                    comp = _int(_x(g, "ReportableCompFromOrgAmt", "CompensationAmt"))
                    k = (name.upper(), ein)
                    row = people.get(k)
                    if row is None:
                        people[k] = [name.upper(), nm["last"], nm["first"], ein, title,
                                     comp,
                                     (clean(_x(g, "USAddress/CityNm")).title() or None),
                                     _x(g, "USAddress/StateAbbreviationCd"),
                                     zip5(_x(g, "USAddress/ZIPCd"))]
                    else:
                        if title and row[4] and title not in row[4]:
                            row[4] = f"{row[4]}; {title}"
                        if comp and (row[5] or 0) < comp:
                            row[5] = comp
        p.unlink()  # free disk as we go; batches are large
        print(f"  {b}: {filings:,} filings, {len(people):,} people so far", flush=True)
    if not people:
        print("  ! no 990 data")
        return
    meta_common = {
        "source": f"IRS Form 990, 990-EZ and 990-PF e-file data (processed {y})",
        "source_url": "https://www.irs.gov/charities-non-profits/form-990-series-downloads",
        "as_of": str(y),
    }
    shards = defaultdict(list)
    for r in people.values():
        if r[3] in orgs:
            shards[surname_key(r[1])].append(r)
    people.clear()
    write_shards(out / "irs990" / "people", shards, {
        **meta_common,
        "sharded_by": "first two letters of the surname",
        "scope": ("Board members and officers of 990 filers with $1M+ in assets "
                  "or revenue, and all 990-PF private-foundation officers and "
                  "trustees, from each organization's latest filing. Home city/state/ZIP appear only "
                  "where the filer listed them (mostly 990-PF trustees). Join "
                  "ein to irs990/orgs/{first 3 digits of EIN}.json."),
        "fields": ["name", "last", "first", "ein", "title", "compensation",
                   "city", "state", "zip"],
    })
    oshards = defaultdict(list)
    for ein, o in orgs.items():
        oshards[ein[:3]].append([ein, *o])
    write_shards(out / "irs990" / "orgs", oshards, {
        **meta_common,
        "sharded_by": "first three digits of the EIN",
        "scope": "Latest filing per organization",
        "fields": ["ein", "name", "city", "state", "form", "tax_year",
                   "total_assets", "revenue", "grants_paid"],
    })


# ---------------------------------------------------------------- reference files

SURNAMES_URL = "https://www2.census.gov/topics/genealogy/2010surnames/names.zip"
IPEDS_URL = "https://nces.ed.gov/ipeds/datacenter/data/HD{y}.zip"


def registrable(host: str) -> str | None:
    """'https://www.uab.edu/path' -> 'uab.edu'; keeps 3 labels for k12/us style."""
    host = re.sub(r"^[a-z]+://", "", (host or "").strip().lower()).split("/")[0]
    host = host.split(":")[0].removeprefix("www.")
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return None
    if parts[-1] == "us" and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def resolve_campuses(cands: list) -> list:
    """Pick one institution for a domain several campuses share.

    The school whose own web address is the bare domain wins (ndsu.edu).
    Otherwise, campuses named 'X-Campus' share the system name 'X'
    (twin-cities.umn.edu, d.umn.edu -> University of Minnesota).
    """
    exact = [i for e, i in cands if e]
    if len(cands) == 1 or len(exact) == 1:
        return (exact or [cands[0][1]])[0]
    systems = {i[0].split("-")[0].strip() for _, i in cands}
    if len(systems) == 1 and all("-" in i[0] for _, i in cands):
        st = {i[2] for _, i in cands}
        return [systems.pop(), None, st.pop() if len(st) == 1 else None]
    return min((i for _, i in cands), key=lambda i: len(i[0]))


def build_ref(out: Path):
    print("Reference files (surname frequency, college domains)…", flush=True)
    base = out / "ref"
    base.mkdir(parents=True, exist_ok=True)
    # Census 2010 surnames: name and frequency only (no demographic columns).
    p = fetch(SURNAMES_URL, RAW / "ref" / "names_2010.zip", TOOL_UA)
    if p and zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as z:
            name = next(n for n in z.namelist() if n.endswith(".csv"))
            rows = []
            for r in csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig")):
                try:
                    per = float(r["prop100k"])
                except ValueError:
                    continue
                if r["name"] != "ALL OTHER NAMES" and per >= 1:
                    rows.append([r["name"], per])
        with open(base / "surnames.json", "w") as f:
            json.dump({"source": "U.S. Census Bureau, 2010 Census surnames",
                       "source_url": "https://www.census.gov/topics/population/genealogy/data/2010_surnames.html",
                       "scope": "Surnames held by at least 1 in 100,000 people; any "
                                "surname not listed is rarer than that",
                       "fields": ["surname", "per_100k"], "rows": rows},
                      f, separators=(",", ":"))
        print(f"  surnames: {len(rows):,}", flush=True)
    # IPEDS institutional directory: college website domain -> institution.
    today = dt.date.today()
    for y in range(today.year - 1, today.year - 5, -1):
        p = fetch(IPEDS_URL.format(y=y), RAW / "ref" / f"HD{y}.zip", TOOL_UA)
        if p and zipfile.is_zipfile(p):
            break
    else:
        p = None
    if p:
        doms = {}
        with zipfile.ZipFile(p) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            for r in csv.DictReader(io.TextIOWrapper(z.open(name), encoding="latin-1")):
                r = {k.lstrip("\ufeff").lstrip("ï»¿"): v for k, v in r.items()}
                web = r.get("WEBADDR", "")
                d = registrable(web)
                if not d:
                    continue
                host = re.sub(r"^[a-z]+://", "", web.strip().lower()).split("/")[0]
                exact = host.removeprefix("www.") == d
                inst = [re.sub(r"-Main Campus$", "", clean(r["INSTNM"])),
                        clean(r["CITY"]), r["STABBR"]]
                doms.setdefault(d, []).append((exact, inst))
        with open(base / "edu_domains.json", "w") as f:
            json.dump({"source": f"NCES IPEDS institutional characteristics (HD{y})",
                       "source_url": "https://nces.ed.gov/ipeds/",
                       "fields": ["institution", "city", "state"],
                       "domains": {d: resolve_campuses(v) for d, v in doms.items()}},
                      f, separators=(",", ":"))
        print(f"  college domains: {len(doms):,}", flush=True)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(ROOT / "site"))
    ap.add_argument("--sources", default="ref,faa,sec,fec,irs990")
    ap.add_argument("--fec-cycles", type=int, default=1)
    ap.add_argument("--irs-year", type=int,
                    help="IRS processing year (default: last full year)")
    ap.add_argument("--faa-zip", type=Path)
    ap.add_argument("--sec-quarters", type=int, default=4)
    a = ap.parse_args()
    out = Path(a.out)
    src = {s.strip() for s in a.sources.split(",")}
    # One source failing must not stop the others or the site deploy.
    for name, fn in (("ref", lambda: build_ref(out)),
                     ("faa", lambda: build_faa(out, a.faa_zip)),
                     ("sec", lambda: build_sec(out, a.sec_quarters)),
                     ("fec", lambda: build_fec(out, a.fec_cycles)),
                     ("irs990", lambda: build_irs990(out, a.irs_year))):
        if name in src:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                print(f"  ! {name} failed: {type(e).__name__}: {e}. Skipping.")


if __name__ == "__main__":
    sys.exit(main())
