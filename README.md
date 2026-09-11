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

## Ideas for further polish (not yet implemented)

- Cache `calculate_distance` results per (lat, lon) pair.
- Add a small loading skeleton/progress bar for first coastline load if the
  `.gpkg` is large.
- Move the coastline file to object storage (e.g. an R2/S3 bucket or a
  release asset) and download-and-cache it on first run, keeping the repo
  itself small.
