import time

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
# GEOCODING (cached so repeat lookups of the same address don't
# hit Nominatim again — also respects their fair-use rate limits)
# =========================
@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def geocode_address(address: str):
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": address + ", New Zealand", "format": "json", "limit": 1}
    # Nominatim's usage policy requires a real identifying User-Agent.
    # Swap in your project name / contact so requests aren't blocked.
    headers = {"User-Agent": "nz-coast-distance-streamlit-app (contact: you@example.com)"}

    r = requests.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()

    data = r.json()
    if not data:
        raise ValueError(f"Address not found: {address}")

    return float(data[0]["lat"]), float(data[0]["lon"])


# =========================
# COASTLINE LOAD (CACHED AS A RESOURCE, NOT RE-SERIALIZED)
# =========================
@st.cache_resource(show_spinner=False)
def load_coastline():
    """
    st.cache_resource (not cache_data) because a GeoDataFrame is a large,
    unhashable, non-trivially-serializable object we only ever read from.
    cache_resource keeps a single in-memory instance across reruns/sessions
    instead of pickling/copying it every time.
    """
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

    # Building the spatial index once, up front, means every subsequent
    # nearest-neighbour query is O(log n) instead of a full O(n) scan.
    _ = gdf.sindex
    return gdf


# =========================
# NEAREST COAST LOGIC (spatial-index accelerated)
# =========================
def calculate_distance(lat: float, lon: float, coast_gdf, label: str):
    """
    Uses the GeoDataFrame's spatial index (STRtree under shapely 2.0) to
    find the nearest coastline feature in O(log n) instead of looping over
    every row. For LINZ coastline data split into many segments/tiles this
    is dramatically faster and scales to large datasets.
    """
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
# STREAMLIT UI
# =========================
st.title("🌊 NZ Coast Distance Calculator")
st.write("Enter an address to find its distance to the nearest coastline.")

with st.spinner("Loading coastline data..."):
    coast_data = load_coastline()

if "calc_result" not in st.session_state:
    st.session_state.calc_result = None
if "manual_fallback" not in st.session_state:
    st.session_state.manual_fallback = False

# --- INPUT SECTION ---
address_input = st.text_input("Enter a New Zealand address:", placeholder="e.g., Sky Tower, Auckland")

if st.button("Calculate Distance", type="primary"):
    if not address_input.strip():
        st.warning("Please enter an address.")
    else:
        with st.spinner("Searching for address..."):
            try:
                lat, lon = geocode_address(address_input)
                st.session_state.calc_result = calculate_distance(lat, lon, coast_data, address_input)
                st.session_state.manual_fallback = False
            except Exception:
                st.error("Address not found or network error. Please drop a pin on the map below instead.")
                st.session_state.manual_fallback = True
                st.session_state.calc_result = None

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
