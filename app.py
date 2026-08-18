from __future__ import annotations

import json

import numpy as np
import pandas as pd
import plotly.express as px
import pydeck as pdk
import streamlit as st

st.set_page_config(page_title="NY Hospital Capacity Dashboard", layout="wide")

DATA_PATH = "data/New_York_State_Statewide_Hospital_Bed_Capacity_20260817.csv"
GEOJSON_PATH = "data/ny_counties.geojson"
FACILITIES_PATH = "data/ny_health_facilities.csv"

NUMERIC_COLS = [
    "Total Staffed Acute Care Beds",
    "Total Staffed Acute Care Beds Occupied",
    "Total Staffed Acute Care Beds Available",
    "Total Staffed ICU Beds",
    "Total Staffed ICU Beds Currently Occupied",
    "Total Staffed ICU Beds Currently Available",
]


@st.cache_data
def load_facility_coords(path: str) -> pd.DataFrame:
    """NY DOH's facility directory covers every licensed facility type (hospitals,
    nursing homes, clinics, ...), keyed by Facility ID -- the same identifier as
    our bed-capacity data's Facility PFI. A handful of IDs appear as exact
    duplicate rows (same coordinates repeated), so dedupe before joining.
    """
    fac = pd.read_csv(path, low_memory=False)
    fac = fac.drop_duplicates(subset="Facility ID", keep="first")
    fac = fac[["Facility ID", "Facility Latitude", "Facility Longitude", "Facility Zip Code"]]
    return fac.rename(
        columns={
            "Facility ID": "Facility PFI",
            "Facility Latitude": "Latitude",
            "Facility Longitude": "Longitude",
            "Facility Zip Code": "Zip Code",
        }
    )


@st.cache_data
def load_data(path: str, facilities_path: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    df = pd.read_csv(path, low_memory=False)

    for col in NUMERIC_COLS:
        df[col] = pd.to_numeric(
            df[col].astype(str).str.replace(",", "", regex=False), errors="coerce"
        )

    df["As of Date"] = pd.to_datetime(df["As of Date"], format="%m/%d/%Y")
    df = df.drop_duplicates(subset=["Facility PFI", "As of Date"], keep="last")

    staffed_acute = df["Total Staffed Acute Care Beds"].replace(0, np.nan)
    staffed_icu = df["Total Staffed ICU Beds"].replace(0, np.nan)
    df["Acute Occupancy %"] = (
        df["Total Staffed Acute Care Beds Occupied"] / staffed_acute * 100
    )
    df["ICU Occupancy %"] = (
        df["Total Staffed ICU Beds Currently Occupied"] / staffed_icu * 100
    )

    # Attach Latitude/Longitude/Zip Code here, before slicing to the latest
    # date, so the full history and the latest-date snapshot both carry it.
    # A left join means a hospital with no match (or a match with no
    # coordinates on file) simply comes through as NaN -- nothing to handle
    # explicitly, the map/analysis code downstream just filters on that.
    facility_coords = load_facility_coords(facilities_path)
    df = df.merge(facility_coords, on="Facility PFI", how="left")

    # The downloaded CSV is a full historical export (one row per facility per
    # reporting date). Keep the full history for trend analysis, plus a
    # latest-date snapshot for the current-state tabs.
    latest_date = df["As of Date"].max()
    latest_df = df[df["As of Date"] == latest_date].copy()

    return df, latest_df, latest_date


# Analytical/app thresholds only -- not medical or regulatory standards.
PRESSURE_THRESHOLDS = (75, 85, 95)
PRESSURE_COLORS = [
    ("#0ca30c", "white"),  # Low
    ("#fab219", "black"),  # Moderate
    ("#ec835a", "black"),  # High
    ("#d03b3b", "white"),  # Critical
]

# A few hospitals report occupancy over 100% -- verified against the source
# data, not a calculation bug (Occupied + Available == Staffed holds exactly).
# Causes vary by facility: patient boarding beyond the staffed bed count,
# short-notice staffing reductions, or a "staffed beds" figure that's stale
# or narrower in definition than the "occupied" count it's divided into.
OVER_100_NOTE = (
    "Occupancy can exceed 100% for some hospitals -- this reflects patient "
    "boarding, short-notice staffing reductions, or inconsistencies in "
    "self-reported facility data, not a calculation error."
)

ACUTE_ACCENT = "#2a78d6"  # categorical slot 1 (blue) -- identifies the Acute group
ICU_ACCENT = "#eb6834"  # categorical slot 2 (orange) -- identifies the ICU group

NYC_BOROUGH_MAP = {
    "NEW YORK": "Manhattan",
    "KINGS": "Brooklyn",
    "BRONX": "Bronx",
    "QUEENS": "Queens",
    "RICHMOND": "Staten Island",
}


def pressure_bucket(pct: float) -> int | None:
    if pd.isna(pct):
        return None
    low, mid, high = PRESSURE_THRESHOLDS
    if pct >= high:
        return 3
    if pct >= mid:
        return 2
    if pct >= low:
        return 1
    return 0


def pressure_style(pct: float) -> str:
    bucket = pressure_bucket(pct)
    if bucket is None:
        return ""
    bg, fg = PRESSURE_COLORS[bucket]
    return f"background-color: {bg}; color: {fg}"


PRIORITY_LABELS = ["Low", "Moderate", "High", "Critical"]


def priority_bucket(acute_pct: float, icu_pct: float) -> int | None:
    """Worst-case tier across acute and ICU pressure -- a hospital high on either counts."""
    buckets = [b for b in (pressure_bucket(acute_pct), pressure_bucket(icu_pct)) if b is not None]
    return max(buckets) if buckets else None


def priority_label(bucket: int | None) -> str:
    # Bucket arrives as a pandas Series value: None mixed with int upcasts the
    # whole column to float64, so a valid bucket shows up as e.g. 3.0, not 3.
    if pd.isna(bucket):
        return "N/A"
    return PRIORITY_LABELS[int(bucket)]


def priority_row_style(row: pd.Series) -> list[str]:
    bucket = priority_bucket(row["Acute Occupancy %"], row["ICU Occupancy %"])
    if bucket is None:
        style = ""
    else:
        bg, fg = PRESSURE_COLORS[bucket]
        style = f"background-color: {bg}; color: {fg}"
    return [style if col == "Priority" else "" for col in row.index]


@st.cache_data
def load_geojson(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


GEOJSON = load_geojson(GEOJSON_PATH)
FIPS_LOOKUP = {f["properties"]["NAME"].upper(): f["id"] for f in GEOJSON["features"]}
SEQUENTIAL_BLUE = ["#cde2fb", "#6da7ec", "#256abf", "#0d366b"]  # one hue, light -> dark


def county_choropleth(plot_df: pd.DataFrame, fips_name_col: str, hover_name_col: str | None = None):
    plot_df = plot_df.copy()
    plot_df["fips"] = plot_df[fips_name_col].str.upper().map(FIPS_LOOKUP)
    plot_df = plot_df.dropna(subset=["fips"])
    fig = px.choropleth(
        plot_df,
        geojson=GEOJSON,
        locations="fips",
        featureidkey="id",
        color="Acute Occupancy %",
        color_continuous_scale=SEQUENTIAL_BLUE,
        hover_name=hover_name_col or fips_name_col,
        hover_data={"fips": False, "Acute Occupancy %": ":.1f", "ICU Occupancy %": ":.1f"},
    )
    fig.update_geos(fitbounds="locations", visible=False)
    fig.update_layout(
        margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor="#fcfcfb",
        coloraxis_colorbar=dict(title="Acute occupancy (%)"),
    )
    return fig


def render_pressure_legend() -> None:
    items = "".join(
        f'<span style="display:inline-flex;align-items:center;margin-right:16px;">'
        f'<span style="width:12px;height:12px;background:{color};display:inline-block;'
        f'margin-right:5px;border:1px solid #777;"></span>{label}</span>'
        for (color, _), label in zip(PRESSURE_COLORS, PRIORITY_LABELS)
    )
    st.markdown(
        f'<div style="font-size:13px;color:#52514e;margin:4px 0 10px;">'
        f'<strong>Acute occupancy pressure</strong>: {items}</div>',
        unsafe_allow_html=True,
    )


def hex_to_rgba(hex_color: str, alpha: int = 200) -> list[int]:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    return [r, g, b, alpha]


def weekly_slider(history: pd.DataFrame, latest: pd.Timestamp, key: str) -> pd.Timestamp:
    """Weekly steps anchored on the latest date, so "today" is always a slider
    option and we don't force scrubbing through all 340 daily positions."""
    earliest = history["As of Date"].min()
    options = []
    d = latest
    while d >= earliest:
        options.append(d)
        d -= pd.Timedelta(days=7)
    options = sorted(options)
    return st.select_slider(
        "Select week",
        options=options,
        value=latest,
        format_func=lambda x: x.strftime("%b %d, %Y"),
        key=key,
    )


def render_hospital_3d_map(
    day_subset: pd.DataFrame,
    view_state: pdk.ViewState,
    subtitle_col: str,
    region_label: str,
    radius: int,
    elevation_scale: int,
) -> None:
    """One 3D bar per hospital at its real coordinates; height and color both
    encode acute occupancy pressure. Hospitals with no coordinates on file are
    dropped from the plot but counted in the caption, never silently merged in."""
    map_df = day_subset.dropna(subset=["Latitude", "Longitude"]).copy()
    excluded_count = len(day_subset) - len(map_df)

    if map_df.empty:
        st.info("No hospital coordinates available for this date.")
        return

    map_df["bucket"] = map_df["Acute Occupancy %"].apply(pressure_bucket)
    map_df["fill_color"] = map_df["bucket"].apply(
        lambda b: hex_to_rgba(PRESSURE_COLORS[int(b)][0]) if pd.notna(b) else [137, 135, 129, 160]
    )
    # Bar height is capped at 100% so the handful of hospitals reporting over
    # 100% (see OVER_100_NOTE) don't visually dwarf every other bar -- color
    # and the tooltip label still reflect the true, uncapped percentage.
    map_df["elevation"] = map_df["Acute Occupancy %"].fillna(0).clip(upper=100)
    map_df["occupancy_label"] = map_df["Acute Occupancy %"].apply(fmt_pct)
    map_df["subtitle"] = map_df[subtitle_col]

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
            initial_view_state=view_state,
            map_style=None,
            tooltip={"text": "{Facility Name} ({subtitle})\nAcute occupancy: {occupancy_label}"},
        )
    )
    st.caption(
        f"{len(map_df)} of {len(day_subset)} reporting {region_label} hospitals mapped"
        + (f" ({excluded_count} excluded, no coordinates on file)." if excluded_count else ".")
        + " Bar height and color both encode acute occupancy pressure -- taller and redder "
        "means more pressure (bar height is capped at 100% so outliers stay readable; "
        "color and the hover label still show the true value). Pressure colors are "
        "illustrative analytical thresholds (75% / 85% / 95%), not medical or "
        "regulatory standards. " + OVER_100_NOTE
    )


def kpi_card(label: str, value: str, accent: str) -> None:
    """Neutral KPI tile with a colored top border identifying its group (Acute/ICU)."""
    st.markdown(
        f"""
        <div style="background:#fcfcfb;border:1px solid #e1e0d9;
                     border-top:4px solid {accent};border-radius:10px;
                     padding:14px 16px;margin-bottom:10px;">
            <div style="font-size:13px;color:#52514e;font-weight:600;
                         text-transform:uppercase;letter-spacing:0.02em;">{label}</div>
            <div style="font-size:28px;color:#0b0b0b;font-weight:700;margin-top:4px;">
                {value}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def kpi_badge(label: str, value: str, pct: float) -> None:
    """Occupancy KPI tile, filled with its pressure status color."""
    bucket = pressure_bucket(pct)
    bg, fg = PRESSURE_COLORS[bucket] if bucket is not None else ("#898781", "white")
    st.markdown(
        f"""
        <div style="background:{bg};color:{fg};border-radius:10px;
                     padding:14px 16px;margin-bottom:10px;">
            <div style="font-size:13px;font-weight:600;text-transform:uppercase;
                         letter-spacing:0.02em;opacity:0.85;">{label}</div>
            <div style="font-size:28px;font-weight:700;margin-top:4px;">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def fmt_pct(value: float) -> str:
    return f"{value:.1f}%" if pd.notna(value) else "N/A"


def capital_priority_table(
    history_df: pd.DataFrame,
    latest_agg: pd.DataFrame,
    group_col: str,
    latest_date: pd.Timestamp,
    window_days: int = 30,
) -> pd.DataFrame:
    """Blends current snapshot occupancy with a 30-day sustained average and a
    trend (30-day avg vs. the 30 days before that) into a single ranking, so a
    group that's merely having one bad day doesn't outrank one under real,
    sustained pressure. Trend is only added (not subtracted) when pressure is
    building -- an easing trend doesn't get credit for "improving" here,
    since current + sustained load are what matters for a capital decision.
    """
    cutoff_recent = latest_date - pd.Timedelta(days=window_days)
    cutoff_prior = latest_date - pd.Timedelta(days=2 * window_days)

    recent_window = history_df[history_df["As of Date"] > cutoff_recent]
    prior_window = history_df[
        (history_df["As of Date"] > cutoff_prior) & (history_df["As of Date"] <= cutoff_recent)
    ]
    recent_agg = aggregate_occupancy(recent_window, group_col)
    prior_agg = aggregate_occupancy(prior_window, group_col)

    table = latest_agg[
        ["facility_count", "staffed_acute", "available_acute", "Acute Occupancy %", "ICU Occupancy %"]
    ].copy()
    table["Acute 30-Day Avg %"] = recent_agg["Acute Occupancy %"]
    table["Trend (pts)"] = recent_agg["Acute Occupancy %"] - prior_agg["Acute Occupancy %"]
    table["Capital Priority Score"] = (
        0.5 * table["Acute Occupancy %"]
        + 0.5 * table["Acute 30-Day Avg %"]
        + table["Trend (pts)"].clip(lower=0)
    )
    return table.sort_values("Capital Priority Score", ascending=False)


def capital_insight_text(name: str, row: pd.Series) -> str:
    trend = row["Trend (pts)"]
    if trend > 2:
        trend_phrase = "pressure has been building over the past month"
    elif trend < -2:
        trend_phrase = "though pressure has eased somewhat over the past month"
    else:
        trend_phrase = "occupancy has held roughly steady over the past month"
    return (
        f"**{name}** — acute occupancy is **{row['Acute Occupancy %']:.1f}%** today, "
        f"averaging **{row['Acute 30-Day Avg %']:.1f}%** over the past 30 days "
        f"({trend:+.1f} pts vs. the 30 days before that), and {trend_phrase}. "
        f"**{row['facility_count']:.0f} hospitals** report only "
        f"**{row['available_acute']:.0f} staffed acute beds available** combined."
    )


def aggregate_occupancy(df: pd.DataFrame, group_col: str) -> pd.DataFrame:
    """Weighted (sum-of-beds) occupancy per group, not an average of percentages."""
    agg = df.groupby(group_col).agg(
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


history_df, df, latest_date = load_data(DATA_PATH, FACILITIES_PATH)
region_agg = aggregate_occupancy(df, "DOH Region")
network_agg = aggregate_occupancy(df, "Facility Network")
county_agg = aggregate_occupancy(df, "Facility County")

statewide_daily = (
    history_df.groupby("As of Date")
    .agg(
        staffed_acute=("Total Staffed Acute Care Beds", "sum"),
        occupied_acute=("Total Staffed Acute Care Beds Occupied", "sum"),
        staffed_icu=("Total Staffed ICU Beds", "sum"),
        occupied_icu=("Total Staffed ICU Beds Currently Occupied", "sum"),
    )
    .reset_index()
)
statewide_daily["Acute Occupancy %"] = (
    statewide_daily["occupied_acute"] / statewide_daily["staffed_acute"].replace(0, np.nan) * 100
)
statewide_daily["ICU Occupancy %"] = (
    statewide_daily["occupied_icu"] / statewide_daily["staffed_icu"].replace(0, np.nan) * 100
)
statewide_daily["Acute 7-Day Avg"] = statewide_daily["Acute Occupancy %"].rolling(7, min_periods=1).mean()
statewide_daily["ICU 7-Day Avg"] = statewide_daily["ICU Occupancy %"].rolling(7, min_periods=1).mean()

week_ago_row = statewide_daily[statewide_daily["As of Date"] == latest_date - pd.Timedelta(days=7)]
acute_week_ago = week_ago_row["Acute Occupancy %"].iloc[0] if not week_ago_row.empty else np.nan
icu_week_ago = week_ago_row["ICU Occupancy %"].iloc[0] if not week_ago_row.empty else np.nan

nyc_df = df[df["Facility County"].isin(NYC_BOROUGH_MAP)].copy()
nyc_df["Borough"] = nyc_df["Facility County"].map(NYC_BOROUGH_MAP)
borough_agg = aggregate_occupancy(nyc_df, "Borough")
nyc_county_agg = aggregate_occupancy(nyc_df, "Facility County").reset_index()
nyc_county_agg["Borough"] = nyc_county_agg["Facility County"].map(NYC_BOROUGH_MAP)

total_acute_staffed = df["Total Staffed Acute Care Beds"].sum()
total_acute_occupied = df["Total Staffed Acute Care Beds Occupied"].sum()
total_acute_available = df["Total Staffed Acute Care Beds Available"].sum()
state_acute_occupancy = total_acute_occupied / total_acute_staffed * 100

total_icu_staffed = df["Total Staffed ICU Beds"].sum()
total_icu_occupied = df["Total Staffed ICU Beds Currently Occupied"].sum()
total_icu_available = df["Total Staffed ICU Beds Currently Available"].sum()
state_icu_occupancy = total_icu_occupied / total_icu_staffed * 100

st.title("New York Hospital Capacity Dashboard")
st.caption(f"Data as of {latest_date.strftime('%B %d, %Y')} — {len(df)} facilities reporting")

TAB_NAMES = [
    "State Overview",
    "Hospital Explorer",
    "Regional Analysis",
    "Network Analysis",
    "Priority Dashboard",
    "NYC Boroughs",
    "Historical Trends",
    "Capital Investment Insights",
]
# st.tabs() has no memory of which tab was active, so any widget interaction
# (e.g. the hospital selectbox) reruns the script and resets it to index 0.
# A session_state-backed radio persists the selection across reruns instead.
if "active_tab" not in st.session_state:
    st.session_state.active_tab = TAB_NAMES[0]

st.markdown(
    """
    <style>
    div[role="radiogroup"] { gap: 4px; border-bottom: 1px solid #e1e0d9; }
    div[role="radiogroup"] label {
        background: #f9f9f7;
        border: 1px solid #e1e0d9;
        border-bottom: none;
        border-radius: 8px 8px 0 0;
        padding: 6px 14px;
    }
    div[role="radiogroup"] label:has(input:checked) {
        background: #2a78d6;
        border-color: #2a78d6;
    }
    div[role="radiogroup"] label:has(input:checked) p {
        color: white !important;
        font-weight: 600;
    }
    </style>
    """,
    unsafe_allow_html=True,
)
active_tab = st.radio(
    "Navigation", TAB_NAMES, horizontal=True, label_visibility="collapsed", key="active_tab"
)

if active_tab == "State Overview":
    st.markdown("##### Acute-Care Capacity")
    acute_col1, acute_col2, acute_col3, acute_col4 = st.columns(4)
    with acute_col1:
        kpi_card("Staffed Acute Beds", f"{total_acute_staffed:,.0f}", ACUTE_ACCENT)
    with acute_col2:
        kpi_card("Occupied Acute Beds", f"{total_acute_occupied:,.0f}", ACUTE_ACCENT)
    with acute_col3:
        kpi_card("Available Acute Beds", f"{total_acute_available:,.0f}", ACUTE_ACCENT)
    with acute_col4:
        kpi_badge("Statewide Acute Occupancy", f"{state_acute_occupancy:.1f}%", state_acute_occupancy)

    st.markdown("##### ICU Capacity")
    icu_col1, icu_col2, icu_col3, icu_col4 = st.columns(4)
    with icu_col1:
        kpi_card("Staffed ICU Beds", f"{total_icu_staffed:,.0f}", ICU_ACCENT)
    with icu_col2:
        kpi_card("Occupied ICU Beds", f"{total_icu_occupied:,.0f}", ICU_ACCENT)
    with icu_col3:
        kpi_card("Available ICU Beds", f"{total_icu_available:,.0f}", ICU_ACCENT)
    with icu_col4:
        kpi_badge("Statewide ICU Occupancy", f"{state_icu_occupancy:.1f}%", state_icu_occupancy)

    st.subheader("Hospitals Under the Most Capacity Pressure")
    ranking_cols = [
        "Facility Name",
        "Facility County",
        "DOH Region",
        "Acute Occupancy %",
        "ICU Occupancy %",
        "Total Staffed Acute Care Beds Available",
        "Total Staffed ICU Beds Currently Available",
    ]
    ranked = (
        df[ranking_cols]
        .sort_values("Acute Occupancy %", ascending=False)
        .reset_index(drop=True)
    )
    st.dataframe(
        ranked.style.format({"Acute Occupancy %": "{:.1f}", "ICU Occupancy %": "{:.1f}"}).map(
            pressure_style, subset=["Acute Occupancy %", "ICU Occupancy %"]
        ),
        use_container_width=True,
    )
    st.caption(
        "Pressure colors are illustrative analytical thresholds (75% / 85% / 95%), "
        "not medical or regulatory standards. " + OVER_100_NOTE
    )

    with st.expander("Raw data preview"):
        st.dataframe(df.head(20), use_container_width=True)

elif active_tab == "Hospital Explorer":
    hospital_name = st.selectbox("Select Hospital", sorted(df["Facility Name"].unique()))
    hosp = df[df["Facility Name"] == hospital_name].iloc[0]

    st.markdown(
        f"**County:** {hosp['Facility County']}  |  "
        f"**DOH Region:** {hosp['DOH Region']}  |  "
        f"**Network:** {hosp['Facility Network']}"
    )
    st.caption(f"Reporting date: {hosp['As of Date'].strftime('%B %d, %Y')}")

    st.markdown("#### Acute-Care Beds")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Staffed", f"{hosp['Total Staffed Acute Care Beds']:,.0f}")
    c2.metric("Occupied", f"{hosp['Total Staffed Acute Care Beds Occupied']:,.0f}")
    c3.metric("Available", f"{hosp['Total Staffed Acute Care Beds Available']:,.0f}")
    c4.metric("Occupancy", fmt_pct(hosp["Acute Occupancy %"]))

    st.markdown("#### ICU Beds")
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Staffed", f"{hosp['Total Staffed ICU Beds']:,.0f}")
    c6.metric("Occupied", f"{hosp['Total Staffed ICU Beds Currently Occupied']:,.0f}")
    c7.metric("Available", f"{hosp['Total Staffed ICU Beds Currently Available']:,.0f}")
    c8.metric("Occupancy", fmt_pct(hosp["ICU Occupancy %"]))

    if hosp["Acute Occupancy %"] > 100 or hosp["ICU Occupancy %"] > 100:
        st.caption(OVER_100_NOTE)

    st.markdown("#### Acute Occupancy in Context")
    region_occ = region_agg.loc[hosp["DOH Region"], "Acute Occupancy %"]
    network_occ = network_agg.loc[hosp["Facility Network"], "Acute Occupancy %"]
    ctx1, ctx2, ctx3 = st.columns(3)
    ctx1.metric("This Hospital", fmt_pct(hosp["Acute Occupancy %"]))
    ctx2.metric(f"{hosp['DOH Region']} Region", fmt_pct(region_occ))
    ctx3.metric("Statewide", fmt_pct(state_acute_occupancy))
    st.caption(f"Network ({hosp['Facility Network']}) acute occupancy: {fmt_pct(network_occ)}")

elif active_tab == "Regional Analysis":
    region_table = region_agg.reset_index().sort_values("Acute Occupancy %", ascending=True)

    fig = px.bar(
        region_table,
        x="Acute Occupancy %",
        y="DOH Region",
        orientation="h",
        text=region_table["Acute Occupancy %"].round(1).astype(str) + "%",
    )
    fig.update_traces(marker_color="#2a78d6", textposition="outside", cliponaxis=False)
    fig.update_traces(name="Acute occupancy", showlegend=True)
    fig.update_layout(
        plot_bgcolor="#fcfcfb",
        paper_bgcolor="#fcfcfb",
        font_color="#0b0b0b",
        xaxis=dict(title="Acute Occupancy %", gridcolor="#e1e0d9", zeroline=False),
        yaxis=dict(title=None, gridcolor="#e1e0d9"),
        legend_title_text="Measure",
        margin=dict(l=10, r=10, t=10, b=10),
        height=max(320, 32 * len(region_table)),
    )
    st.subheader("Acute-Care Occupancy by DOH Region")
    st.plotly_chart(fig, use_container_width=True)

    st.caption(
        "Hover over a region for its acute and ICU occupancy. Pressure colors are "
        "illustrative analytical thresholds (75% / 85% / 95%), not medical or "
        "regulatory standards. " + OVER_100_NOTE
    )

elif active_tab == "Network Analysis":
    network_names = sorted(network_agg.index)
    selected_network = st.selectbox("Select Hospital Network", network_names)
    net_row = network_agg.loc[selected_network]

    st.markdown(f"#### {selected_network}")
    n1, n2, n3, n4 = st.columns(4)
    n1.metric("Hospitals in Network", f"{net_row['facility_count']:,.0f}")
    n2.metric("Staffed Acute Beds", f"{net_row['staffed_acute']:,.0f}")
    n3.metric("Available Acute Beds", f"{net_row['available_acute']:,.0f}")
    n4.metric("Network Acute Occupancy", fmt_pct(net_row["Acute Occupancy %"]))

    n5, n6, n7, n8 = st.columns(4)
    n5.metric("Staffed ICU Beds", f"{net_row['staffed_icu']:,.0f}")
    n6.metric("Available ICU Beds", f"{net_row['available_icu']:,.0f}")
    n7.metric("Network ICU Occupancy", fmt_pct(net_row["ICU Occupancy %"]))
    n8.metric("Statewide Acute Occupancy", fmt_pct(state_acute_occupancy))

    st.subheader(f"Hospitals in {selected_network}")
    network_hospitals_cols = [
        "Facility Name",
        "Facility County",
        "DOH Region",
        "Acute Occupancy %",
        "ICU Occupancy %",
        "Total Staffed Acute Care Beds Available",
        "Total Staffed ICU Beds Currently Available",
    ]
    network_hospitals = (
        df[df["Facility Network"] == selected_network][network_hospitals_cols]
        .sort_values("Acute Occupancy %", ascending=False)
        .reset_index(drop=True)
    )
    st.dataframe(
        network_hospitals.style.format(
            {"Acute Occupancy %": "{:.1f}", "ICU Occupancy %": "{:.1f}"}
        ).map(pressure_style, subset=["Acute Occupancy %", "ICU Occupancy %"]),
        use_container_width=True,
    )
    st.caption(
        "Helps distinguish whether capacity pressure is isolated to one hospital "
        "or affecting the whole network. Pressure colors are illustrative analytical "
        "thresholds (75% / 85% / 95%), not medical or regulatory standards. " + OVER_100_NOTE
    )

elif active_tab == "Priority Dashboard":
    st.caption(
        "Ranks every hospital by whichever is worse -- acute or ICU occupancy -- so a "
        "hospital that's fine on acute beds but critical on ICU still surfaces. "
        "Priority labels are illustrative analytical thresholds (75% / 85% / 95%), "
        "not medical or regulatory standards. " + OVER_100_NOTE
    )

    st.subheader("Statewide Acute-Care Occupancy by County")
    county_plot_df = county_agg.reset_index()
    st.plotly_chart(
        county_choropleth(county_plot_df, "Facility County"),
        use_container_width=True,
    )

    st.subheader("Hospital Capacity — 3D Map (Statewide)")
    state_map_date = weekly_slider(history_df, latest_date, key="state_map_week")
    st.caption(f"Week of {state_map_date.strftime('%B %d, %Y')}")

    day_state = history_df[history_df["As of Date"] == state_map_date].copy()

    render_hospital_3d_map(
        day_state,
        pdk.ViewState(latitude=42.9, longitude=-75.5, zoom=6, pitch=45, bearing=0),
        subtitle_col="Facility County",
        region_label="New York State",
        radius=1500,
        elevation_scale=200,
    )
    render_pressure_legend()

    st.subheader("Hospital Priority Ranking")
    priority_cols = [
        "Facility Name",
        "Facility County",
        "DOH Region",
        "Facility Network",
        "Acute Occupancy %",
        "ICU Occupancy %",
        "Total Staffed Acute Care Beds Available",
        "Total Staffed ICU Beds Currently Available",
    ]
    priority_df = df[priority_cols].copy()
    priority_df["Priority Bucket"] = [
        priority_bucket(a, i)
        for a, i in zip(priority_df["Acute Occupancy %"], priority_df["ICU Occupancy %"])
    ]
    priority_df["Priority"] = priority_df["Priority Bucket"].apply(priority_label)
    priority_df = priority_df.sort_values(
        ["Priority Bucket", "Acute Occupancy %"], ascending=[False, False]
    ).drop(columns="Priority Bucket").reset_index(drop=True)
    priority_df = priority_df[
        ["Priority"] + [c for c in priority_df.columns if c != "Priority"]
    ]

    st.dataframe(
        priority_df.style.format(
            {"Acute Occupancy %": "{:.1f}", "ICU Occupancy %": "{:.1f}"}
        ).map(
            pressure_style, subset=["Acute Occupancy %", "ICU Occupancy %"]
        ).apply(priority_row_style, axis=1),
        use_container_width=True,
    )

elif active_tab == "NYC Boroughs":
    st.caption(
        f"{nyc_df['Facility PFI'].nunique()} facilities across the five NYC boroughs "
        f"(Manhattan, Brooklyn, Bronx, Queens, Staten Island)."
    )

    st.subheader("Acute-Care Occupancy Map")
    st.plotly_chart(
        county_choropleth(nyc_county_agg, "Facility County", hover_name_col="Borough"),
        use_container_width=True,
    )

    borough_table = borough_agg.reset_index().sort_values("Acute Occupancy %", ascending=True)

    st.subheader("Borough Comparison Table")
    borough_display = borough_table.rename(
        columns={
            "facility_count": "Hospitals",
            "staffed_acute": "Staffed Acute Beds",
            "occupied_acute": "Occupied Acute Beds",
            "available_acute": "Available Acute Beds",
            "staffed_icu": "Staffed ICU Beds",
            "occupied_icu": "Occupied ICU Beds",
            "available_icu": "Available ICU Beds",
        }
    ).sort_values("Acute Occupancy %", ascending=False)
    st.dataframe(
        borough_display.style.format(
            {"Acute Occupancy %": "{:.1f}", "ICU Occupancy %": "{:.1f}"}
        ).map(pressure_style, subset=["Acute Occupancy %", "ICU Occupancy %"]),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        "Pressure colors are illustrative analytical thresholds (75% / 85% / 95%), "
        "not medical or regulatory standards. " + OVER_100_NOTE
    )

    st.subheader("Hospitals in Selected Borough")
    selected_borough = st.selectbox("Select Borough", sorted(borough_agg.index))
    borough_hospitals_cols = [
        "Facility Name",
        "Facility County",
        "Facility Network",
        "Acute Occupancy %",
        "ICU Occupancy %",
        "Total Staffed Acute Care Beds Available",
        "Total Staffed ICU Beds Currently Available",
    ]
    borough_hospitals = (
        nyc_df[nyc_df["Borough"] == selected_borough][borough_hospitals_cols]
        .sort_values("Acute Occupancy %", ascending=False)
        .reset_index(drop=True)
    )
    st.dataframe(
        borough_hospitals.style.format(
            {"Acute Occupancy %": "{:.1f}", "ICU Occupancy %": "{:.1f}"}
        ).map(pressure_style, subset=["Acute Occupancy %", "ICU Occupancy %"]),
        use_container_width=True,
    )

    st.subheader("Hospital Capacity — 3D Map")
    nyc_map_date = weekly_slider(history_df, latest_date, key="nyc_map_week")
    st.caption(f"Week of {nyc_map_date.strftime('%B %d, %Y')}")

    day_nyc = history_df[
        (history_df["As of Date"] == nyc_map_date)
        & (history_df["Facility County"].isin(NYC_BOROUGH_MAP))
    ].copy()
    day_nyc["Borough"] = day_nyc["Facility County"].map(NYC_BOROUGH_MAP)

    render_hospital_3d_map(
        day_nyc,
        pdk.ViewState(latitude=40.72, longitude=-73.95, zoom=9.8, pitch=45, bearing=0),
        subtitle_col="Borough",
        region_label="NYC",
        radius=120,
        elevation_scale=60,
    )
    render_pressure_legend()

elif active_tab == "Historical Trends":
    st.caption(
        f"Full reporting history: {history_df['As of Date'].min().strftime('%B %d, %Y')} "
        f"to {latest_date.strftime('%B %d, %Y')} "
        f"({history_df['As of Date'].nunique()} days)."
    )

    st.subheader("Statewide Occupancy Trend")
    trend_col1, trend_col2 = st.columns(2)
    with trend_col1:
        acute_delta = state_acute_occupancy - acute_week_ago if pd.notna(acute_week_ago) else None
        st.metric(
            "Acute Occupancy (vs. 7 days ago)",
            f"{state_acute_occupancy:.1f}%",
            delta=f"{acute_delta:+.1f} pts" if acute_delta is not None else None,
            delta_color="inverse",
        )
    with trend_col2:
        icu_delta = state_icu_occupancy - icu_week_ago if pd.notna(icu_week_ago) else None
        st.metric(
            "ICU Occupancy (vs. 7 days ago)",
            f"{state_icu_occupancy:.1f}%",
            delta=f"{icu_delta:+.1f} pts" if icu_delta is not None else None,
            delta_color="inverse",
        )

    fig_trend = px.line(statewide_daily, x="As of Date", y=["Acute 7-Day Avg", "ICU 7-Day Avg"])
    trend_colors = {"Acute 7-Day Avg": ACUTE_ACCENT, "ICU 7-Day Avg": ICU_ACCENT}
    for trace in fig_trend.data:
        trace.line.color = trend_colors[trace.name]
        trace.name = trace.name.replace(" 7-Day Avg", "")
    fig_trend.update_layout(
        plot_bgcolor="#fcfcfb",
        paper_bgcolor="#fcfcfb",
        font_color="#0b0b0b",
        xaxis=dict(title=None, gridcolor="#e1e0d9"),
        yaxis=dict(title="Occupancy % (7-day avg)", gridcolor="#e1e0d9", zeroline=False),
        legend_title_text="",
        margin=dict(l=10, r=10, t=10, b=10),
    )
    st.plotly_chart(fig_trend, use_container_width=True)
    st.caption(
        "Statewide acute and ICU occupancy, smoothed with a 7-day rolling average "
        "to remove day-to-day reporting noise."
    )

    st.subheader("Hospital Trend")
    # Facility Name text isn't stable over time (formatting changes, real
    # renames) but Facility PFI is, so build the selector from the latest
    # snapshot's clean PFI-to-name mapping and filter history by PFI --
    # matching by name would silently truncate the trend for ~76 hospitals
    # whose name string changed partway through the 340-day history.
    trend_hospital_options = sorted(zip(df["Facility Name"], df["Facility PFI"]))
    trend_hospital = st.selectbox(
        "Select Hospital",
        [name for name, _ in trend_hospital_options],
        key="trend_hospital",
    )
    trend_pfi = dict(trend_hospital_options)[trend_hospital]
    hosp_history = (
        history_df[history_df["Facility PFI"] == trend_pfi]
        .sort_values("As of Date")
        .copy()
    )
    hosp_history["Acute 7-Day Avg"] = hosp_history["Acute Occupancy %"].rolling(7, min_periods=1).mean()

    fig_hosp = px.line(hosp_history, x="As of Date", y="Acute Occupancy %")
    fig_hosp.data[0].line.color = "#cde2fb"
    fig_hosp.data[0].line.width = 1
    fig_hosp.data[0].name = "Daily"
    fig_hosp.data[0].showlegend = True
    fig_hosp.add_scatter(
        x=hosp_history["As of Date"],
        y=hosp_history["Acute 7-Day Avg"],
        mode="lines",
        name="7-Day Avg",
        line=dict(color=ACUTE_ACCENT, width=2.5),
    )
    fig_hosp.update_layout(
        plot_bgcolor="#fcfcfb",
        paper_bgcolor="#fcfcfb",
        font_color="#0b0b0b",
        xaxis=dict(title=None, gridcolor="#e1e0d9"),
        yaxis=dict(title="Acute Occupancy %", gridcolor="#e1e0d9", zeroline=False),
        legend_title_text="",
        margin=dict(l=10, r=10, t=10, b=10),
    )
    st.plotly_chart(fig_hosp, use_container_width=True)
    caption_text = f"Daily acute occupancy for {trend_hospital}, with a 7-day rolling average overlaid."
    if (hosp_history["Acute Occupancy %"] > 100).any():
        caption_text += " " + OVER_100_NOTE
    st.caption(caption_text)

elif active_tab == "Capital Investment Insights":
    st.caption(
        "Synthesizes the occupancy, regional, network, and trend data from the other tabs "
        "into a ranked view of where sustained bed-capacity pressure is greatest -- the kind "
        "of signal that informs where public capital financing for hospital construction and "
        "expansion gets prioritized in New York. The two public institutions most directly "
        "involved in financing hospital capacity upgrades statewide are the **Dormitory "
        "Authority of the State of New York (DASNY)**, which issues bonds and manages "
        "construction financing for the large majority of the state's hospital and healthcare "
        "capital projects, and the **NYS Department of Health**, which awards capital grants "
        "(e.g. the Statewide Health Care Facility Transformation Program) that DASNY often "
        "administers the financing for. This section is a data-driven decision-support signal, "
        "not an official funding recommendation."
    )

    region_capital = capital_priority_table(history_df, region_agg, "DOH Region", latest_date)
    top_region_name = region_capital.index[0]
    top_region = region_capital.iloc[0]

    st.subheader("Statewide Snapshot")
    s1, s2, s3 = st.columns(3)
    s1.metric("Statewide Acute Occupancy", f"{state_acute_occupancy:.1f}%")
    s2.metric(
        "Highest-Priority Region",
        top_region_name,
        delta=f"{top_region['Acute Occupancy %']:.1f}% acute occupancy",
        delta_color="off",
    )
    s3.metric(
        "Available Acute Beds There",
        f"{top_region['available_acute']:,.0f}",
        delta=f"across {top_region['facility_count']:.0f} hospitals",
        delta_color="off",
    )

    st.subheader("Regions Ranked by Capital Investment Priority")
    st.caption(
        "Priority score blends current occupancy, the 30-day sustained average, and the "
        "30-day trend, so a region under real ongoing strain outranks one having a single "
        "bad day. " + OVER_100_NOTE
    )
    region_display = region_capital.rename(
        columns={
            "facility_count": "Hospitals",
            "staffed_acute": "Staffed Acute Beds",
            "available_acute": "Available Acute Beds",
        }
    ).reset_index().rename(columns={"DOH Region": "DOH Region"})
    st.dataframe(
        region_display.style.format(
            {
                "Acute Occupancy %": "{:.1f}",
                "ICU Occupancy %": "{:.1f}",
                "Acute 30-Day Avg %": "{:.1f}",
                "Trend (pts)": "{:+.1f}",
                "Capital Priority Score": "{:.1f}",
            }
        ).map(pressure_style, subset=["Acute Occupancy %", "ICU Occupancy %", "Acute 30-Day Avg %"]),
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("Top Candidate Regions for Capital Review")
    for region_name, row in region_capital.head(3).iterrows():
        st.markdown(f"- {capital_insight_text(region_name, row)}")

    st.subheader(f"Hospitals Driving Priority in {top_region_name}")
    st.caption(
        "The individual facilities within the top-ranked region that would likely be the "
        "focus of any capital review -- ranked by current acute occupancy."
    )
    region_hospital_cols = [
        "Facility Name",
        "Facility County",
        "Facility Network",
        "Acute Occupancy %",
        "ICU Occupancy %",
        "Total Staffed Acute Care Beds Available",
    ]
    region_hospitals = (
        df[df["DOH Region"] == top_region_name][region_hospital_cols]
        .sort_values("Acute Occupancy %", ascending=False)
        .head(10)
        .reset_index(drop=True)
    )
    st.dataframe(
        region_hospitals.style.format(
            {"Acute Occupancy %": "{:.1f}", "ICU Occupancy %": "{:.1f}"}
        ).map(pressure_style, subset=["Acute Occupancy %", "ICU Occupancy %"]),
        use_container_width=True,
    )

    st.subheader("Networks Ranked by Capital Investment Priority")
    st.caption(
        "Network-level view -- useful because a single capital award to a health system can "
        "often expand capacity across several affiliated hospitals at once."
    )
    network_capital = capital_priority_table(history_df, network_agg, "Facility Network", latest_date)
    network_display = network_capital.rename(
        columns={
            "facility_count": "Hospitals",
            "staffed_acute": "Staffed Acute Beds",
            "available_acute": "Available Acute Beds",
        }
    ).reset_index()
    st.dataframe(
        network_display.head(10).style.format(
            {
                "Acute Occupancy %": "{:.1f}",
                "ICU Occupancy %": "{:.1f}",
                "Acute 30-Day Avg %": "{:.1f}",
                "Trend (pts)": "{:+.1f}",
                "Capital Priority Score": "{:.1f}",
            }
        ).map(pressure_style, subset=["Acute Occupancy %", "ICU Occupancy %", "Acute 30-Day Avg %"]),
        use_container_width=True,
        hide_index=True,
    )
