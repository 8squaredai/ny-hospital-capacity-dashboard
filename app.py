from __future__ import annotations

import json

import numpy as np
import pandas as pd
import plotly.express as px
import pydeck as pdk
import streamlit as st

st.set_page_config(page_title="NY Hospital Capacity", layout="wide")

DATA_PATH = "data/hospital_history.csv"
FACILITIES_PATH = "data/ny_health_facilities.csv"
GEOJSON_PATH = "data/ny_counties.geojson"
SOURCE_URL = "https://health.data.ny.gov/Health/New-York-State-Statewide-Hospital-Bed-Capacity/2dbc-sqe7"

NUMERIC_COLS = [
    "Total Staffed Acute Care Beds",
    "Total Staffed Acute Care Beds Occupied",
    "Total Staffed Acute Care Beds Available",
    "Total Staffed ICU Beds",
    "Total Staffed ICU Beds Currently Occupied",
    "Total Staffed ICU Beds Currently Available",
]

# Maps DOH's verbose regional-office names to the short labels used throughout the UI.
REGION_LABELS = {
    "CAPITAL DISTRICT REGIONAL OFFICE": "Capital District",
    "CENTRAL NEW YORK REGIONAL OFFICE": "Central New York",
    "METROPOLITAN AREA REGIONAL OFFICE - LONG ISLAND": "Long Island",
    "METROPOLITAN AREA REGIONAL OFFICE - NEW ROCHELLE": "Hudson Valley",
    "METROPOLITAN AREA REGIONAL OFFICE - NEW YORK CITY": "New York City",
    "WESTERN REGIONAL OFFICE - BUFFALO": "Western NY (Buffalo)",
    "WESTERN REGIONAL OFFICE - ROCHESTER": "Western NY (Rochester)",
}
NYC_BOROUGH_MAP = {
    "NEW YORK": "Manhattan",
    "KINGS": "Brooklyn",
    "BRONX": "Bronx",
    "QUEENS": "Queens",
    "RICHMOND": "Staten Island",
}

# Analytical/app thresholds only -- not medical or regulatory standards. Matches the
# three-tier system documented on the About Data page (Available / High occupancy / Critical).
STATUS_THRESHOLDS = (75, 90)
STATUS_LABELS = ["Available", "High occupancy", "Critical"]
STATUS_COLORS = {
    0: ("#dcf5e3", "#1a7a3c"),
    1: ("#fdecc8", "#8a5b00"),
    2: ("#fbdada", "#b42318"),
}
STATUS_MAP_COLORS = {0: "#2fa84f", 1: "#e0a52c", 2: "#d1453b"}

# A few hospitals report occupancy over 100% -- verified against the source data, not a
# calculation bug (Occupied + Available == Staffed holds exactly). Causes vary by facility:
# patient boarding beyond the staffed bed count, short-notice staffing reductions, or a
# "staffed beds" figure that's stale or narrower in definition than "occupied".
OVER_100_NOTE = (
    "Occupancy can exceed 100% for some hospitals -- this reflects patient boarding, "
    "short-notice staffing reductions, or inconsistencies in self-reported facility "
    "data, not a calculation error."
)

METRICS = {
    "Acute Occupancy %": dict(kind="pct", num="Total Staffed Acute Care Beds Occupied", den="Total Staffed Acute Care Beds"),
    "ICU Occupancy %": dict(kind="pct", num="Total Staffed ICU Beds Currently Occupied", den="Total Staffed ICU Beds"),
    "Available Acute Beds": dict(kind="sum", col="Total Staffed Acute Care Beds Available"),
    "Available ICU Beds": dict(kind="sum", col="Total Staffed ICU Beds Currently Available"),
}

ACCENT = "#2a78d6"


@st.cache_data
def load_facility_coords(path: str) -> pd.DataFrame:
    """NY DOH's facility directory covers every licensed facility type (hospitals, nursing
    homes, clinics, ...), keyed by Facility ID -- the same identifier as our bed-capacity
    data's Facility PFI. A handful of IDs appear as exact duplicate rows, so dedupe first.
    """
    fac = pd.read_csv(path, low_memory=False)
    fac = fac.drop_duplicates(subset="Facility ID", keep="first")
    fac = fac[
        [
            "Facility ID",
            "Facility Latitude",
            "Facility Longitude",
            "Facility Zip Code",
            "Facility Address 1",
            "Facility City",
            "Description",
        ]
    ]
    return fac.rename(
        columns={
            "Facility ID": "Facility PFI",
            "Facility Latitude": "Latitude",
            "Facility Longitude": "Longitude",
            "Facility Zip Code": "Zip Code",
            "Facility Address 1": "Address",
            "Facility City": "City",
            "Description": "Hospital Type",
        }
    )


@st.cache_data
def load_data(path: str, facilities_path: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    df = pd.read_csv(path, low_memory=False)

    for col in NUMERIC_COLS:
        df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", "", regex=False), errors="coerce")

    df["As of Date"] = pd.to_datetime(df["As of Date"], format="%m/%d/%Y")
    df = df.drop_duplicates(subset=["Facility PFI", "As of Date"], keep="last")

    staffed_acute = df["Total Staffed Acute Care Beds"].replace(0, np.nan)
    staffed_icu = df["Total Staffed ICU Beds"].replace(0, np.nan)
    df["Acute Occupancy %"] = df["Total Staffed Acute Care Beds Occupied"] / staffed_acute * 100
    df["ICU Occupancy %"] = df["Total Staffed ICU Beds Currently Occupied"] / staffed_icu * 100

    # Attach location/type here, before slicing to the latest date, so both the full
    # history and the latest-date snapshot carry it. A left join means a hospital with
    # no match simply comes through as NaN -- the map/filters downstream just handle that.
    facility_info = load_facility_coords(facilities_path)
    df = df.merge(facility_info, on="Facility PFI", how="left")
    df["Region"] = df["DOH Region"].map(REGION_LABELS).fillna("Unknown")
    df["Borough"] = df["Facility County"].map(NYC_BOROUGH_MAP)
    df["Hospital Type"] = df["Hospital Type"].fillna("Unknown")

    latest_date = df["As of Date"].max()
    latest_df = df[df["As of Date"] == latest_date].copy()

    return df, latest_df, latest_date


def status_bucket(pct: float) -> int | None:
    if pd.isna(pct):
        return None
    low, high = STATUS_THRESHOLDS
    if pct >= high:
        return 2
    if pct >= low:
        return 1
    return 0


def worst_status_bucket(acute_pct: float, icu_pct: float) -> int | None:
    """A hospital high on either acute or ICU counts -- worst case, not an average."""
    buckets = [b for b in (status_bucket(acute_pct), status_bucket(icu_pct)) if b is not None]
    return max(buckets) if buckets else None


def status_label(bucket: int | None) -> str:
    if bucket is None or pd.isna(bucket):
        return "N/A"
    return STATUS_LABELS[int(bucket)]


def status_cell_style(label: str) -> str:
    if label not in STATUS_LABELS:
        return ""
    bg, fg = STATUS_COLORS[STATUS_LABELS.index(label)]
    return f"background-color: {bg}; color: {fg}; border-radius: 999px; font-weight: 600; text-align: center;"


def status_badge_html(bucket: int | None, uppercase: bool = False, suffix: str = "") -> str:
    if bucket is None:
        bg, fg, text = "#e7e6e2", "#52514e", "N/A"
    else:
        bg, fg = STATUS_COLORS[bucket]
        text = STATUS_LABELS[bucket] + suffix
    if uppercase:
        text = text.upper()
    return (
        f'<span style="background:{bg};color:{fg};padding:6px 16px;border-radius:999px;'
        f'font-weight:700;font-size:13px;white-space:nowrap;">{text}</span>'
    )


def fmt_pct(value: float) -> str:
    return f"{value:.1f}%" if pd.notna(value) else "N/A"


def fmt_int(value: float) -> str:
    return f"{value:,.0f}" if pd.notna(value) else "N/A"


def kpi_card(label: str, value: str, bucket: int | None = None) -> None:
    if bucket is None:
        bg, fg = "#fcfcfb", "#0b0b0b"
        border = "1px solid #e1e0d9"
    else:
        bg, fg = STATUS_COLORS[bucket]
        border = "none"
    st.markdown(
        f"""
        <div style="background:{bg};border:{border};border-radius:10px;
                     padding:16px 18px;margin-bottom:10px;">
            <div style="font-size:28px;color:{fg};font-weight:700;">{value}</div>
            <div style="font-size:13px;color:{fg};opacity:0.85;margin-top:2px;">{label}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def aggregate_occupancy(rows: pd.DataFrame, group_col: str) -> pd.DataFrame:
    """Weighted (sum-of-beds) occupancy per group, not an average of percentages."""
    agg = rows.groupby(group_col).agg(
        facility_count=("Facility PFI", "count"),
        staffed_acute=("Total Staffed Acute Care Beds", "sum"),
        occupied_acute=("Total Staffed Acute Care Beds Occupied", "sum"),
        available_acute=("Total Staffed Acute Care Beds Available", "sum"),
        staffed_icu=("Total Staffed ICU Beds", "sum"),
        occupied_icu=("Total Staffed ICU Beds Currently Occupied", "sum"),
        available_icu=("Total Staffed ICU Beds Currently Available", "sum"),
    )
    agg["Acute Occupancy %"] = agg["occupied_acute"] / agg["staffed_acute"].replace(0, np.nan) * 100
    agg["ICU Occupancy %"] = agg["occupied_icu"] / agg["staffed_icu"].replace(0, np.nan) * 100
    return agg


def metric_snapshot(rows: pd.DataFrame, metric: str, group_col: str | None = None):
    """Value of a METRICS entry, either statewide (group_col=None) or grouped."""
    spec = METRICS[metric]
    if group_col is None:
        if spec["kind"] == "pct":
            num, den = rows[spec["num"]].sum(), rows[spec["den"]].sum()
            return num / den * 100 if den else np.nan
        return rows[spec["col"]].sum()

    if spec["kind"] == "pct":
        g = rows.groupby(group_col)[[spec["num"], spec["den"]]].sum()
        return (g[spec["num"]] / g[spec["den"]].replace(0, np.nan) * 100).rename(metric)
    return rows.groupby(group_col)[spec["col"]].sum().rename(metric)


def metric_timeseries(history: pd.DataFrame, metric: str) -> pd.DataFrame:
    spec = METRICS[metric]
    if spec["kind"] == "pct":
        g = history.groupby("As of Date")[[spec["num"], spec["den"]]].sum().reset_index()
        g[metric] = g[spec["num"]] / g[spec["den"]].replace(0, np.nan) * 100
    else:
        g = history.groupby("As of Date")[spec["col"]].sum().reset_index().rename(columns={spec["col"]: metric})
    return g[["As of Date", metric]]


def plain_layout(fig, height: int | None = None) -> None:
    fig.update_layout(
        plot_bgcolor="#fcfcfb",
        paper_bgcolor="#fcfcfb",
        font_color="#0b0b0b",
        margin=dict(l=10, r=10, t=10, b=10),
    )
    if height:
        fig.update_layout(height=height)


def searchable_text(rows: pd.DataFrame) -> pd.Series:
    return (
        rows["Facility Name"].fillna("")
        + " "
        + rows["Facility County"].fillna("")
        + " "
        + rows["Borough"].fillna("")
    ).str.lower()


def filter_bar(rows: pd.DataFrame, key_prefix: str, search_placeholder: str) -> pd.DataFrame:
    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
    with c1:
        search = st.text_input(search_placeholder, key=f"{key_prefix}_search", label_visibility="collapsed", placeholder=search_placeholder)
    with c2:
        region = st.selectbox("Region", ["All regions"] + sorted(rows["Region"].unique()), key=f"{key_prefix}_region")
    with c3:
        htype = st.selectbox("Hospital type", ["All types"] + sorted(rows["Hospital Type"].unique()), key=f"{key_prefix}_type")
    with c4:
        status = st.selectbox("Capacity status", ["All statuses"] + STATUS_LABELS, key=f"{key_prefix}_status")

    filtered = rows.copy()
    if search:
        filtered = filtered[searchable_text(filtered).str.contains(search.lower())]
    if region != "All regions":
        filtered = filtered[filtered["Region"] == region]
    if htype != "All types":
        filtered = filtered[filtered["Hospital Type"] == htype]
    if status != "All statuses":
        bucket = STATUS_LABELS.index(status)
        filtered = filtered[
            [worst_status_bucket(a, i) == bucket for a, i in zip(filtered["Acute Occupancy %"], filtered["ICU Occupancy %"])]
        ]
    return filtered


def open_detail(pfi, return_page: str) -> None:
    st.session_state.detail_pfi = pfi
    st.session_state.return_page = return_page
    st.rerun()


def render_marker_map(rows: pd.DataFrame, key: str, zoom: float = 5.7, center: dict | None = None) -> None:
    map_df = rows.dropna(subset=["Latitude", "Longitude"]).copy()
    excluded = len(rows) - len(map_df)
    if map_df.empty:
        st.info("No hospital coordinates available for this selection.")
        return

    map_df["Status Bucket"] = [
        worst_status_bucket(a, i) for a, i in zip(map_df["Acute Occupancy %"], map_df["ICU Occupancy %"])
    ]
    map_df["Status"] = map_df["Status Bucket"].apply(status_label)
    map_df["Occupancy Label"] = map_df["Acute Occupancy %"].apply(fmt_pct)

    fig = px.scatter_map(
        map_df,
        lat="Latitude",
        lon="Longitude",
        color="Status",
        color_discrete_map={STATUS_LABELS[b]: c for b, c in STATUS_MAP_COLORS.items()},
        category_orders={"Status": STATUS_LABELS},
        hover_name="Facility Name",
        hover_data={"Latitude": False, "Longitude": False, "Status": True, "Occupancy Label": True},
        custom_data=["Facility PFI"],
        zoom=zoom,
        center=center,
        height=460,
        map_style="open-street-map",
    )
    fig.update_traces(marker=dict(size=11))
    fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), legend_title_text="")
    event = st.plotly_chart(fig, width="stretch", on_select="rerun", key=key)

    caption = f"{len(map_df)} hospitals shown."
    if excluded:
        caption += f" {excluded} excluded (no coordinates on file)."
    caption += " Click a marker to open its detail page."
    st.caption(caption)

    points = event.get("selection", {}).get("points", []) if event else []
    if points:
        pfi = points[0]["customdata"][0]
        open_detail(pfi, st.session_state.page)


def hex_to_rgba(hex_color: str, alpha: int = 200) -> list[int]:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    return [r, g, b, alpha]


def render_3d_map(rows: pd.DataFrame, radius: int = 1500, elevation_scale: int = 200) -> None:
    """One 3D bar per hospital at its real coordinates; height and color both encode acute
    occupancy pressure. No native click-to-select in pydeck, unlike the marker map, so this
    view is for scanning statewide shape rather than drilling into a specific hospital."""
    map_df = rows.dropna(subset=["Latitude", "Longitude"]).copy()
    excluded = len(rows) - len(map_df)
    if map_df.empty:
        st.info("No hospital coordinates available for this selection.")
        return

    map_df["bucket"] = [
        worst_status_bucket(a, i) for a, i in zip(map_df["Acute Occupancy %"], map_df["ICU Occupancy %"])
    ]
    map_df["fill_color"] = map_df["bucket"].apply(
        lambda b: hex_to_rgba(STATUS_MAP_COLORS[int(b)]) if pd.notna(b) else [137, 135, 129, 160]
    )
    # Bar height capped at 100% so the handful of hospitals reporting over 100% (see
    # OVER_100_NOTE) don't visually dwarf every other bar -- color and the tooltip still
    # reflect the true, uncapped percentage.
    map_df["elevation"] = map_df["Acute Occupancy %"].fillna(0).clip(upper=100)
    map_df["occupancy_label"] = map_df["Acute Occupancy %"].apply(fmt_pct)

    layer = pdk.Layer(
        "ColumnLayer",
        data=map_df,
        get_position=["Longitude", "Latitude"],
        get_elevation="elevation",
        elevation_scale=elevation_scale,
        radius=radius,
        get_fill_color="fill_color",
        pickable=True,
        auto_highlight=True,
    )
    st.pydeck_chart(
        pdk.Deck(
            layers=[layer],
            initial_view_state=pdk.ViewState(latitude=42.9, longitude=-75.5, zoom=6, pitch=45, bearing=0),
            map_style=None,
            tooltip={"text": "{Facility Name}\nAcute occupancy: {occupancy_label}"},
        )
    )
    caption = f"{len(map_df)} hospitals mapped."
    if excluded:
        caption += f" {excluded} excluded (no coordinates on file)."
    caption += " Bar height and color both encode acute occupancy pressure."
    st.caption(caption)


@st.cache_data
def load_geojson(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


GEOJSON = load_geojson(GEOJSON_PATH)
FIPS_LOOKUP = {f["properties"]["NAME"].upper(): f["id"] for f in GEOJSON["features"]}
SEQUENTIAL_BLUE = ["#cde2fb", "#6da7ec", "#256abf", "#0d366b"]


def render_choropleth_map(rows: pd.DataFrame) -> None:
    county_agg = aggregate_occupancy(rows, "Facility County").reset_index()
    county_agg["fips"] = county_agg["Facility County"].str.upper().map(FIPS_LOOKUP)
    plot_df = county_agg.dropna(subset=["fips"])
    if plot_df.empty:
        st.info("No county data available for this selection.")
        return

    fig = px.choropleth(
        plot_df,
        geojson=GEOJSON,
        locations="fips",
        featureidkey="id",
        color="Acute Occupancy %",
        color_continuous_scale=SEQUENTIAL_BLUE,
        hover_name="Facility County",
        hover_data={"fips": False, "Acute Occupancy %": ":.1f", "ICU Occupancy %": ":.1f"},
        height=460,
    )
    fig.update_geos(fitbounds="locations", visible=False)
    fig.update_layout(
        margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor="#fcfcfb",
        coloraxis_colorbar=dict(title="Acute Occ. %"),
    )
    st.plotly_chart(fig, width="stretch")
    st.caption(f"{len(plot_df)} counties shown, colored by weighted acute occupancy. Choropleth has no drill-down -- use the Markers view to open a hospital's detail page.")


def render_hospital_table(rows: pd.DataFrame, key: str, columns: list[str] | None = None) -> None:
    cols = columns or ["Facility Name", "Region", "Total Staffed Acute Care Beds Available", "Total Staffed ICU Beds Currently Available", "Acute Occupancy %", "Status"]
    table = rows.copy()
    table["Status"] = [status_label(worst_status_bucket(a, i)) for a, i in zip(table["Acute Occupancy %"], table["ICU Occupancy %"])]
    table = table.sort_values("Acute Occupancy %", ascending=False).reset_index(drop=True)
    display = table[cols].rename(
        columns={
            "Facility Name": "Hospital",
            "Total Staffed Acute Care Beds Available": "Beds free",
            "Total Staffed ICU Beds Currently Available": "ICU free",
            "Acute Occupancy %": "Occupancy",
        }
    )
    st.caption(f"{len(display)} hospitals — sorted by occupancy. Click a column header to re-sort.")
    event = st.dataframe(
        display.style.format({"Occupancy": "{:.1f}%"}).map(status_cell_style, subset=["Status"]),
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=key,
    )
    rows_selected = event.get("selection", {}).get("rows", []) if event else []
    if rows_selected:
        pfi = table.iloc[rows_selected[0]]["Facility PFI"]
        open_detail(pfi, st.session_state.page)


# ---------------------------------------------------------------------------
# Data load
# ---------------------------------------------------------------------------

history_df, df, latest_date = load_data(DATA_PATH, FACILITIES_PATH)

if "page" not in st.session_state:
    st.session_state.page = "Overview"
if "detail_pfi" not in st.session_state:
    st.session_state.detail_pfi = None
if "return_page" not in st.session_state:
    st.session_state.return_page = "Overview"

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.markdown(
    f"""
    <div style="background:#132a4e;margin:-1rem -1rem 1rem -1rem;padding:20px 2rem;
                 display:flex;justify-content:space-between;align-items:flex-end;">
        <div>
            <div style="color:white;font-size:26px;font-weight:800;">NY Hospital Capacity</div>
            <div style="color:#c3cbdb;font-size:14px;margin-top:2px;">
                Hospital pressure and bed availability across New York
            </div>
        </div>
        <div style="color:#c3cbdb;font-size:14px;">
            Data as of {latest_date.strftime('%B %d, %Y')}
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

PAGES = ["Overview", "Hospitals", "Trends", "Compare", "About Data"]

# ---------------------------------------------------------------------------
# Hospital Detail (drill-down -- not a top-level tab)
# ---------------------------------------------------------------------------

if st.session_state.detail_pfi is not None:
    hosp_rows = df[df["Facility PFI"] == st.session_state.detail_pfi]
    if hosp_rows.empty:
        st.session_state.detail_pfi = None
        st.rerun()
    hosp = hosp_rows.iloc[0]

    if st.button(f"← Back to {st.session_state.return_page}"):
        st.session_state.detail_pfi = None
        st.session_state.page = st.session_state.return_page
        st.rerun()

    bucket = worst_status_bucket(hosp["Acute Occupancy %"], hosp["ICU Occupancy %"])
    title_col, badge_col = st.columns([4, 1])
    with title_col:
        st.markdown(f"### {hosp['Facility Name']}")
        location = ", ".join(x for x in [hosp["City"] if pd.notna(hosp["City"]) else None, hosp["Facility County"]] if x)
        st.caption(location)
    with badge_col:
        st.markdown(status_badge_html(bucket, uppercase=True, suffix=" CAPACITY"), unsafe_allow_html=True)

    hist = history_df[history_df["Facility PFI"] == hosp["Facility PFI"]].sort_values("As of Date")
    prev_row = hist[hist["As of Date"] == latest_date - pd.Timedelta(days=1)]
    change = hosp["Acute Occupancy %"] - prev_row["Acute Occupancy %"].iloc[0] if not prev_row.empty else np.nan

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Occupancy", fmt_pct(hosp["Acute Occupancy %"]))
    k2.metric("Beds free", fmt_int(hosp["Total Staffed Acute Care Beds Available"]))
    k3.metric("ICU beds free", fmt_int(hosp["Total Staffed ICU Beds Currently Available"]))
    k4.metric("24h change", f"{change:+.1f} pts" if pd.notna(change) else "N/A")
    if hosp["Acute Occupancy %"] > 100 or hosp["ICU Occupancy %"] > 100:
        st.caption(OVER_100_NOTE)

    chart_col, side_col = st.columns([2, 1])
    with chart_col:
        st.markdown("#### Capacity trend")
        hist = hist.copy()
        hist["Acute 7-Day Avg"] = hist["Acute Occupancy %"].rolling(7, min_periods=1).mean()
        fig = px.line(hist, x="As of Date", y="Acute Occupancy %")
        fig.data[0].line.color = "#cde2fb"
        fig.data[0].line.width = 1
        fig.add_scatter(x=hist["As of Date"], y=hist["Acute 7-Day Avg"], mode="lines", name="7-Day Avg", line=dict(color=ACCENT, width=2.5))
        plain_layout(fig, height=320)
        st.plotly_chart(fig, width="stretch")

    with side_col:
        st.markdown("#### Bed availability")
        acute_frac = hosp["Total Staffed Acute Care Beds Occupied"] / hosp["Total Staffed Acute Care Beds"] if hosp["Total Staffed Acute Care Beds"] else 0
        icu_frac = hosp["Total Staffed ICU Beds Currently Occupied"] / hosp["Total Staffed ICU Beds"] if hosp["Total Staffed ICU Beds"] else 0
        st.write(f"General beds — {fmt_int(hosp['Total Staffed Acute Care Beds Available'])} / {fmt_int(hosp['Total Staffed Acute Care Beds'])} free")
        st.progress(min(max(acute_frac, 0), 1))
        st.write(f"ICU beds — {fmt_int(hosp['Total Staffed ICU Beds Currently Available'])} / {fmt_int(hosp['Total Staffed ICU Beds'])} free")
        st.progress(min(max(icu_frac, 0), 1))
        emergency_label = {0: "Low pressure", 1: "Moderate pressure", 2: "High pressure"}.get(bucket, "N/A")
        st.write(f"Emergency pressure (proxy) — {emergency_label}")
        st.progress(min(max(acute_frac, 0), 1))
        st.caption("Derived from acute occupancy -- the dataset does not include emergency-department-specific data.")

    st.markdown("#### Hospital information")
    address = hosp["Address"] if pd.notna(hosp["Address"]) else "Not on file"
    info_cols = st.columns(4)
    info_cols[0].markdown(f"**Address**\n\n{address}")
    info_cols[1].markdown(f"**Hospital type**\n\n{hosp['Hospital Type']}")
    info_cols[2].markdown(f"**Network**\n\n{hosp['Facility Network']}")
    info_cols[3].markdown(f"**Last updated**\n\n{hosp['As of Date'].strftime('%B %d, %Y')}")

    st.stop()

# ---------------------------------------------------------------------------
# Top navigation
# ---------------------------------------------------------------------------

st.markdown(
    """
    <style>
    div[role="radiogroup"] { gap: 4px; border-bottom: 1px solid #e1e0d9; margin-bottom: 1rem; }
    div[role="radiogroup"] label {
        background: #f9f9f7; border-radius: 8px; padding: 6px 16px; margin-bottom: 6px;
    }
    div[role="radiogroup"] label:has(input:checked) { background: #e3ebfb; }
    div[role="radiogroup"] label:has(input:checked) p { color: #2a4fbf !important; font-weight: 700; }
    </style>
    """,
    unsafe_allow_html=True,
)
active_page = st.radio("Navigation", PAGES, horizontal=True, label_visibility="collapsed", key="page")

# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

if active_page == "Overview":
    st.markdown("#### Overview dashboard")
    filtered = filter_bar(df, "ov", "Search hospital...")

    buckets = [worst_status_bucket(a, i) for a, i in zip(filtered["Acute Occupancy %"], filtered["ICU Occupancy %"])]
    avg_occ = metric_snapshot(filtered, "Acute Occupancy %") if len(filtered) else np.nan
    near_capacity = buckets.count(1)
    critical = buckets.count(2)

    k1, k2, k3, k4 = st.columns(4)
    with k1:
        kpi_card("Hospitals monitored", f"{len(filtered):,}")
    with k2:
        kpi_card("Average occupancy", fmt_pct(avg_occ))
    with k3:
        kpi_card("Near capacity", f"{near_capacity}", bucket=1)
    with k4:
        kpi_card("Critical", f"{critical}", bucket=2)

    map_col, side_col = st.columns([2, 1])
    with map_col:
        header_col, toggle_col = st.columns([2, 2])
        with header_col:
            st.markdown("##### Interactive New York map")
        with toggle_col:
            map_view = st.segmented_control(
                "Map view", ["Markers", "3D", "Choropleth"], default="Markers", key="ov_map_view", label_visibility="collapsed"
            ) or "Markers"
        if map_view == "3D":
            render_3d_map(filtered)
        elif map_view == "Choropleth":
            render_choropleth_map(filtered)
        else:
            render_marker_map(filtered, key="overview_map")

    with side_col:
        st.markdown("##### Capacity status")
        for b in (2, 1, 0):
            count = buckets.count(b)
            range_text = {0: f"< {STATUS_THRESHOLDS[0]}%", 1: f"{STATUS_THRESHOLDS[0]}–{STATUS_THRESHOLDS[1]}%", 2: f"> {STATUS_THRESHOLDS[1]}%"}[b]
            st.markdown(
                f"""
                <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
                    <div style="width:14px;height:14px;border-radius:50%;background:{STATUS_MAP_COLORS[b]};"></div>
                    <div><b>{count} {STATUS_LABELS[b]}</b><br/><span style="color:#787670;font-size:12px;">{range_text}</span></div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        st.markdown("**Highest pressure**")
        top5 = filtered.sort_values("Acute Occupancy %", ascending=False).head(5)
        for i, (_, row) in enumerate(top5.iterrows(), start=1):
            st.write(f"{i}. {row['Facility Name']} — {fmt_pct(row['Acute Occupancy %'])}")

    st.caption("Pressure colors are illustrative analytical thresholds, not medical or regulatory standards. " + OVER_100_NOTE)

# ---------------------------------------------------------------------------
# Hospitals
# ---------------------------------------------------------------------------

elif active_page == "Hospitals":
    st.markdown("#### Hospitals")
    filtered = filter_bar(df, "hosp", "Search by hospital or borough...")
    render_hospital_table(filtered, key="hospitals_table")
    st.caption("Pressure colors are illustrative analytical thresholds, not medical or regulatory standards. " + OVER_100_NOTE)

# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------

elif active_page == "Trends":
    st.markdown("#### Capacity trends")
    c1, c2, c3 = st.columns(3)
    with c1:
        range_label = st.selectbox("Date range", ["Last 7 days", "Last 30 days", "Last 90 days", "All time"], index=1)
    with c2:
        region_label = st.selectbox("Region", ["All NY"] + sorted(df["Region"].unique()))
    with c3:
        metric = st.selectbox("Metric", list(METRICS.keys()))

    days = {"Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90, "All time": None}[range_label]
    range_start = history_df["As of Date"].min() if days is None else latest_date - pd.Timedelta(days=days)

    hist = history_df[history_df["As of Date"] >= range_start]
    if region_label != "All NY":
        hist = hist[hist["Region"] == region_label]
    snapshot = df if region_label == "All NY" else df[df["Region"] == region_label]

    chart_col, side_col = st.columns([2, 1])
    with chart_col:
        st.markdown("##### " + metric + " over time")
        series = metric_timeseries(hist, metric)
        fig = px.line(series, x="As of Date", y=metric, markers=len(series) <= 40)
        fig.data[0].line.color = ACCENT
        plain_layout(fig, height=360)
        st.plotly_chart(fig, width="stretch")

    with side_col:
        st.markdown("##### By region")
        by_region = metric_snapshot(df, metric, group_col="Region").sort_values(ascending=False)
        fmt = fmt_pct if METRICS[metric]["kind"] == "pct" else fmt_int
        for region, value in by_region.items():
            st.write(f"{region} — **{fmt(value)}**")

        st.markdown("##### Key change")
        start_val = series[metric].iloc[0] if len(series) else np.nan
        end_val = series[metric].iloc[-1] if len(series) else np.nan
        delta = end_val - start_val if pd.notna(start_val) and pd.notna(end_val) else np.nan
        unit = "pts" if METRICS[metric]["kind"] == "pct" else "beds"
        st.markdown(f"### {delta:+.1f} {unit}" if pd.notna(delta) else "### N/A")
        st.caption(f"{metric} vs. start of range ({range_label.lower()})")

    st.markdown("##### Hospital comparison")
    top_n = snapshot.sort_values("Acute Occupancy %", ascending=False).head(8).sort_values("Acute Occupancy %")
    fig_bar = px.bar(
        top_n, x="Acute Occupancy %", y="Facility Name", orientation="h",
        text=top_n["Acute Occupancy %"].round(1).astype(str) + "%",
    )
    fig_bar.update_traces(marker_color=ACCENT, textposition="outside", cliponaxis=False)
    plain_layout(fig_bar, height=max(260, 32 * len(top_n)))
    fig_bar.update_layout(yaxis=dict(title=None), xaxis=dict(title="Acute Occupancy %"))
    st.plotly_chart(fig_bar, width="stretch")
    st.caption("Top 8 hospitals by acute occupancy" + (f" in {region_label}" if region_label != "All NY" else " statewide") + " on the latest reporting date.")

# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------

elif active_page == "Compare":
    st.markdown("#### Compare hospitals")
    st.caption("Select 2 to 4 hospitals to compare occupancy, availability, and recent trends side by side.")
    selected = st.multiselect("Select hospitals", sorted(df["Facility Name"].unique()), max_selections=4)

    if len(selected) < 2:
        st.info("Select at least 2 hospitals to compare.")
    else:
        rows = df[df["Facility Name"].isin(selected)].copy()
        rows["Status"] = [status_label(worst_status_bucket(a, i)) for a, i in zip(rows["Acute Occupancy %"], rows["ICU Occupancy %"])]
        display = rows[
            ["Facility Name", "Region", "Acute Occupancy %", "ICU Occupancy %", "Total Staffed Acute Care Beds Available", "Total Staffed ICU Beds Currently Available", "Status"]
        ].rename(
            columns={
                "Facility Name": "Hospital",
                "Total Staffed Acute Care Beds Available": "Beds free",
                "Total Staffed ICU Beds Currently Available": "ICU free",
            }
        )
        st.dataframe(
            display.style.format({"Acute Occupancy %": "{:.1f}%", "ICU Occupancy %": "{:.1f}%"}).map(status_cell_style, subset=["Status"]),
            width="stretch",
            hide_index=True,
        )

        st.markdown("##### Acute occupancy trend")
        pfis = rows["Facility PFI"].tolist()
        hist = history_df[history_df["Facility PFI"].isin(pfis)]
        fig = px.line(hist, x="As of Date", y="Acute Occupancy %", color="Facility Name")
        plain_layout(fig, height=360)
        st.plotly_chart(fig, width="stretch")

# ---------------------------------------------------------------------------
# About Data
# ---------------------------------------------------------------------------

elif active_page == "About Data":
    st.markdown("#### About the data")
    left, right = st.columns([2, 1])

    with left:
        st.markdown("**Data source**")
        st.write(f"New York State Statewide Hospital Bed Capacity, published by the NY State Department of Health. [View the original dataset]({SOURCE_URL}).")
        st.divider()
        st.markdown("**Update frequency**")
        st.write(
            f"This app loads a downloaded snapshot of the dataset covering {history_df['As of Date'].nunique()} daily "
            f"reporting dates, from {history_df['As of Date'].min().strftime('%B %d, %Y')} to {latest_date.strftime('%B %d, %Y')}. "
            "The app does not fetch live data; refreshing it requires downloading a new export."
        )
        st.divider()
        st.markdown("**Definitions**")
        st.write(
            "- **Acute occupancy %** = occupied staffed acute-care beds ÷ total staffed acute-care beds.\n"
            "- **ICU occupancy %** = occupied staffed ICU beds ÷ total staffed ICU beds.\n"
            "- **Beds free / ICU free** = staffed beds currently available, as self-reported by each facility."
        )
        st.divider()
        st.markdown("**Methodology**")
        st.write(
            "Regional, network, and statewide occupancy figures are weighted (total occupied beds ÷ total staffed beds "
            "across the group), not an average of individual hospitals' percentages -- this avoids overweighting small "
            "facilities relative to large ones. A hospital's overall status badge reflects whichever of acute or ICU "
            "occupancy is worse, so a hospital fine on acute beds but critical on ICU still surfaces."
        )
        st.divider()
        st.markdown("**Limitations**")
        st.write(
            "The dataset does not include staffing levels, patient acuity, specialty availability, hospital finances, "
            "or transfer feasibility. Reported capacity may lag real-time operational conditions, and a handful of "
            "hospitals report occupancy above 100% (see the note on the Overview and Hospitals pages). This app "
            "identifies where capacity pressure exists; it does not diagnose the cause or prescribe a specific "
            "intervention."
        )

    with right:
        st.markdown("##### Capacity definitions")
        for b in (0, 1, 2):
            range_text = {0: f"< {STATUS_THRESHOLDS[0]}%", 1: f"{STATUS_THRESHOLDS[0]}–{STATUS_THRESHOLDS[1]}%", 2: f"> {STATUS_THRESHOLDS[1]}%"}[b]
            st.markdown(
                f"""
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                    {status_badge_html(b)}
                    <span style="color:#52514e;">{range_text}</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
        st.caption("Analytical/app thresholds only -- not medical or regulatory standards.")

        st.markdown("##### Data freshness")
        st.markdown(f"**{latest_date.strftime('%B %d, %Y')}**")
        st.caption("Reported capacity may change between updates and does not reflect real-time operational conditions.")
