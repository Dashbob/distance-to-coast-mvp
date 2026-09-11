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

# Rough NZ mainland bounding box (excludes outlying islands), used to bias
# and constrain address autocomplete results.
NZ_VIEWBOX = "166.0,-34.0,179.5,-47.5"  # left,top,right,bottom (lon/lat)

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
@st.cache_data(show_spinner=False, ttl=60 * 60)
def _nz_address_candidates(searchterm: str):
    """
    Returns live suggestions for the address search box, restricted to New
    Zealand by both `countrycodes` and a bounding viewbox. Only addresses
    Nominatim actually resolves are offered, so picking one from the
    dropdown *is* the validation step — there's no separate "is this a real
    NZ address" check needed downstream, and no risk of a stray click
    calculating a distance for an unresolved/garbage address.
    """
    searchterm = (searchterm or "").strip()
    if len(searchterm) < 3:
        return []

    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": searchterm,
        "format": "json",
        "limit": 6,
        "countrycodes": "nz",
        "viewbox": NZ_VIEWBOX,
        "bounded": 1,
    }
    # Nominatim's usage policy requires a real identifying User-Agent.
    # Swap in your project name / contact so requests aren't blocked.
    headers = {"User-Agent": "nz-coast-distance-streamlit-app (contact: you@example.com)"}

    try:
        r = requests.get(url, params=params, headers=headers, timeout=8)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    return [
        (
            item["display_name"],
            {"label": item["display_name"], "lat": float(item["lat"]), "lon": float(item["lon"])},
        )
        for item in data
    ]


def search_nz_addresses(searchterm: str):
    return _nz_address_candidates(searchterm)


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
def classify_colorsteel_environment(distance_m: float):
    """
    Approximate mapping to New Zealand Steel's published COLORSTEEL(R)
    Environmental Categories, using the *West coast* distance bands (the
    more conservative of the two published sets) so this never under-states
    corrosion risk. Real boundaries differ by East vs West coast and are
    adjusted further by prevailing wind, breaking surf vs calm water, and
    other site factors -- this is indicative only, not a warranty
    determination. See colorsteel.co.nz/warranty and NZ Steel's
    Environmental Categories & Warranty guide for the authoritative version.
    """
    bands = [
        ("Extremely severe", 0, 50,
         "Frequently outside standard residential warranty eligibility. Direct confirmation from Colorsteel/New Zealand Steel is generally required."),
        ("Very severe", 50, 500,
         "Product choice is limited (e.g. marine-grade options). Confirm warranty eligibility before specifying."),
        ("Severe", 500, 1000,
         "Most COLORSTEEL(R) product ranges are warrantable, but the specific product/coating matters."),
        ("Moderate", 1000, 5000,
         "Covers the majority of New Zealand. Standard COLORSTEEL(R) ranges are typically warrantable here."),
        ("Mild", 5000, float("inf"),
         "Least corrosive category. Broadest product choice; full standard warranty terms typically apply."),
    ]
    for name, lo, hi, note in bands:
        if lo <= distance_m < hi:
            return name, note
    return "Unknown", ""


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
    tier, note = classify_colorsteel_environment(res["distance_m"])
    with st.expander(f"🏠 Estimated Colorsteel® environmental category: {tier} (guidance only)"):
        st.write(note)
        st.caption(
            "Indicative only — based on New Zealand Steel's published COLORSTEEL® Environmental "
            "Categories & Warranty guide, using the more conservative West-coast distance bands. "
            "Actual category boundaries differ between the East and West coast and are affected by "
            "prevailing wind, breaking surf vs. calm water, and other site-specific factors. "
            "For anything within 100 m of a salt water body — and before relying on this for a real "
            "project — confirm directly with Colorsteel: https://colorsteel.co.nz/warranty"
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
