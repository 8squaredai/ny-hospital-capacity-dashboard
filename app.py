import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="NY Hospital Capacity Dashboard", layout="wide")

DATA_PATH = "data/New_York_State_Statewide_Hospital_Bed_Capacity_20260817.csv"

NUMERIC_COLS = [
    "Total Staffed Acute Care Beds",
    "Total Staffed Acute Care Beds Occupied",
    "Total Staffed Acute Care Beds Available",
    "Total Staffed ICU Beds",
    "Total Staffed ICU Beds Currently Occupied",
    "Total Staffed ICU Beds Currently Available",
]


@st.cache_data
def load_data(path: str) -> tuple[pd.DataFrame, pd.Timestamp]:
    df = pd.read_csv(path, low_memory=False)

    for col in NUMERIC_COLS:
        df[col] = pd.to_numeric(
            df[col].astype(str).str.replace(",", "", regex=False), errors="coerce"
        )

    df["As of Date"] = pd.to_datetime(df["As of Date"], format="%m/%d/%Y")

    # The downloaded CSV is a full historical export (one row per facility per
    # reporting date). For this snapshot view, keep only the most recent date.
    latest_date = df["As of Date"].max()
    df = df[df["As of Date"] == latest_date].copy()
    df = df.drop_duplicates(subset="Facility PFI", keep="last")

    staffed_acute = df["Total Staffed Acute Care Beds"].replace(0, np.nan)
    staffed_icu = df["Total Staffed ICU Beds"].replace(0, np.nan)
    df["Acute Occupancy %"] = (
        df["Total Staffed Acute Care Beds Occupied"] / staffed_acute * 100
    )
    df["ICU Occupancy %"] = (
        df["Total Staffed ICU Beds Currently Occupied"] / staffed_icu * 100
    )

    return df, latest_date


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


df, latest_date = load_data(DATA_PATH)
region_agg = aggregate_occupancy(df, "DOH Region")
network_agg = aggregate_occupancy(df, "Facility Network")

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

tab_overview, tab_explorer, tab_regional = st.tabs(
    ["State Overview", "Hospital Explorer", "Regional Analysis"]
)

with tab_overview:
    acute_col1, acute_col2, acute_col3, acute_col4 = st.columns(4)
    acute_col1.metric("Staffed Acute Beds", f"{total_acute_staffed:,.0f}")
    acute_col2.metric("Occupied Acute Beds", f"{total_acute_occupied:,.0f}")
    acute_col3.metric("Available Acute Beds", f"{total_acute_available:,.0f}")
    acute_col4.metric("Statewide Acute Occupancy", f"{state_acute_occupancy:.1f}%")

    icu_col1, icu_col2, icu_col3, icu_col4 = st.columns(4)
    icu_col1.metric("Staffed ICU Beds", f"{total_icu_staffed:,.0f}")
    icu_col2.metric("Occupied ICU Beds", f"{total_icu_occupied:,.0f}")
    icu_col3.metric("Available ICU Beds", f"{total_icu_available:,.0f}")
    icu_col4.metric("Statewide ICU Occupancy", f"{state_icu_occupancy:.1f}%")

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
        ranked.style.format({"Acute Occupancy %": "{:.1f}", "ICU Occupancy %": "{:.1f}"}),
        use_container_width=True,
    )

    with st.expander("Raw data preview"):
        st.dataframe(df.head(20), use_container_width=True)

with tab_explorer:
    hospital_name = st.selectbox("Select Hospital", sorted(df["Facility Name"].unique()))
    hosp = df[df["Facility Name"] == hospital_name].iloc[0]

    st.markdown(
        f"**County:** {hosp['Facility County']}  |  "
        f"**DOH Region:** {hosp['DOH Region']}  |  "
        f"**Network:** {hosp['Facility Network']}"
    )
    st.caption(f"Reporting date: {hosp['As of Date'].strftime('%B %d, %Y')}")

    def fmt_pct(value: float) -> str:
        return f"{value:.1f}%" if pd.notna(value) else "N/A"

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

    st.markdown("#### Acute Occupancy in Context")
    region_occ = region_agg.loc[hosp["DOH Region"], "Acute Occupancy %"]
    network_occ = network_agg.loc[hosp["Facility Network"], "Acute Occupancy %"]
    ctx1, ctx2, ctx3 = st.columns(3)
    ctx1.metric("This Hospital", fmt_pct(hosp["Acute Occupancy %"]))
    ctx2.metric(f"{hosp['DOH Region']} Region", fmt_pct(region_occ))
    ctx3.metric("Statewide", fmt_pct(state_acute_occupancy))
    st.caption(f"Network ({hosp['Facility Network']}) acute occupancy: {fmt_pct(network_occ)}")

with tab_regional:
    region_table = region_agg.reset_index().sort_values("Acute Occupancy %", ascending=True)

    fig = px.bar(
        region_table,
        x="Acute Occupancy %",
        y="DOH Region",
        orientation="h",
        text=region_table["Acute Occupancy %"].round(1).astype(str) + "%",
    )
    fig.update_traces(marker_color="#2a78d6", textposition="outside", cliponaxis=False)
    fig.update_layout(
        plot_bgcolor="#fcfcfb",
        paper_bgcolor="#fcfcfb",
        font_color="#0b0b0b",
        xaxis=dict(title="Acute Occupancy %", gridcolor="#e1e0d9", zeroline=False),
        yaxis=dict(title=None, gridcolor="#e1e0d9"),
        margin=dict(l=10, r=10, t=10, b=10),
        height=max(320, 32 * len(region_table)),
    )
    st.subheader("Acute-Care Occupancy by DOH Region")
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Region Comparison Table")
    region_display = region_table.rename(
        columns={
            "DOH Region": "DOH Region",
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
        region_display.style.format(
            {"Acute Occupancy %": "{:.1f}", "ICU Occupancy %": "{:.1f}"}
        ),
        use_container_width=True,
        hide_index=True,
    )
