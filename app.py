import streamlit as st
import pandas as pd
import requests
import folium
from datetime import datetime, timedelta, timezone
from streamlit_folium import st_folium
from streamlit_autorefresh import st_autorefresh
from zoneinfo import ZoneInfo


st.set_page_config(
    page_title="Waltham Shuttle — LIVE Delay Tracker",
    layout="wide"
)

# Auto-refresh every 5 seconds
st_autorefresh(interval=5000, key="live_refresh")

# ============================================================
# CONFIG
# ============================================================
st.set_page_config(
    page_title="Waltham Shuttle — Live Delay Tracker",
    layout="wide"
)

TRIPSHOT_URL = "https://brandeis.tripshot.com/v1/p/liveStatus?regionId=CA558DDC-D7F2-4B48-9CAC-DEEA1134F820"
DELAY_CSV = "waltham_shuttle_data.csv"
ROUTE_KEYWORD = "Waltham Shuttle"   # we'll match this inside routeName


# ============================================================
# STATIC STOP LIST (order matters)
# ============================================================
STOP_SEQUENCE = [
    ("Spingold (front )", 42.364790, -71.261724),
    ("Admissions", 42.364679, -71.260284),
    ("South St @ Gosman Athletic Ctr (MBTA Stop)", 42.365557386558834, -71.25539228715745),
    ("Shakespeare Rd and South St (Northbound) (MBTA Stop)", 42.368206742360236, -71.25138025536546),
    ("Highland St and South St Northbound (MBTA Stop)", 42.370053, -71.250237),
    ("Highland St & Prospect St (Teo Mini Mart)", 42.371317, -71.246643),
    ("Crescent St @ Cherry St (Watch Factory) (MBTA Stop)", 42.367492, -71.242968),
    ("Crescent St and Brown St (MBTA Stop)", 42.36585699329894, -71.24355747247441),
    ("Crescent St and Woerd Ave", 42.36382352538531, -71.24351309004835),
    ("Moody St and Washington Ave (MBTA Stop)", 42.36290718234881, -71.23883997013196),
    ("Moody St and Brown Street (MBTA Stop)", 42.36546343457749, -71.2381905664258),
    ("Moody St @ Walnut St (Solea Tapas) (MBTA Stop)", 42.36949771418865, -71.23712211378853),
    ("Moody St @ Kung Fu Tea (MBTA Stop)", 42.37189661941102, -71.23669999466624),
    ("140 Moody St @ Enterprise Rent-A-Car (MBTA Stop)", 42.373878117750316, -71.23639336793454),
    ("Moody St at Main St (Merc Apartments) (MBTA Stop)", 42.3762483822894, -71.23765476272881),
    ("Moody St and Grant Street (MBTA Stop)", 42.37626926326552, -71.24017518798874),
    ("Main St and Fiske Street (MBTA Stop)", 42.37634335783192, -71.24369572224494),
    ("Main St and Hammond St (MBTA Stop)", 42.37635596509219, -71.24666436591693),
    ("South St @ Walgreens (MBTA Stop)", 42.375504957764626, -71.25010267432268),
    ("66 South St @ Giglio Dental (MBTA Stop)", 42.37381343784146, -71.25002902886422),
    ("South St and Bedford Street (MBTA Stop)", 42.37150336056544, -71.25021850850308),
    ("Shakespeare Rd and South St (Southbound) (MBTA Stop)", 42.368398339822555, -71.25126762727875),
    ("South Street (Southbound) (MBTA Stop)", 42.3660376783752, -71.25487993106981),
    ("Counselling Center (Mailman)", 42.36601387222058, -71.25631329057543),
    ("Usdan Student Center (Across from Rabb Steps)", 42.3687768841067, -71.25716021844215),
    ("Hassenfeld Lot", 42.36658858211576, -71.26160172863395),
]


# ============================================================
# DATA HELPERS
# ============================================================
@st.cache_data(show_spinner=False)
def load_stop_df():
    rows = []
    for idx, (name, lat, lng) in enumerate(STOP_SEQUENCE):
        rows.append({"order": idx, "stop_name": name, "lat": lat, "lng": lng})
    return pd.DataFrame(rows)


@st.cache_data(show_spinner=False)
def load_delay_data():
    try:
        df = pd.read_csv(DELAY_CSV)
        return df
    except Exception as e:
        st.error(f"Could not read '{DELAY_CSV}': {e}")
        return pd.DataFrame()


def parse_route_stop_status(route_obj):
    """Turn TripShot stopStatus list into a tidy DataFrame."""
    records = []
    for via_idx, status_block in enumerate(route_obj.get("stopStatus", [])):
        if not status_block:
            continue

        # status type: "Awaiting", "Departed", etc.
        state = list(status_block.keys())[0]
        payload = status_block[state]

        scheduled = payload.get("scheduledDepartureTime") or payload.get("scheduledAt")
        eta = payload.get("expectedArrivalTime") or payload.get("expectedDepartureTime")
        stop_id = payload.get("stopId")  # ALWAYS extract stop_id

        # ---------- Correct Delay Calculation ----------
        delay_min = None
        try:
            if scheduled and eta:
                # TripShot timestamps are already UTC — no conversion needed
                scheduled_dt = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
                eta_dt = datetime.fromisoformat(eta.replace("Z", "+00:00"))
                delay_min = round((eta_dt - scheduled_dt).total_seconds() / 60, 2)
        except Exception:
            delay_min = None

        # Store parsed datetime objects
        records.append({
            "via_idx": via_idx,
            "stop_id": stop_id,
            "status_type": state,
            "scheduled_utc": scheduled_dt if scheduled else None,
            "eta_utc": eta_dt if eta else None,
            "delay_min": delay_min,
            "rider_status": payload.get("riderStatus", "")
        })

    return pd.DataFrame(records)


def enrich_with_stop_names(df, stops_df):
    """
    Attach human-readable stop names to each row based on via_idx
    (the sequence index of the route). This ignores stop_id UUIDs.
    """
    if "via_idx" in df.columns:
        df["Stop"] = df["via_idx"].apply(
            lambda i: STOP_SEQUENCE[i][0] if 0 <= i < len(STOP_SEQUENCE) else "Unknown"
        )
    else:
        # Fallback: just use whatever is there
        df["Stop"] = "Unknown"

    return df




# ---------- NEW: robust JSON scanning helpers ----------

def _find_route_obj(json_data, route_keyword=ROUTE_KEYWORD):
    """
    Recursively search through the TripShot JSON and return the first object
    that has both 'routeName' and 'stopStatus'. Prefer ones whose routeName
    contains the route_keyword (e.g., 'Waltham Shuttle').
    """
    candidates = []

    def recurse(obj):
        if isinstance(obj, dict):
            if "routeName" in obj and "stopStatus" in obj:
                candidates.append(obj)
            for v in obj.values():
                recurse(v)
        elif isinstance(obj, list):
            for x in obj:
                recurse(x)

    recurse(json_data)

    if not candidates:
        return None

    # Prefer route that matches our keyword
    for r in candidates:
        name = str(r.get("routeName", ""))
        if route_keyword.lower() in name.lower():
            return r

    # Fallback: first candidate
    return candidates[0]


def _find_vehicle_position(json_data):
    """
    Return the GPS of the vehicle whose route matches Waltham Shuttle.
    """
    target = None

    def recurse(obj, parent_route=None):
        nonlocal target
        if isinstance(obj, dict):
            rname = obj.get("routeName")
            if rname and ROUTE_KEYWORD.lower() in rname.lower():
                parent_route = obj

            if "location" in obj and parent_route:
                loc = obj["location"]
                if "lt" in loc and "lg" in loc:
                    target = (loc["lt"], loc["lg"])

            for v in obj.values():
                recurse(v, parent_route)

        elif isinstance(obj, list):
            for x in obj:
                recurse(x, parent_route)

    recurse(json_data)
    return target


def get_live_route_and_vehicle():
    """Fetch TripShot JSON and extract (route_object, vehicle_position)."""
    try:
        resp = requests.get(TRIPSHOT_URL, timeout=6)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        st.error(f"TripShot API error: {e}")
        return None, None

    route_obj = _find_route_obj(data)
    vehicle_pos = _find_vehicle_position(data)

    return route_obj, vehicle_pos


# ============================================================
# PAGE LAYOUT
# ============================================================
st.title("🚌 Waltham Shuttle — LIVE Delay Tracker")
st.caption("Real-time Waltham Shuttle monitoring using TripShot API + historical analytics.")
st.markdown("---")

col_left, col_right = st.columns([2, 1])

# =====================
# RIGHT COLUMN – DELAY SUMMARY
# =====================
with col_right:
    st.subheader("📊 Delay Summary")

    delay_df = load_delay_data()

    if delay_df.empty:
        st.info("Place `waltham_shuttle_data.csv` in the app folder.")
    else:
        # If your CSV has a 'route' column, filter by Waltham.
        if "route" in delay_df.columns:
            df = delay_df[delay_df["route"].str.contains("Waltham", case=False, na=False)]
        else:
            df = delay_df.copy()

        if df.empty:
            st.warning("CSV contains no Waltham Shuttle rows.")
        else:
            avg_delay = df["delay_minutes"].mean()
            max_delay = df["delay_minutes"].max()
            min_delay = df["delay_minutes"].min()

            on_time_pct = 100 * df["delay_minutes"].between(-1, 1).mean()
            late_pct = 100 * (df["delay_minutes"] > 1).mean()
            early_pct = 100 * (df["delay_minutes"] < -1).mean()

            st.metric("Average Delay (min)", f"{avg_delay:.2f}")
            st.metric("Max Delay (min)", f"{max_delay:.2f}")
            st.metric("Earliest (min)", f"{min_delay:.2f}")

            st.markdown(
                f"""
                **Reliability Overview**
                - On-time (±1 min): **{on_time_pct:.1f}%**  
                - Late (>1 min): **{late_pct:.1f}%**  
                - Early (<−1 min): **{early_pct:.1f}%**
                """
            )

# =====================
# LEFT COLUMN – LIVE TABLE
# =====================
with col_left:
    st.subheader("Live Shuttle Status")

    stops_df = load_stop_df()
    route_obj, vehicle_pos = get_live_route_and_vehicle()

    if route_obj is None:
        st.warning("Waltham Shuttle not running, or TripShot structure changed.")
        live_df = pd.DataFrame()
    else:
        live_df = enrich_with_stop_names(parse_route_stop_status(route_obj), stops_df)

        live_df = live_df.rename(columns={
            "via_idx": "Seq",
            "scheduled_utc": "Scheduled (UTC)",
            "eta_utc": "ETA (UTC)",
            "delay_min": "Delay (min)",
            "status_type": "Status",
            "rider_status": "Rider Status"
        })

        # Remove UUID stop column
        if "stop_id" in live_df.columns:
            live_df = live_df.drop(columns=["stop_id"])

        # Sort and show
        live_df = live_df.sort_values("Seq").reset_index(drop=True)
        st.dataframe(live_df, use_container_width=True, height=350)

st.markdown("---")

# ============================================================
# MAP
# ============================================================
st.subheader("🗺 Live Map — Stops, Path & Bus Location")

m = folium.Map(location=[42.37, -71.25], zoom_start=13, tiles="CartoDB dark_matter")

# Pathway connecting all stops
poly_coords = [(lat, lng) for (_, lat, lng) in STOP_SEQUENCE]
folium.PolyLine(poly_coords, weight=4, opacity=0.7).add_to(m)

# Stops as small blue circles
for i, (name, lat, lng) in enumerate(STOP_SEQUENCE, start=1):
    folium.CircleMarker(
        location=(lat, lng),
        radius=4,
        popup=f"{i}. {name}",
        fill=True
    ).add_to(m)

# Live bus marker if GPS available
if vehicle_pos:
    folium.Marker(
        location=vehicle_pos,
        popup="Live bus position",
        icon=folium.Icon(color="green", icon="bus", prefix="fa")
    ).add_to(m)
else:
    st.info("Bus GPS unavailable right now.")

st_folium(m, width=1200, height=520)

st.caption("Reload the page in your browser whenever you want to refresh the live data.")
