#!/usr/bin/env python3
"""Build the public ZIP-level reference layer for DataAppend.

Downloads free public datasets (Census ACS 5-year, IRS SOI ZIP statistics,
Census gazetteer and ZCTA relationship files), joins them by 5-digit ZIP,
and writes one small JSON file per ZIP plus a combined CSV, ready to serve
from GitHub Pages as a static API.

Aggregate, neighborhood-level public data only. No personal data is read
or written by this script.

Standard library only. Usage:
    python3 pipeline/build_zip_layer.py --out site
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import io
import json
import math
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
WEB = ROOT / "web"

# Approximate location of the Library in Medora, ND. Distances are
# straight-line miles from here to each ZIP's internal point, not drive miles.
LIBRARY_LAT, LIBRARY_LON = 46.9139, -103.5244

# (upper bound in miles, label). Edit to match the Library's market definitions.
DISTANCE_BANDS = [
    (100, "local"),
    (300, "regional drive"),
    (600, "long drive"),
    (math.inf, "fly-in"),
]

ACS_BASE = ("https://www2.census.gov/programs-surveys/acs/summary_file/{y}/"
            "table-based-SF/data/5YRData/acsdt5y{y}-{t}.dat")
IRS_BASE = "https://www.irs.gov/pub/irs-soi/{yy}zpallagi.csv"
GAZ_BASE = ("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
            "{y}_Gazetteer/{y}_Gaz_zcta_national.zip")
REL_COUNTY = ("https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"
              "tab20_zcta520_county20_natl.txt")
REL_PLACE = ("https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"
             "tab20_zcta520_place20_natl.txt")

RUCA_ZIP = ("https://www.ers.usda.gov/media/5444/"
            "2020-rural-urban-commuting-area-codes-zip-codes.csv")

ACS_TABLES = ["b01003", "b01002", "b19013", "b19301", "b19001",
              "b25077", "b25003", "b15003", "b01001", "b21001", "b11005"]

# USDA primary RUCA code -> plain-language class
RUCA_CLASS = {1: "metro", 2: "metro", 3: "metro", 4: "micro", 5: "micro",
              6: "micro", 7: "small town", 8: "small town", 9: "small town",
              10: "rural"}
ZCTA_PREFIX = "860Z200US"

STATE_FIPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO",
    "09": "CT", "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI",
    "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY", "72": "PR",
}

UA = {"User-Agent": "TRPL-DataAppend/1.0 (+https://github.com/Theodore-Roosevelt-Presidential-Library/DataAppend)"}


# ---------------------------------------------------------------- downloads

def url_exists(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD", headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return False


def fetch(url: str, name: str) -> Path:
    """Download url into data/raw/name unless already cached."""
    RAW.mkdir(parents=True, exist_ok=True)
    dest = RAW / name
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    print(f"  downloading {url}", flush=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, length=1 << 20)
    tmp.rename(dest)
    return dest


def latest(candidates, make_url):
    for c in candidates:
        if url_exists(make_url(c)):
            return c
    raise SystemExit(f"No published file found for {make_url(candidates[0])}")


# ---------------------------------------------------------------- parsing

def num(v):
    """Parse a numeric cell; Census negative sentinels and blanks become None."""
    if v is None:
        return None
    v = v.strip()
    if not v or v in (".", "N", "(X)", "-", "**", "***", "*****"):
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    return None if x < 0 else x


def read_acs(path: Path) -> dict[str, dict[str, float | None]]:
    out = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.reader(f, delimiter="|")
        header = next(rd)
        for row in rd:
            if row and row[0].startswith(ZCTA_PREFIX):
                out[row[0][len(ZCTA_PREFIX):]] = {
                    h: num(v) for h, v in zip(header[1:], row[1:])}
    return out


def read_gazetteer(path: Path) -> dict[str, tuple[float, float]]:
    out = {}
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.endswith(".txt"))
        text = io.TextIOWrapper(z.open(name), encoding="utf-8-sig")
        first = text.readline()
        delim = "|" if first.count("|") > first.count("\t") else "\t"
        header = [h.strip() for h in first.split(delim)]
        rd = csv.reader(text, delimiter=delim)
        i_geo, i_lat, i_lon = (header.index("GEOID"), header.index("INTPTLAT"),
                               header.index("INTPTLONG"))
        for row in rd:
            out[row[i_geo].strip()] = (float(row[i_lat]), float(row[i_lon]))
    return out


PLACE_SUFFIX = re.compile(
    r"\s+(city and borough|consolidated government|unified government|"
    r"metropolitan government|city|town|village|borough|CDP|municipality|"
    r"comunidad|zona urbana)( \(balance\))?$", re.I)


def read_relationship(path: Path, geoid_col: str, name_col: str,
                      prefer_col: str | None = None):
    """For each ZCTA, the related geography with the largest land overlap.

    With prefer_col, rows whose value there is "A" (an active government,
    i.e. an incorporated city or town) win over statistical areas such as
    CDPs, so ZIP 59101 is labeled Billings rather than Lockwood.
    """
    best = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f, delimiter="|")
        for row in rd:
            z, g = row["GEOID_ZCTA5_20"], row[geoid_col]
            if not z or not g:
                continue
            rank = (row.get(prefer_col) == "A" if prefer_col else True,
                    float(row["AREALAND_PART"] or 0))
            if z not in best or rank > best[z][0]:
                best[z] = (rank, g, row[name_col])
    return {z: (g, n) for z, (_, g, n) in best.items()}


def read_ruca(path: Path) -> dict[str, dict]:
    out = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            z = row["ZIPCode"].strip().zfill(5)
            try:
                code = int(float(row["PrimaryRUCA"]))
            except (TypeError, ValueError):
                code = None
            out[z] = {"state": row["State"].strip(),
                      "po_name": row["POName"].strip().title() or None,
                      "zip_type": row["ZIPCodeType"].strip(),
                      "ruca": code}
    return out


def read_irs(path: Path) -> dict[str, dict]:
    keep = ("N1", "A00100", "N00600", "N01000", "N19700", "A19700",
            "N01400", "N01700", "N00900", "N26270", "N18500", "A18500")
    agg = defaultdict(lambda: defaultdict(float))
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            z = row["zipcode"].strip().zfill(5)
            if z in ("00000", "99999"):
                continue
            a = agg[z]
            a["state"] = row["STATE"]
            for k in keep:
                a[k] += float(row[k] or 0)
            if row["agi_stub"].strip() == "6":
                a["N1_200k"] += float(row["N1"] or 0)
    return agg


# ---------------------------------------------------------------- helpers

def miles(lat1, lon1, lat2, lon2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def band(mi):
    return next(label for limit, label in DISTANCE_BANDS if mi < limit)


def pct(part, whole, nd=1):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, nd)


def money(x):
    return None if x is None else int(round(x))


def reliability_from_moe(est, moe):
    """Census guidance: coefficient of variation = (MOE / 1.645) / estimate."""
    if not est or moe is None:
        return None
    cv = (moe / 1.645) / est
    return "high" if cv < 0.15 else "medium" if cv < 0.30 else "low"


class Ranker:
    """Percentile rank (0-100) of a value within a reference distribution."""

    def __init__(self, values):
        self.v = sorted(x for x in values if x is not None)

    def __call__(self, x):
        if x is None or not self.v:
            return None
        lo = bisect.bisect_left(self.v, x)
        hi = bisect.bisect_right(self.v, x)
        return 100.0 * (lo + 0.5 * (hi - lo)) / len(self.v)


# ---------------------------------------------------------------- build

def build(out: Path, acs_year: int | None, irs_year: int | None):
    today = dt.date.today()

    print("Locating latest vintages…", flush=True)
    acs_year = acs_year or latest(
        list(range(today.year - 1, today.year - 6, -1)),
        lambda y: ACS_BASE.format(y=y, t="b19013"))
    irs_year = irs_year or latest(
        list(range(today.year - 2, today.year - 7, -1)),
        lambda y: IRS_BASE.format(yy=str(y)[2:]))
    gaz_year = latest(list(range(today.year, today.year - 6, -1)),
                      lambda y: GAZ_BASE.format(y=y))
    print(f"  ACS 5-year ending {acs_year}; IRS tax year {irs_year}; "
          f"gazetteer {gaz_year}", flush=True)

    print("Downloading (cached in data/raw)…", flush=True)
    acs_files = {t: fetch(ACS_BASE.format(y=acs_year, t=t),
                          f"acsdt5y{acs_year}-{t}.dat") for t in ACS_TABLES}
    irs_file = fetch(IRS_BASE.format(yy=str(irs_year)[2:]),
                     f"{str(irs_year)[2:]}zpallagi.csv")
    gaz_file = fetch(GAZ_BASE.format(y=gaz_year), f"{gaz_year}_Gaz_zcta_national.zip")
    county_file = fetch(REL_COUNTY, "tab20_zcta520_county20_natl.txt")
    place_file = fetch(REL_PLACE, "tab20_zcta520_place20_natl.txt")
    ruca_file = fetch(RUCA_ZIP, "ruca_zip_2020.csv")

    print("Parsing…", flush=True)
    ruca = read_ruca(ruca_file)
    acs = {t: read_acs(p) for t, p in acs_files.items()}
    irs = read_irs(irs_file)
    gaz = read_gazetteer(gaz_file)
    county = read_relationship(county_file, "GEOID_COUNTY_20", "NAMELSAD_COUNTY_20")
    place = read_relationship(place_file, "GEOID_PLACE_20", "NAMELSAD_PLACE_20",
                              prefer_col="FUNCSTAT_PLACE_20")

    # PO-box and large-volume ZIPs have no Census or IRS data but do appear
    # in mailing lists, so they get a record with mailing city and RUCA class.
    po_zips = {z for z, v in ruca.items() if v["zip_type"].startswith("Post Office")}
    zips = sorted(set(acs["b01003"]) | set(irs) | po_zips)

    # For PO-box ZIPs, borrow the map point of the most populous residential
    # ZIP with the same post office name and state (e.g. 58602 -> 58601).
    town_zip = {}
    for z, v in ruca.items():
        if z in gaz and v["po_name"]:
            key = (v["state"], v["po_name"])
            p = acs["b01003"].get(z, {}).get("B01003_E001") or 0
            if key not in town_zip or p > town_zip[key][0]:
                town_zip[key] = (p, z)
    rec = {}
    for z in zips:
        g = lambda t, c: acs[t].get(z, {}).get(c)
        pop = g("b01003", "B01003_E001")
        hh = g("b19001", "B19001_E001")
        occ = g("b25003", "B25003_E001")
        ed = g("b15003", "B15003_E001")
        mhi, mhi_moe = g("b19013", "B19013_E001"), g("b19013", "B19013_M001")
        bach = None
        if ed:
            bach = sum((g("b15003", f"B15003_E0{n}") or 0) for n in (22, 23, 24, 25))
        grad = None
        if ed:
            grad = sum((g("b15003", f"B15003_E0{n}") or 0) for n in (23, 24, 25))

        st = cty = plc = None
        if z in county:
            geoid, cty = county[z]
            st = STATE_FIPS.get(geoid[:2])
        if z in place:
            plc = PLACE_SUFFIX.sub("", place[z][1]).strip()
        i = irs.get(z)
        u = ruca.get(z, {})
        if not st and i:
            st = i["state"]
        if not st:
            st = u.get("state")

        lat = lon = mi = point_from = None
        src = z if z in gaz else None
        if not src and u.get("po_name"):
            src = town_zip.get((u["state"], u["po_name"]), (0, None))[1]
            point_from = src
        if src:
            lat, lon = gaz[src]
            mi = miles(LIBRARY_LAT, LIBRARY_LON, lat, lon)

        r = {
            "zip": z,
            "mail_city": u.get("po_name"),
            "place": plc,
            "county": cty,
            "state": st,
            "zip_type": ("po_box_or_unique" if z in po_zips else "area"),
            "ruca": u.get("ruca"),
            "urban_rural": RUCA_CLASS.get(u.get("ruca")),
            "lat": round(lat, 4) if lat is not None else None,
            "lon": round(lon, 4) if lon is not None else None,
            "miles_to_library": round(mi) if mi is not None else None,
            "distance_band": band(mi) if mi is not None else None,
            "point_from_zip": point_from,
            "acs": None,
            "irs": None,
            "index": None,
        }
        if z in acs["b01003"]:
            mhv = g("b25077", "B25077_E001")
            # ACS top-codes medians: 250,001 means "$250,000 or more" and
            # 2,000,001 means "$2,000,000 or more".
            top = [k for k, v, cap in (("median_household_income", mhi, 250001),
                                       ("median_home_value", mhv, 2000001))
                   if v is not None and v >= cap]
            r["acs"] = {
                "population": money(pop),
                "median_age": g("b01002", "B01002_E001"),
                "households": money(hh),
                "median_household_income": money(mhi),
                "median_household_income_reliability": reliability_from_moe(mhi, mhi_moe),
                "per_capita_income": money(g("b19301", "B19301_E001")),
                "pct_households_200k_plus": pct(g("b19001", "B19001_E017"), hh),
                "median_home_value": money(mhv),
                "top_coded": top,
                "pct_owner_occupied": pct(g("b25003", "B25003_E002"), occ),
                "pct_bachelors_plus": pct(bach, ed),
                "pct_graduate_degree": pct(grad, ed),
                "pct_age_65_plus": pct(
                    sum((g("b01001", f"B01001_E0{n:02d}") or 0)
                        for n in (*range(20, 26), *range(44, 50))),
                    g("b01001", "B01001_E001")),
                "pct_veterans": pct(g("b21001", "B21001_E002"),
                                    g("b21001", "B21001_E001")),
                "pct_households_with_children": pct(g("b11005", "B11005_E002"),
                                                    g("b11005", "B11005_E001")),
                "small_population": bool(pop is not None and pop < 500),
            }
        if i and i["N1"]:
            n = i["N1"]
            r["irs"] = {
                "returns": int(n),
                "avg_agi": money(i["A00100"] * 1000 / n),
                "pct_returns_200k_plus": pct(i["N1_200k"], n),
                "pct_returns_with_dividends": pct(i["N00600"], n),
                "pct_returns_with_capital_gains": pct(i["N01000"], n),
                "pct_returns_itemizing_charity": pct(i["N19700"], n),
                "avg_itemized_charitable_deduction":
                    money(i["A19700"] * 1000 / i["N19700"]) if i["N19700"] else None,
                "pct_returns_with_pension_income": pct(i["N01700"], n),
                "pct_returns_with_ira_distributions": pct(i["N01400"], n),
                "pct_returns_with_business_income": pct(i["N00900"], n),
                "pct_returns_with_partnership_scorp_income": pct(i["N26270"], n),
                "avg_itemized_real_estate_taxes":
                    money(i["A18500"] * 1000 / i["N18500"]) if i["N18500"] else None,
                "small_sample": n < 250,
            }
        rec[z] = r

    # ---- Affluence index: average of national percentile ranks, re-ranked.
    def eligible(r):
        a = r["acs"]
        return a and a["population"] and a["population"] >= 200

    comps = {
        "median_household_income": lambda r: r["acs"]["median_household_income"],
        "pct_households_200k_plus": lambda r: r["acs"]["pct_households_200k_plus"],
        "median_home_value": lambda r: r["acs"]["median_home_value"],
        "pct_bachelors_plus": lambda r: r["acs"]["pct_bachelors_plus"],
        "irs_avg_agi": lambda r: (r["irs"]["avg_agi"]
                                  if r["irs"] and not r["irs"]["small_sample"] else None),
    }
    pool = [r for r in rec.values() if eligible(r)]
    rankers = {k: Ranker(f(r) for r in pool) for k, f in comps.items()}
    composite = {}
    for r in pool:
        ranks = [rankers[k](f(r)) for k, f in comps.items()]
        ranks = [x for x in ranks if x is not None]
        if len(ranks) >= 3:
            composite[r["zip"]] = (sum(ranks) / len(ranks), len(ranks))
    final = Ranker(v for v, _ in composite.values())
    for z, (v, n) in composite.items():
        p = max(1, min(100, round(final(v))))
        tier = ("A" if p >= 90 else "B" if p >= 75 else "C" if p >= 50
                else "D" if p >= 25 else "E")
        rec[z]["index"] = {"affluence_percentile": p, "tier": tier,
                           "components_used": n}

    # ---- Write output
    print(f"Writing {len(rec):,} ZIP records to {out}/…", flush=True)
    if out.exists():
        shutil.rmtree(out)
    (out / "zip").mkdir(parents=True)
    for z, r in rec.items():
        with open(out / "zip" / f"{z}.json", "w") as f:
            json.dump(r, f, separators=(",", ":"))

    flat_cols = ["zip", "mail_city", "place", "county", "state", "zip_type",
                 "ruca", "urban_rural", "lat", "lon",
                 "miles_to_library", "distance_band", "point_from_zip"]
    acs_cols = list(next(r for r in rec.values() if r["acs"])["acs"].keys())
    irs_cols = list(next(r for r in rec.values() if r["irs"])["irs"].keys())
    idx_cols = ["affluence_percentile", "tier", "components_used"]
    with open(out / "zips.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(flat_cols + [f"acs_{c}" for c in acs_cols]
                   + [f"irs_{c}" for c in irs_cols] + idx_cols)
        cell = lambda v: ";".join(v) if isinstance(v, list) else v
        for r in rec.values():
            w.writerow([r[c] for c in flat_cols]
                       + [cell((r["acs"] or {}).get(c)) for c in acs_cols]
                       + [(r["irs"] or {}).get(c) for c in irs_cols]
                       + [(r["index"] or {}).get(c) for c in idx_cols])

    meta = {
        "built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "zip_count": len(rec),
        "indexed_count": len(composite),
        "library_point": {"lat": LIBRARY_LAT, "lon": LIBRARY_LON},
        "distance_bands_miles": [[None if math.isinf(l) else l, n]
                                 for l, n in DISTANCE_BANDS],
        "sources": {
            "acs": {"name": f"U.S. Census Bureau, American Community Survey "
                            f"{acs_year - 4}-{acs_year} 5-year estimates",
                    "tables": [t.upper() for t in ACS_TABLES],
                    "url": ACS_BASE.format(y=acs_year, t="<table>")},
            "irs": {"name": f"IRS Statistics of Income, Individual Income Tax "
                            f"Statistics by ZIP Code, tax year {irs_year}",
                    "url": IRS_BASE.format(yy=str(irs_year)[2:])},
            "ruca": {"name": "USDA Economic Research Service, 2020 Rural-Urban "
                             "Commuting Area codes by ZIP code",
                     "url": RUCA_ZIP},
            "geography": {"name": f"Census {gaz_year} ZCTA Gazetteer; 2020 "
                                  f"ZCTA-to-county and ZCTA-to-place relationship files",
                          "url": [GAZ_BASE.format(y=gaz_year), REL_COUNTY, REL_PLACE]},
        },
    }
    with open(out / "meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    if WEB.exists():
        for p in WEB.iterdir():
            if p.is_file():
                shutil.copy(p, out / p.name)
    (out / ".nojekyll").touch()
    print(f"Done: {len(rec):,} ZIPs, {len(composite):,} with an affluence index.")
    return rec


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(ROOT / "site"), help="output folder")
    ap.add_argument("--acs-year", type=int, help="ACS 5-year end year (default: latest)")
    ap.add_argument("--irs-year", type=int, help="IRS tax year (default: latest)")
    a = ap.parse_args()
    build(Path(a.out), a.acs_year, a.irs_year)


if __name__ == "__main__":
    sys.exit(main())
