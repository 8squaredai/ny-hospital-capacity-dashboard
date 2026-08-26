from __future__ import annotations

import html
import json
from typing import Literal, Optional

import numpy as np
import pandas as pd
import plotly.express as px
import pydeck as pdk
import streamlit as st
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel

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

# Verified directly against the source data (2026-08-24): 833 of 73,524 acute rows (~1.1%)
# and 1,080 (~1.5%) of ICU rows exceed 100% occupancy. In effectively all of them (all but 4
# rows total) Beds Free = Staffed - Occupied holds exactly, so a negative "beds free" number
# is the algebraic mirror of the same >100% occupancy, not a separate bug. Causes vary by
# facility: patient boarding beyond the staffed bed count, short-notice staffing reductions
# that shrink the staffed baseline, or a "staffed beds" figure that's stale or narrower in
# definition than "occupied". It isn't always a one-off blip either -- a few small facilities
# (e.g. Lockport Memorial Hospital) report this on nearly every single day in the dataset,
# suggesting a persistent reporting-definition mismatch for that facility rather than a
# transient surge.
OVER_100_NOTE = (
    "Occupancy above 100% (and a negative \"beds free\" number, which is the same thing "
    "expressed as a bed count) means more patients are occupying beds than the hospital "
    "has marked as staffed. This reflects patient boarding, short-notice staffing "
    "reductions, or a stale/narrower staffed-bed count -- not a calculation error. For a "
    "few small facilities this is a near-daily, persistent pattern rather than a one-off "
    "spike."
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


def occupancy_column_config() -> dict:
    """Hover-tooltip column config for any table showing occupancy % or beds-free counts --
    covers every column name used across the different tables; Streamlit ignores entries
    for columns that aren't actually present in a given table, so one shared dict is safe
    to reuse everywhere instead of repeating it per call site."""
    tip = st.column_config.NumberColumn(help=OVER_100_NOTE)
    return {
        "Occupancy": tip,
        "Acute Occupancy %": tip,
        "ICU Occupancy %": tip,
        "Beds free": tip,
        "ICU free": tip,
    }


def status_cell_style(label: str) -> str:
    if label not in STATUS_LABELS:
        return ""
    bg, fg = STATUS_COLORS[STATUS_LABELS.index(label)]
    return f"background-color: {bg}; color: {fg}; border-radius: 999px; font-weight: 600; text-align: center;"


def html_progress_bar(fraction: float, bucket: int | None) -> None:
    """A colored progress bar matching the app's status palette -- st.progress()
    has no color parameter, so this renders the same visual language (green/
    amber/red) instead of a flat default-blue bar regardless of intensity."""
    pct = min(max(fraction, 0), 1) * 100
    color = STATUS_MAP_COLORS.get(bucket, "#898781")
    st.markdown(
        f"""
        <div style="background:#e7e6e2;border-radius:999px;height:10px;margin:4px 0 14px 0;overflow:hidden;">
            <div style="background:{color};width:{pct:.1f}%;height:100%;border-radius:999px;"></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


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


def info_icon_html(tooltip: str, color: str = "#787670") -> str:
    """A small (i) with a native browser tooltip on hover -- no JS/CSS library needed,
    works the same in any HTML context (custom cards, table captions, etc.)."""
    safe_tooltip = html.escape(tooltip, quote=True)
    return (
        f'<span title="{safe_tooltip}" style="display:inline-block;width:14px;height:14px;'
        f'border-radius:50%;border:1.5px solid {color};color:{color};font-size:10px;'
        f'line-height:13px;text-align:center;cursor:help;margin-left:5px;'
        f'font-weight:700;font-family:Georgia,serif;">i</span>'
    )


def kpi_card(label: str, value: str, bucket: int | None = None, tooltip: str | None = None) -> None:
    if bucket is None:
        bg, fg = "#fcfcfb", "#0b0b0b"
        border = "1px solid #e1e0d9"
    else:
        bg, fg = STATUS_COLORS[bucket]
        border = "none"
    icon = info_icon_html(tooltip, color=fg) if tooltip else ""
    st.markdown(
        f"""
        <div style="background:{bg};border:{border};border-radius:10px;
                     padding:16px 18px;margin-bottom:10px;">
            <div style="font-size:28px;color:{fg};font-weight:700;">{value}</div>
            <div style="font-size:13px;color:{fg};opacity:0.85;margin-top:2px;">{label}{icon}</div>
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
    c1, c2, c3, c4, c5 = st.columns([2, 1, 1, 1, 1])
    with c1:
        search = st.text_input(search_placeholder, key=f"{key_prefix}_search", label_visibility="collapsed", placeholder=search_placeholder)
    with c2:
        region = st.selectbox("Region", ["All regions"] + sorted(rows["Region"].unique()), key=f"{key_prefix}_region")
    with c3:
        county_raw_options = sorted(rows["Facility County"].dropna().unique())
        county_label_to_raw = {c.title(): c for c in county_raw_options}
        county_display = st.selectbox("County", ["All counties"] + sorted(county_label_to_raw.keys()), key=f"{key_prefix}_county")
        county = county_label_to_raw.get(county_display)
    with c4:
        htype = st.selectbox("Hospital type", ["All types"] + sorted(rows["Hospital Type"].unique()), key=f"{key_prefix}_type")
    with c5:
        status = st.selectbox("Capacity status", ["All statuses"] + STATUS_LABELS, key=f"{key_prefix}_status")

    filtered = rows.copy()
    if search:
        filtered = filtered[searchable_text(filtered).str.contains(search.lower())]
    if region != "All regions":
        filtered = filtered[filtered["Region"] == region]
    if county:
        filtered = filtered[filtered["Facility County"] == county]
    if htype != "All types":
        filtered = filtered[filtered["Hospital Type"] == htype]
    if status != "All statuses":
        bucket = STATUS_LABELS.index(status)
        filtered = filtered[
            [worst_status_bucket(a, i) == bucket for a, i in zip(filtered["Acute Occupancy %"], filtered["ICU Occupancy %"])]
        ]
    return filtered


class DashboardQuery(BaseModel):
    """Structured shape an LLM call must fill in from a free-text search request.
    The LLM only ever returns this -- never hospital names, numbers, or prose --
    so parsing user intent can't accidentally leak into what looks like analysis."""

    region: Optional[str] = None
    county: Optional[str] = None
    hospital_type: Optional[str] = None
    status: Optional[Literal["Available", "High occupancy", "Critical"]] = None
    sort_metric: Literal["Acute Occupancy %", "ICU Occupancy %", "Available Acute Beds", "Available ICU Beds"] = "Acute Occupancy %"
    sort_order: Literal["asc", "desc"] = "desc"


SORT_COLUMN_MAP = {
    "Acute Occupancy %": "Acute Occupancy %",
    "ICU Occupancy %": "ICU Occupancy %",
    "Available Acute Beds": "Total Staffed Acute Care Beds Available",
    "Available ICU Beds": "Total Staffed ICU Beds Currently Available",
}


def best_match(value: str | None, choices: list[str]) -> str | None:
    """Loose match for an LLM-returned region/type string against real column
    values (exact, then substring, then NYC borough alias) -- an LLM could return
    "Brooklyn" when the real region label is "New York City", and a close-but-not-
    exact match should still apply rather than silently dropping the whole filter."""
    if not value:
        return None
    value_lower = value.lower()
    for choice in choices:
        if choice.lower() == value_lower:
            return choice
    for choice in choices:
        if value_lower in choice.lower() or choice.lower() in value_lower:
            return choice
    # NYC borough alias -- resolves into whichever target space `choices`
    # represents. Previously this only ever returned "New York City" (a Region
    # value), so a borough name matched fine against Region choices but always
    # failed against County choices (e.g. "Brooklyn" never resolved to "KINGS"),
    # silently dropping the filter whenever an LLM classified it as a county.
    borough_to_county = {b.lower(): county for county, b in NYC_BOROUGH_MAP.items()}
    county_code = borough_to_county.get(value_lower)
    if county_code:
        if county_code in choices:
            return county_code
        if "New York City" in choices:
            return "New York City"
    return None


def get_gemini_api_key() -> str | None:
    # st.secrets raises rather than returning None when no secrets.toml exists at
    # all (verified directly -- not the behavior you'd assume from a Mapping), so
    # the search feature must stay usable with no key configured at all.
    try:
        return st.secrets.get("GOOGLE_API_KEY")
    except Exception:
        return None


def parse_dashboard_query(text: str) -> DashboardQuery | None:
    """Turns free text into a DashboardQuery via Gemini structured output. Returns
    None on any failure (missing key, network error, bad response) so the caller
    can fall back to the plain dropdown filters -- the app must stay fully usable
    even when the LLM is unavailable."""
    api_key = get_gemini_api_key()
    if not api_key:
        return None
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=(
                "Parse this hospital-dashboard search request into structured filters. "
                "Only set fields the request actually implies; leave the rest at their defaults.\n\n"
                f"Request: {text}"
            ),
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=DashboardQuery,
            ),
        )
        return response.parsed
    except Exception:
        return None


def open_detail(pfi, return_page: str) -> None:
    st.session_state.detail_pfi = pfi
    st.session_state.return_page = return_page
    st.rerun()


NY_CENTER = {"lat": 42.9, "lon": -75.5}


def render_marker_map(rows: pd.DataFrame, key: str, zoom: float = 5.7, center: dict | None = None) -> None:
    # Explicit default rather than relying on Plotly's auto-center-from-data
    # behavior -- that behavior varies across Plotly versions (requirements.txt
    # only pins plotly>=5.24, a loose floor), and on at least one resolved
    # version it defaulted to (0, 0) -- the middle of the Gulf of Guinea --
    # instead of fitting to the NY hospital points, even though the same code
    # centered correctly with the locally pinned version.
    center = center or NY_CENTER
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


def render_hospital_table(
    rows: pd.DataFrame,
    key: str,
    columns: list[str] | None = None,
    sort_column: str = "Acute Occupancy %",
    ascending: bool = False,
) -> None:
    cols = columns or ["Facility Name", "Region", "County", "Total Staffed Acute Care Beds Available", "Total Staffed ICU Beds Currently Available", "Acute Occupancy %", "Status"]
    table = rows.copy()
    table["County"] = table["Facility County"].str.title()
    table["Status"] = [status_label(worst_status_bucket(a, i)) for a, i in zip(table["Acute Occupancy %"], table["ICU Occupancy %"])]
    table = table.sort_values(sort_column, ascending=ascending).reset_index(drop=True)
    display = table[cols].rename(
        columns={
            "Facility Name": "Hospital",
            "Total Staffed Acute Care Beds Available": "Beds free",
            "Total Staffed ICU Beds Currently Available": "ICU free",
            "Acute Occupancy %": "Occupancy",
        }
    )
    sort_label = "occupancy" if "Occupancy" in sort_column else sort_column.lower()
    st.caption(f"{len(display)} hospitals — sorted by {sort_label} ({'lowest first' if ascending else 'highest first'}). Click a column header to re-sort.")
    event = st.dataframe(
        display.style.format({"Occupancy": "{:.1f}%"}).map(status_cell_style, subset=["Status"]),
        column_config=occupancy_column_config(),
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
    k1.metric("Occupancy", fmt_pct(hosp["Acute Occupancy %"]), help=OVER_100_NOTE)
    k2.metric("Beds free", fmt_int(hosp["Total Staffed Acute Care Beds Available"]), help=OVER_100_NOTE)
    k3.metric("ICU beds free", fmt_int(hosp["Total Staffed ICU Beds Currently Available"]), help=OVER_100_NOTE)
    k4.metric("24h change", f"{change:+.1f} pts" if pd.notna(change) else "N/A")
    if hosp["Acute Occupancy %"] > 100 or hosp["ICU Occupancy %"] > 100:
        st.caption("This hospital is reporting more occupied beds than staffed beds as of this date -- hover Occupancy or Beds free above for what that means.")

    st.markdown("#### Acute occupancy in context")
    region_occ = metric_snapshot(df[df["Region"] == hosp["Region"]], "Acute Occupancy %")
    state_occ = metric_snapshot(df, "Acute Occupancy %")
    ctx1, ctx2, ctx3 = st.columns(3)
    ctx1.metric("This hospital", fmt_pct(hosp["Acute Occupancy %"]))
    ctx2.metric(f"{hosp['Region']} region", fmt_pct(region_occ))
    ctx3.metric("Statewide", fmt_pct(state_occ))
    st.caption("Region and statewide figures are weighted (total occupied ÷ total staffed beds across the group), not averaged.")

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
        acute_bucket = status_bucket(hosp["Acute Occupancy %"])
        icu_bucket = status_bucket(hosp["ICU Occupancy %"])
        st.write(f"General beds — {fmt_int(hosp['Total Staffed Acute Care Beds Available'])} / {fmt_int(hosp['Total Staffed Acute Care Beds'])} free")
        html_progress_bar(acute_frac, acute_bucket)
        st.write(f"ICU beds — {fmt_int(hosp['Total Staffed ICU Beds Currently Available'])} / {fmt_int(hosp['Total Staffed ICU Beds'])} free")
        html_progress_bar(icu_frac, icu_bucket)
        emergency_label = {0: "Low pressure", 1: "Moderate pressure", 2: "High pressure"}.get(bucket, "N/A")
        st.write(f"Emergency pressure (proxy) — {emergency_label}")
        html_progress_bar(acute_frac, bucket)
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
        kpi_card("Average occupancy", fmt_pct(avg_occ), tooltip=OVER_100_NOTE)
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
        st.markdown(f"**Highest pressure** {info_icon_html(OVER_100_NOTE)}", unsafe_allow_html=True)
        top5 = filtered.sort_values("Acute Occupancy %", ascending=False).head(5)
        for i, (_, row) in enumerate(top5.iterrows(), start=1):
            st.write(f"{i}. {row['Facility Name']} — {fmt_pct(row['Acute Occupancy %'])}")

    st.caption("Pressure colors are illustrative analytical thresholds, not medical or regulatory standards.")

# ---------------------------------------------------------------------------
# Hospitals
# ---------------------------------------------------------------------------

elif active_page == "Hospitals":
    st.markdown("#### Hospitals")

    with st.form("ai_search_form"):
        ai_col, button_col = st.columns([5, 1])
        with ai_col:
            ai_query = st.text_input(
                "Ask in plain English (optional)",
                placeholder='e.g. "critical hospitals in Brooklyn with the fewest ICU beds free"',
                key="hosp_ai_query",
            )
        with button_col:
            st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
            ai_submitted = st.form_submit_button("Search")

    if ai_submitted and ai_query:
        parsed = parse_dashboard_query(ai_query)
        if parsed is None:
            st.warning("AI search is unavailable right now (no API key configured, or the request failed) -- use the filters below instead.")
        else:
            region_match = best_match(parsed.region, sorted(df["Region"].unique()))
            county_match = best_match(parsed.county, sorted(df["Facility County"].dropna().unique()))
            type_match = best_match(parsed.hospital_type, sorted(df["Hospital Type"].unique()))
            st.session_state["hosp_region"] = region_match or "All regions"
            # The County selectbox's options are title-cased for display ("Kings"), but
            # Facility County itself is stored uppercase ("KINGS") -- best_match above
            # runs against the raw values, so convert back to the display form the
            # widget actually holds in session_state before assigning it.
            st.session_state["hosp_county"] = county_match.title() if county_match else "All counties"
            st.session_state["hosp_type"] = type_match or "All types"
            st.session_state["hosp_status"] = parsed.status or "All statuses"
            st.session_state["hosp_search"] = ""
            st.session_state["hosp_sort_metric"] = parsed.sort_metric
            st.session_state["hosp_sort_order"] = parsed.sort_order
            st.caption(
                f"Showing: {parsed.status or 'all'} hospitals"
                + (f" in {region_match}" if region_match else "")
                + (f", {county_match.title()} County" if county_match else "")
                + (f", type {type_match}" if type_match else "")
                + f", sorted by {parsed.sort_metric} ({parsed.sort_order}ending)."
            )

    sort_metric = st.session_state.get("hosp_sort_metric", "Acute Occupancy %")
    sort_order = st.session_state.get("hosp_sort_order", "desc")

    filtered = filter_bar(df, "hosp", "Search by hospital or borough...")
    render_hospital_table(
        filtered,
        key="hospitals_table",
        sort_column=SORT_COLUMN_MAP[sort_metric],
        ascending=(sort_order == "asc"),
    )
    st.caption("Pressure colors are illustrative analytical thresholds, not medical or regulatory standards. Hover an Occupancy/Beds free column header for what a value over 100% or below 0 means.")

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
        group_choice = st.segmented_control("Group by", ["Region", "County"], default="Region", key="trends_group_by", label_visibility="collapsed") or "Region"
        fmt = fmt_pct if METRICS[metric]["kind"] == "pct" else fmt_int
        if group_choice == "Region":
            st.markdown("##### By region")
            by_group = metric_snapshot(df, metric, group_col="Region").sort_values(ascending=False)
            for group, value in by_group.items():
                st.write(f"{group} — **{fmt(value)}**")
        else:
            st.markdown("##### By county (top 10)")
            county_snapshot = df.copy()
            county_snapshot["County"] = county_snapshot["Facility County"].str.title()
            by_group = metric_snapshot(county_snapshot, metric, group_col="County").sort_values(ascending=False).head(10)
            for group, value in by_group.items():
                st.write(f"{group} — **{fmt(value)}**")
            st.caption("35% of NY counties have only one reporting hospital, so a county's figure there is really just that hospital's own number.")

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
            column_config=occupancy_column_config(),
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
            f"This app loads a snapshot of the dataset covering {history_df['As of Date'].nunique()} daily "
            f"reporting dates, from {history_df['As of Date'].min().strftime('%B %d, %Y')} to {latest_date.strftime('%B %d, %Y')}. "
            "It does not query NY DOH live on each page load. Instead, a scheduled GitHub Actions "
            "workflow runs daily, downloads the full current export, and commits it back to the "
            "repository only if the data actually changed. Streamlit Community Cloud detects that "
            "commit and automatically reboots the app, so the snapshot above stays current without "
            "anyone manually re-downloading anything."
        )
        st.divider()
        st.markdown("**Definitions**")
        st.write(
            "- **Acute occupancy %** = occupied staffed acute-care beds ÷ total staffed acute-care beds.\n"
            "- **ICU occupancy %** = occupied staffed ICU beds ÷ total staffed ICU beds.\n"
            "- **Beds free / ICU free** = staffed beds currently available, as self-reported by each facility."
        )
        st.divider()
        st.markdown("**Occupancy over 100% and negative \"beds free\"**")
        st.write(OVER_100_NOTE)
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
            "or transfer feasibility. Reported capacity may lag real-time operational conditions (see above for why "
            "occupancy can read above 100%). This app identifies where capacity pressure exists; it does not "
            "diagnose the cause or prescribe a specific intervention."
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
