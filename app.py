import streamlit as st
import requests
import geopandas as gpd
from shapely.geometry import Point
from shapely.ops import nearest_points
from pyproj import Transformer
import folium
from streamlit_folium import st_folium
from streamlit_searchbox import st_searchbox

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
# ADDRESS VALIDATION / AUTOCOMPLETE
# =========================
# NOTE: Nominatim (OSM) is deliberately NOT used here. Its own usage policy
# lists "auto-complete search" implemented client-side as strictly
# forbidden ("This is not yet supported by Nominatim and you must not
# implement such a service on the client side using the API" —
# operations.osmfoundation.org/policies/nominatim). In practice this is why
# the previous version appeared to query but never returned suggestions:
# type-ahead-style requests get silently dropped/blocked. Photon
# (komoot.io) is a free, public geocoder explicitly built for
# search-as-you-type and is the right tool for this job.
PHOTON_URL = "https://photon.komoot.io/api/"
NZ_BBOX = "166.0,-47.5,179.5,-34.0"  # min_lon,min_lat,max_lon,max_lat


@st.cache_data(show_spinner=False, ttl=60 * 60)
def _nz_address_candidates(searchterm: str):
    """
    Returns (suggestions, error) for the address search box. Suggestions
    are restricted to New Zealand via Photon's bbox filter (a hard filter,
    not just a bias). Only addresses Photon actually resolves are offered,
    so picking one from the dropdown *is* the validation step.
    """
    searchterm = (searchterm or "").strip()
    if len(searchterm) < 3:
        return [], None

    params = {"q": searchterm, "limit": 6, "lang": "en", "bbox": NZ_BBOX}

    try:
        r = requests.get(PHOTON_URL, params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return [], f"Address search failed ({e.__class__.__name__}). Check your network connection."

    suggestions = []
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        coords = (feat.get("geometry") or {}).get("coordinates")
        if not coords:
            continue
        lon, lat = coords[0], coords[1]

        label_parts = [
            props.get("name"),
            " ".join(p for p in [props.get("housenumber"), props.get("street")] if p) or None,
            props.get("city") or props.get("district"),
            props.get("state"),
            props.get("postcode"),
        ]
        # dedupe while preserving order (e.g. name == city for some POIs)
        label = ", ".join(dict.fromkeys(p for p in label_parts if p))
        if not label:
            continue

        suggestions.append((label, {"label": label, "lat": lat, "lon": lon}))

    return suggestions, None


def search_nz_addresses(searchterm: str):
    suggestions, error = _nz_address_candidates(searchterm)
    st.session_state["_address_search_error"] = error
    return suggestions


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
st.write("Start typing a New Zealand address and pick it from the list to find its distance to the nearest coastline.")

with st.spinner("Loading coastline data..."):
    coast_data = load_coastline()

if "calc_result" not in st.session_state:
    st.session_state.calc_result = None
if "manual_fallback" not in st.session_state:
    st.session_state.manual_fallback = False

# --- INPUT SECTION: live-validated address search ---
selected_address = st_searchbox(
    search_nz_addresses,
    placeholder="e.g., Sky Tower, Auckland",
    label="Enter a New Zealand address:",
    key="address_searchbox",
    clear_on_submit=False,
)
if st.session_state.get("_address_search_error"):
    st.caption(f"⚠️ {st.session_state['_address_search_error']}")

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
