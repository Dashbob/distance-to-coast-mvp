# NZ Coast Distance Calculator

A small Streamlit app that geocodes a New Zealand address and reports the
straight-line distance to the nearest coastline, using LINZ coastline data.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Make sure `linz_coast_50258.gpkg` sits next to `app.py`.

## Deploying to Streamlit Community Cloud

1. Push this repo to GitHub (include the `.gpkg` file, or fetch it at
   startup from cloud storage if it's large — GitHub blocks files over
   100 MB, and repos over ~1 GB get flagged).
2. On [share.streamlit.io](https://share.streamlit.io), point at your repo
   and `app.py`.
3. No `packages.txt` / apt GDAL install is required — the app reads the
   coastline file with the `pyogrio` engine, which ships its own GDAL
   binaries in the wheel. That removes the most common cause of broken
   deploys for GeoPandas apps on Streamlit Cloud.

## What changed from the MVP version

- **Nearest-coast lookup now uses the spatial index** (`coast_gdf.sindex.nearest`)
  instead of a Python `for` loop over every coastline row. This turns an
  O(n) scan into an O(log n) index query — the difference matters a lot if
  the coastline layer is split into many tiles/segments.
- **Coastline data is cached with `st.cache_resource`** instead of
  `st.cache_data`. `cache_data` pickles/hashes its return value, which is
  expensive and sometimes unreliable for a large GeoDataFrame; `cache_resource`
  just keeps one shared in-memory object.
- **Geocoding results are cached** (`st.cache_data`, 24h TTL) so re-running
  the same address doesn't hit Nominatim again — keeps the demo responsive
  and plays nicer with Nominatim's fair-use rate limits.
- **CRS safety check** on load: reprojects to NZTM (EPSG:2193) automatically
  if the source file isn't already in that CRS, instead of silently assuming
  it is.
- **Dependency swap (fiona → pyogrio)** so the deploy doesn't depend on a
  system GDAL install.

## Address validation

The address field is now a live search box (`streamlit-searchbox`) backed by
Nominatim, restricted to New Zealand via `countrycodes=nz` and a bounding
viewbox. Suggestions populate as you type (debounced, 3+ characters), and
you can only submit a calculation by picking one of the resolved
suggestions — so an address is "valid" by construction rather than checked
after the fact. The manual pin-drop map is still there as a fallback for
addresses Nominatim can't resolve.

## Colorsteel® warranty-environment guidance

Once a distance is calculated, an expander shows an *indicative* COLORSTEEL®
environmental category (Mild / Moderate / Severe / Very Severe / Extremely
Severe) based on New Zealand Steel's published Environmental Categories &
Warranty guide (colorsteel.co.nz/warranty). A few important caveats, carried
into the app's own disclaimer text:

- The guide publishes **separate distance bands for the East and West
  coast** (the West coast, being more exposed to prevailing weather, uses
  larger distances for the same category). This app uses the more
  conservative West-coast bands so it never *understates* corrosion risk,
  but the real category for an East-coast address may be one step milder.
- Boundaries are further adjusted by wind exposure and whether the water is
  breaking surf or a calm harbour/estuary — a straight-line distance can't
  capture that.
- New Zealand Steel explicitly states that **anything within 100 m of a
  salt water body needs direct confirmation from Colorsteel** for warranty
  purposes, and very severe/extremely severe sites are often outside
  standard residential warranty eligibility without that confirmation.

This feature is guidance for demo purposes only, not a warranty
determination — the in-app copy says as much and links to
colorsteel.co.nz/warranty for the authoritative source.

## Ideas for further polish (not yet implemented)

- Cache `calculate_distance` results per (lat, lon) pair.
- Add a small loading skeleton/progress bar for first coastline load if the
  `.gpkg` is large.
- Move the coastline file to object storage (e.g. an R2/S3 bucket or a
  release asset) and download-and-cache it on first run, keeping the repo
  itself small.
