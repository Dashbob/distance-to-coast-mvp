import streamlit as st
import requests
import geopandas as gpd
from shapely.geometry import Point
from shapely.ops import nearest_points
from pyproj import Transformer
import folium
from streamlit_folium import st_folium

# =========================
# CONFIG & PAGE SETUP
# =========================
st.set_page_config(page_title="NZ Coast Distance Tool", page_icon="🌊")

COAST_CACHE = "linz_coast_50258.gpkg"
NZTM_EPSG = 2193
WGS84_EPSG = 4326

# =========================
# CRS TRANSFORMS
# =========================
to_nztm = Transformer.from_crs(f"EPSG:{WGS84_EPSG}", f"EPSG:{NZTM_EPSG}", always_xy=True)
to_wgs84 = Transformer.from_crs(f"EPSG:{NZTM_EPSG}", f"EPSG:{WGS84_EPSG}", always_xy=True)


def wgs84_to_nztm(lon, lat):
    return to_nztm.transform(lon, lat)


def nztm_to_wgs84(x, y):
    return to_wgs84.transform(x, y)


# =========================
# ADDRESS VALIDATION (LINZ DATA SERVICE — KEYED, NOT A SHARED PUBLIC ENDPOINT)
# =========================
# Two free, unauthenticated public geocoders were tried and both hit
# reliability walls: Nominatim's usage policy forbids autocomplete and
# blocks at the IP level (which also meant our earlier per-keystroke
# testing could get an entire shared-hosting IP range blocked), and a
# Photon fallback started returning HTTP errors under repeated requests.
# LINZ's own "NZ Addresses" dataset, queried through the LINZ Data Service
# WFS API with a personal API key, sidesteps both problems: auth is by key
# rather than by IP, and it's the authoritative NZ address source besides.
#
# Get a free key: sign in at https://data.linz.govt.nz -> avatar menu ->
# "My API keys" -> generate a key with the default read-only scope (this
# grants access to all public layers, including layer 105689 used below).
# Store it as LINZ_API_KEY in .streamlit/secrets.toml locally, and in the
# app's "Secrets" settings on Streamlit Community Cloud when deployed.
LINZ_ADDRESS_LAYER = "layer-123113"  # "NZ Addresses" (replaced NZ Street Address, Jan 2023)
LINZ_WFS_TEMPLATE = "https://data.linz.govt.nz/services;key={api_key}/wfs"


def _cql_escape(term: str) -> str:
    """Escape single quotes for safe inclusion in a CQL_FILTER literal."""
    return term.replace("'", "''")


def geocode_address_candidates(address: str, limit: int = 8):
    """
    Returns (candidates, error). candidates is a list of
    {"label", "lat", "lon"} dicts sourced from LINZ's authoritative NZ
    Addresses dataset — if the list is empty (and there's no error)
    nothing matched, which is itself the validation signal.
    """
    address = (address or "").strip()
    if not address:
        return [], "Please enter an address."

    api_key = st.secrets.get("LINZ_API_KEY")
    if not api_key:
        return [], (
            "No LINZ API key configured. Add LINZ_API_KEY to .streamlit/secrets.toml "
            "(or the app's Secrets settings if deployed) — see the README for how to get a free key."
        )

    term = _cql_escape(address)
    cql_filter = (
        f"full_road_name ILIKE '%{term}%' "
        f"OR suburb_locality ILIKE '%{term}%' "
        f"OR town_city ILIKE '%{term}%'"
    )
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": LINZ_ADDRESS_LAYER,
        "outputFormat": "json",
        "SRSName": "EPSG:4326",
        "count": limit,
        "cql_filter": cql_filter,
    }
    url = LINZ_WFS_TEMPLATE.format(api_key=api_key)

    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.HTTPError:
        status = r.status_code if "r" in locals() else "unknown"
        return [], f"Address search failed (HTTP {status} from LINZ). Check that your API key is valid and active."
    except Exception as e:
        return [], f"Address search failed ({e.__class__.__name__}). Check your network connection."

    candidates = []
    for feat in data.get("features", []):
        props = feat.get("properties", {}) or {}
        coords = (feat.get("geometry") or {}).get("coordinates")
        if not coords:
            continue
        lon, lat = coords[0], coords[1]

        number = props.get("full_address_number") or props.get("address_number") or ""
        street = props.get("full_road_name") or ""
        street_line = f"{number} {street}".strip()
        suburb = props.get("suburb_locality") or ""
        city = props.get("town_city") or ""

        label = ", ".join(p for p in [street_line, suburb, city] if p)
        if not label:
            continue

        candidates.append({"label": label, "lat": lat, "lon": lon})

    if not candidates:
        return [], "No matching New Zealand address found. Try a more specific search, or use the map instead."
    return candidates, None


# =========================
# COASTLINE LOAD (CACHED AS A RESOURCE, NOT RE-SERIALIZED)
# =========================
@st.cache_resource(show_spinner=False)
def load_coastline():
    try:
        # pyogrio is the fast GeoPandas I/O backend and ships its own GDAL
        # binaries in the wheel — no apt-get/packages.txt GDAL install
        # needed on Streamlit Community Cloud.
        gdf = gpd.read_file(COAST_CACHE, engine="pyogrio")
    except Exception as e:
        st.error(f"Error loading coastline data: {e}")
        st.stop()

    if gdf.crs is None:
        st.error("Coastline file has no CRS defined — cannot safely reproject.")
        st.stop()
    if gdf.crs.to_epsg() != NZTM_EPSG:
        gdf = gdf.to_crs(epsg=NZTM_EPSG)

    _ = gdf.sindex  # build the spatial index once, up front
    return gdf


# =========================
# NEAREST COAST LOGIC (spatial-index accelerated)
# =========================
def calculate_distance(lat: float, lon: float, coast_gdf, label: str):
    x, y = wgs84_to_nztm(lon, lat)
    p = Point(x, y)

    nearest_idx, _ = coast_gdf.sindex.nearest(p, return_all=False, return_distance=True)
    tree_idx = nearest_idx[1][0]
    row = coast_gdf.iloc[tree_idx]
    geom = row.geometry

    nearest = nearest_points(p, geom)[1]
    dist = p.distance(nearest)
    coast_lon, coast_lat = nztm_to_wgs84(nearest.x, nearest.y)

    return {
        "label": label,
        "input_latlon": (lat, lon),
        "distance_m": dist,
        "nearest_coast_latlon": (coast_lat, coast_lon),
        "coast_side": row.get("coast_side", "Unknown"),
    }


# =========================
# COLORSTEEL ENVIRONMENTAL CATEGORY GUIDANCE
# =========================
# New Zealand Steel's published COLORSTEEL(R) Environmental Categories &
# Warranty guide gives *different* distance bands for the East and West
# coasts (the West coast is treated as more exposed/corrosive at the same
# distance, given prevailing weather and breaking surf). Boundaries below
# are in metres from the coastline.
_COLORSTEEL_BANDS = {
    "east": [
        ("Extremely severe", 0, 25),
        ("Very severe", 25, 100),
        ("Severe", 100, 500),
        ("Moderate", 500, 5000),
        ("Mild", 5000, float("inf")),
    ],
    "west": [
        ("Extremely severe", 0, 50),
        ("Very severe", 50, 500),
        ("Severe", 500, 1000),
        ("Moderate", 1000, 5000),
        ("Mild", 5000, float("inf")),
    ],
}

_COLORSTEEL_NOTES = {
    "Extremely severe": "Frequently outside standard residential warranty eligibility. Direct confirmation from Colorsteel/New Zealand Steel is generally required.",
    "Very severe": "Product choice is limited (e.g. marine-grade options). Confirm warranty eligibility before specifying.",
    "Severe": "Most COLORSTEEL(R) product ranges are warrantable, but the specific product/coating matters.",
    "Moderate": "Covers the majority of New Zealand. Standard COLORSTEEL(R) ranges are typically warrantable here.",
    "Mild": "Least corrosive category. Broadest product choice; full standard warranty terms typically apply.",
}


def classify_colorsteel_environment(distance_m: float, coast_side: str):
    """
    Maps a distance-to-coast to an indicative COLORSTEEL(R) environmental
    category using the East or West coast bands, chosen from the
    `coast_side` attribute already present in the LINZ coastline layer
    (rather than assuming a side). If the side can't be determined from the
    data, falls back to the more conservative West-coast bands so this
    never under-states corrosion risk, and flags that fallback in the
    returned tuple.

    This is indicative only, not a warranty determination -- boundaries are
    further adjusted in reality by prevailing wind, breaking surf vs calm
    water, and other site-specific factors. See colorsteel.co.nz/warranty
    and NZ Steel's Environmental Categories & Warranty guide for the
    authoritative version.

    Returns: (tier_name, note, resolved_side, side_was_unknown)
    """
    side_raw = (coast_side or "").strip().lower()
    if "west" in side_raw:
        resolved_side = "west"
        side_unknown = False
    elif "east" in side_raw:
        resolved_side = "east"
        side_unknown = False
    else:
        resolved_side = "west"  # unknown -> conservative fallback
        side_unknown = True

    for name, lo, hi in _COLORSTEEL_BANDS[resolved_side]:
        if lo <= distance_m < hi:
            return name, _COLORSTEEL_NOTES[name], resolved_side, side_unknown
    return "Unknown", "", resolved_side, side_unknown


# =========================
# STREAMLIT UI
# =========================
st.title("🌊 NZ Coast Distance Calculator")
st.write("Search for a New Zealand address, confirm the match, then calculate its distance to the nearest coastline.")

with st.spinner("Loading coastline data..."):
    coast_data = load_coastline()

if "calc_result" not in st.session_state:
    st.session_state.calc_result = None
if "manual_fallback" not in st.session_state:
    st.session_state.manual_fallback = False
if "address_candidates" not in st.session_state:
    st.session_state.address_candidates = []
if "address_search_error" not in st.session_state:
    st.session_state.address_search_error = None

# --- INPUT SECTION: search on submit, then confirm the resolved address ---
address_input = st.text_input("Enter a New Zealand address:", placeholder="e.g., Sky Tower, Auckland")

if st.button("Search address"):
    with st.spinner("Searching..."):
        candidates, error = geocode_address_candidates(address_input)
        st.session_state.address_candidates = candidates
        st.session_state.address_search_error = error
        st.session_state.calc_result = None

if st.session_state.address_search_error:
    st.warning(st.session_state.address_search_error)

selected_address = None
if st.session_state.address_candidates:
    options = {c["label"]: c for c in st.session_state.address_candidates}
    picked_label = st.selectbox(
        "We found these matches — confirm the right one:",
        list(options.keys()),
        key="address_pick",
    )
    selected_address = options[picked_label]

col1, col2 = st.columns([3, 2])
with col1:
    calc_clicked = st.button("Calculate Distance", type="primary", disabled=selected_address is None)
with col2:
    if st.button("Can't find your address? Use map instead"):
        st.session_state.manual_fallback = True

if calc_clicked and selected_address:
    with st.spinner("Calculating..."):
        st.session_state.calc_result = calculate_distance(
            selected_address["lat"], selected_address["lon"], coast_data, selected_address["label"]
        )
        st.session_state.manual_fallback = False

# --- FALLBACK MAP SECTION ---
if st.session_state.manual_fallback:
    st.info("Click anywhere on the map to select your location.")

    m = folium.Map(location=[-37.893402, 175.466771], zoom_start=5, tiles="CartoDB positron")
    map_data = st_folium(m, width=720, height=500, key="fallback_map")

    if map_data and map_data.get("last_clicked"):
        clicked_lat = map_data["last_clicked"]["lat"]
        clicked_lon = map_data["last_clicked"]["lng"]

        with st.spinner("Calculating distance from pin..."):
            st.session_state.calc_result = calculate_distance(
                clicked_lat, clicked_lon, coast_data, f"Manual Coordinates: {clicked_lat:.5f}, {clicked_lon:.5f}"
            )

        st.session_state.manual_fallback = False
        st.rerun()

# --- RESULTS SECTION ---
if st.session_state.calc_result and not st.session_state.manual_fallback:
    res = st.session_state.calc_result
    st.success("Calculation complete!")
    st.subheader(f"Results for: {res['label']}")

    col1, col2 = st.columns(2)
    with col1:
        dist_km = res["distance_m"] / 1000
        st.metric("Distance to Coast", f"{dist_km:.2f} km")
    with col2:
        st.metric("Coast Side", str(res["coast_side"]).title())

    # --- Colorsteel warranty-environment guidance ---
    tier, note, resolved_side, side_unknown = classify_colorsteel_environment(
        res["distance_m"], res["coast_side"]
    )
    with st.expander(
        f"🏠 Estimated Colorsteel® environmental category: {tier} ({resolved_side.title()} coast bands)"
    ):
        st.write(note)
        if side_unknown:
            st.caption(
                f"The coastline dataset didn't return a usable East/West side for this point "
                f"(coast_side = '{res['coast_side']}'), so the more conservative West-coast bands "
                f"were used as a fallback."
            )
        else:
            st.caption(f"Based on this point's coast_side value from the coastline dataset: '{res['coast_side']}'.")
        st.caption(
            "Indicative only — actual category boundaries are also affected by prevailing wind, "
            "breaking surf vs. calm water, and other site-specific factors. For anything within "
            "100 m of a salt water body — and before relying on this for a real project — confirm "
            "directly with Colorsteel: https://colorsteel.co.nz/warranty"
        )

    st.write("**Map View:**")

    lat1, lon1 = res["input_latlon"]
    lat2, lon2 = res["nearest_coast_latlon"]
    center_lat = (lat1 + lat2) / 2
    center_lon = (lon1 + lon2) / 2

    res_map = folium.Map(location=[center_lat, center_lon], zoom_start=11, tiles="CartoDB positron")

    folium.Marker([lat1, lon1], tooltip="Your Location", icon=folium.Icon(color="red", icon="home")).add_to(res_map)
    folium.Marker([lat2, lon2], tooltip="Nearest Coast", icon=folium.Icon(color="blue", icon="tint")).add_to(res_map)
    folium.PolyLine(
        locations=[[lat1, lon1], [lat2, lon2]], color="red", weight=3, dash_array="10, 10", opacity=0.8
    ).add_to(res_map)

    st_folium(res_map, width=720, height=500, returned_objects=[], key="result_map")
