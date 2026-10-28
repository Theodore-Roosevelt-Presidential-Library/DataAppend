# DataAppend

Public data appending tools for the Theodore Roosevelt Presidential Library.

The goal is a data append service: send in a contact's first name, last name,
ZIP and email, and get back data points useful for marketing and fundraising
identification. It is being built in layers, starting with free public data.

| Layer | What it adds | Where it runs | Status |
|---|---|---|---|
| 1. ZIP reference | Neighborhood income, education, age, housing and giving context, urban/rural class and distance from Medora for every U.S. ZIP, including PO-box ZIPs | This repo → GitHub Pages (public, aggregate data only) | Built |
| 2. Person-level public records | FAA aircraft owners, SEC insiders (built); FEC, NPPES, IRS 990 officers (planned) | This repo → GitHub Pages, one folder per source | In progress |
| 3. Matching | Compares the Library's contacts with layer 2 locally | Private: your Mac, homelab or a private repo | Built (`pipeline/match_people.py`) |

**Standing rule:** the Library's own contact data (contact exports, match
results, emails) is never committed to this repository or published to its
site. Public-record layers republish what the agencies publish, without street
addresses. Matching against contacts runs privately.

## Layer 1: ZIP reference API

A static JSON API served from GitHub Pages at **https://append.labs.trlibrary.com**:

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
change to `pipeline/` or `web/`.

One-time setup in **Settings → Pages**:

1. Build and deployment → Source: **GitHub Actions**
2. Custom domain: **append.labs.trlibrary.com**, then check **Enforce HTTPS**
   once the certificate is issued

DNS needs no change: `*.labs.trlibrary.com` already points to
`theodore-roosevelt-presidential-library.github.io`. The domain lives in the
Pages setting rather than a `CNAME` file, because Actions-based deployments
ignore that file.

## Layer 2: person-level public records

Built by `pipeline/build_people_layer.py` into the same site, one folder per
source. Each file republishes the agency's own public data, minus street
addresses, and every folder has a `meta.json` with source, date and scope.

| Path | Source | Sharded by | Contents |
|---|---|---|---|
| `faa/aircraft/{ZIP}.json` | FAA Aircraft Registry | ZIP of registered address | Individual and co-owner registrations: name, city, state, N-number, make, model, year, aircraft and engine type, seats |
| `sec/insiders/{AB}.json` | SEC Insider Transactions Data Sets (Forms 3/4/5), last 4 quarters | First two letters of surname | Officers, directors and 10% owners: name, filing city/state/ZIP, issuer, ticker, relationship, title, last filing date |

Downloads identify themselves as this tool. Two sources need care:

- **FAA** refuses automated downloads from cloud servers, so the registry file
  is refreshed by hand (monthly is plenty) and attached to the `faa-data`
  release, where the Action picks it up:

  Download https://registry.faa.gov/database/ReleasableAircraft.zip in a
  browser (the FAA also refuses `curl`, and saves a block page under the
  zip's name). Check it is about 70 MB, then:

  ```bash
  cd ~/Downloads
  # First time only:
  gh release create faa-data ReleasableAircraft.zip --title "FAA registry input" \
      --notes "FAA releasable aircraft database, downloaded by hand." \
      -R Theodore-Roosevelt-Presidential-Library/DataAppend
  # Every later refresh:
  gh release upload faa-data ReleasableAircraft.zip --clobber \
      -R Theodore-Roosevelt-Presidential-Library/DataAppend
  gh workflow run zip-layer.yml -R Theodore-Roosevelt-Presidential-Library/DataAppend
  ```

  Without the GitHub CLI, upload it on the repo's Releases page. If a bad file
  is attached, the build logs a warning and skips the FAA layer.
- **SEC** requires a real contact email in the User-Agent under its fair-access
  policy. Set the repository secret `SEC_USER_AGENT` to
  `TRPL DataAppend name@trlibrary.com`.

## Layer 3: matching (run privately)

```bash
python pipeline/match_people.py contacts.csv matches.csv \
    --base https://append.labs.trlibrary.com
```

Reads a CSV with first name, last name, ZIP and (optionally) email and id. It
fetches only the shard files it needs, by ZIP or surname prefix, so no contact
details leave the machine. Each match carries a confidence level:

- First name exact (3), nickname such as Bill/William (2), or initial (1)
- Same ZIP (+2) or same state (+1)
- Email domain matches an SEC issuer (+2)
- High = 5 or more, medium = 3–4, low = under 3. Default output is medium and up.

On a synthetic test of 200 aircraft owners (with nicknames, initials and
dropped leading zeros) and 200 invented people in the same ZIPs, it matched
all 200 owners and none of the invented people. Real lists will match less
often because people move and change names.

Never commit `contacts*.csv` or `matches*.csv` (both are in `.gitignore`).

## License

Code: MIT. Data: U.S. government works, public domain.
