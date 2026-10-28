# DataAppend

Public data appending tools for the Theodore Roosevelt Presidential Library.

The goal is a data append service: send in a contact's first name, last name,
ZIP and email, and get back data points useful for marketing and fundraising
identification. It is being built in layers, starting with free public data.

| Layer | What it adds | Where it runs | Status |
|---|---|---|---|
| 1. ZIP reference | Neighborhood income, education, age, housing and giving context, urban/rural class and distance from Medora for every U.S. ZIP, including PO-box ZIPs | This repo → GitHub Pages (public, aggregate data only) | Built |
| 2. Person matching | FEC and state campaign-finance records, nonprofit board roles (IRS 990), SEC insider filings, email-domain signals, Library visit and giving history | Private repo, nightly GitHub Action | Planned |

**Standing rule:** no personal data (names, emails, addresses, contact
exports) is ever committed to this repository or published to its site.
Person-level matching belongs in a private repo.

## Layer 1: ZIP reference API

A static JSON API served from GitHub Pages:

- `zip/{ZIP}.json`: one ZIP's record
- `zips.csv`: every ZIP in one file, for batch joins
- `meta.json`: build date, data vintages, sources
- `index.html`: lookup page and field documentation

Example record (58645, Medora):

```json
{
  "zip": "58645", "place": "Medora", "county": "Billings County", "state": "ND",
  "miles_to_library": 3, "distance_band": "local",
  "acs": { "population": 320, "median_household_income": 78086,
           "median_household_income_reliability": "medium", "...": "..." },
  "irs": { "returns": 210, "avg_agi": 102262, "small_sample": true, "...": "..." },
  "index": { "affluence_percentile": 62, "tier": "C", "components_used": 4 }
}
```

### Sources

All free and public, downloaded in bulk with no API key:

- **U.S. Census Bureau, American Community Survey 5-year estimates**, via the
  table-based summary files: tables B01003 (population), B01002 (median age),
  B19013 (median household income), B19301 (per-capita income), B19001
  (household income brackets), B25077 (median home value), B25003 (tenure),
  B15003 (educational attainment), B01001 (age, for share 65+), B21001
  (veteran status), B11005 (households with children).
- **IRS Statistics of Income, Individual Income Tax Statistics by ZIP Code**
  (returns, AGI, dividends, capital gains, itemized charitable deductions,
  pension income, IRA distributions, business and partnership/S-corp income,
  itemized real-estate taxes).
- **USDA Economic Research Service, 2020 Rural-Urban Commuting Area codes by
  ZIP** (urban/rural class, post office name, PO-box ZIPs).
- **Census ZCTA Gazetteer** (ZIP centers) and the **2020 ZCTA-to-county and
  ZCTA-to-place relationship files** (names).

The build finds the newest published vintage of each automatically.

### Reading the data responsibly

- Figures describe a neighborhood, not a person. Use them to segment, plan and
  prioritize research, never in copy addressed to an individual.
- `small_population` (under 500 people) and `small_sample` (under 250 tax
  returns) mark noisy records. The IRS rounds and suppresses small cells.
- `median_household_income_reliability` comes from the Census margin of error
  (coefficient of variation under 15% = high, under 30% = medium, else low).
- `top_coded` lists medians the Census caps: 250,001 means $250,000 or more;
  2,000,001 means $2 million or more.
- Itemized charitable deductions and real-estate taxes reflect itemizers only;
  most households have taken the standard deduction since 2018. Compare ZIPs
  with them; don't read them as rates for everyone.
- PO-box and single-organization ZIPs (`zip_type: po_box_or_unique`) carry no
  neighborhood figures. Their distance uses the same town's residential ZIP,
  named in `point_from_zip`.
- `urban_rural` groups USDA RUCA codes: 1–3 metro, 4–6 micro, 7–9 small town,
  10 rural.
- Distances are straight-line miles from Medora, not drive miles. Bands are
  set in `DISTANCE_BANDS` at the top of the build script.

**Affluence index:** the national percentile ranks of median household income,
share of households at $200K+, median home value, share with a bachelor's
degree, and IRS average AGI (skipped for small samples) are averaged, and that
average is ranked again nationally (1–100). Tiers: A 90+, B 75–89, C 50–74,
D 25–49, E under 25. ZIPs under 200 people, or with fewer than three usable
measures, get no index.

### Building locally

Standard library only; no packages to install.

```bash
python3 -m venv .venv
source .venv/bin/activate
python pipeline/build_zip_layer.py --out site
python -m http.server -d site 8000   # then open http://localhost:8000
```

The first run downloads about 500 MB into `data/raw/` (ignored by git) and
takes a few minutes; later runs reuse the cache.

### Publishing

`.github/workflows/zip-layer.yml` rebuilds and deploys to GitHub Pages monthly,
on demand (Actions → Build ZIP reference layer → Run workflow), and on any
change to `pipeline/` or `web/`. One-time setup: **Settings → Pages → Build and
deployment → Source: GitHub Actions.**

## License

Code: MIT. Data: U.S. government works, public domain.
