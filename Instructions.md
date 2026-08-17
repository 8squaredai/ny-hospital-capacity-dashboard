# Claude Code Project Brief — New York Hospital Capacity Decision App

## 1. Project Overview

We are building a **Streamlit business decision-support app** using real public hospital capacity data from the **New York State Department of Health**.

The app should help **New York State health officials / regional hospital operations decision-makers** understand hospital capacity pressure across the state and identify **which hospitals, regions, and hospital networks may require the most operational attention or further resource investigation**.

This is a university Python for Data Analytics project, so the app should demonstrate:

- Python
- pandas
- data cleaning
- calculated metrics
- grouping and aggregation
- interactive filtering
- useful visualizations
- clear business recommendations
- reproducible analysis
- eventually an optional LLM-powered recommendation feature

The focus should be on **useful decision-making**, not on adding unnecessary features.

---

# 2. Main Business Question

The main question the app should help answer is:

> **Which New York hospitals, regions, and hospital networks are experiencing the greatest hospital capacity pressure and should therefore be prioritized for further operational investigation or potential resource support?**

Important:

The data does **not** contain:

- staff numbers
- nurse numbers
- doctor numbers
- hospital budgets
- patient-level information
- patient severity
- transfer eligibility
- specialty availability

Therefore, the app must **not claim** things like:

> "Hospital X needs 20 more nurses."

Instead, it may say:

> "Hospital X is experiencing unusually high capacity pressure and should be prioritized for further operational investigation."

or:

> "Management should investigate whether additional capacity, staffing, regional support, or other operational interventions are appropriate."

The app identifies **where pressure exists**, not necessarily the root cause.

---

# 3. Dataset

Dataset:

**New York State Statewide Hospital Bed Capacity**

Official source:

https://health.data.ny.gov/Health/New-York-State-Statewide-Hospital-Bed-Capacity/2dbc-sqe7

The dataset contains one row per hospital/facility for the reporting date.

The important columns are:

| Column | Meaning |
|---|---|
| `as_of_date` | Date that the hospital reported its capacity |
| `facility_pfi` | Unique hospital/facility identifier |
| `facility_name` | Hospital name |
| `doh_region` | NY Department of Health region |
| `facility_county` | County where the hospital is located |
| `facility_network` | Hospital network/system |
| `ny_forward_region` | NY Forward regional grouping |
| `total_staffed_acute_care` | Total operational staffed acute-care beds |
| `total_staffed_acute_care_1` | Occupied staffed acute-care beds |
| `total_staffed_acute_care_2` | Available staffed acute-care beds |
| `total_staffed_icu_beds` | Total operational staffed ICU beds |
| `total_staffed_icu_beds_1` | Occupied staffed ICU beds |
| `total_staffed_icu_beds_2` | Available staffed ICU beds |

The downloaded CSV currently appears to mainly provide the current reporting snapshot.

For the initial MVP, use the downloaded CSV.

Later, we want to save snapshots over multiple days and build our own historical dataset.

---

# 4. Core Metrics We Need to Calculate

Do not rely only on the raw variables.

Create calculated columns using pandas.

## Acute-Care Occupancy Rate

```python
acute_occupancy_rate = (
    occupied_acute_beds / total_staffed_acute_beds
) * 100
```

Using the dataset:

```python
df["acute_occupancy_rate"] = (
    df["total_staffed_acute_care_1"]
    / df["total_staffed_acute_care"]
    * 100
)
```

Handle hospitals with zero staffed beds so we do not divide by zero.

---

## ICU Occupancy Rate

```python
icu_occupancy_rate = (
    occupied_icu_beds / total_staffed_icu_beds
) * 100
```

Using:

```python
df["icu_occupancy_rate"] = (
    df["total_staffed_icu_beds_1"]
    / df["total_staffed_icu_beds"]
    * 100
)
```

Again, safely handle zero ICU beds.

---

# 5. Important Aggregation Rule

When calculating occupancy for a region or hospital network, do **not simply average individual hospital percentages**.

Instead, calculate weighted/system occupancy:

```text
Total occupied beds across the group
÷
Total staffed beds across the group
× 100
```

For example:

```python
regional_occupancy = (
    region["total_staffed_acute_care_1"].sum()
    / region["total_staffed_acute_care"].sum()
    * 100
)
```

This is preferable because hospitals have very different numbers of beds.

Use the same approach for ICU occupancy.

---

# 6. Initial App Structure

For the first version, build **five main sections/pages**.

Do not build everything at once.

Priority is to get the core app working first.

## Page 1 — State Overview

Purpose:

Give a high-level overview of New York State hospital capacity.

Show KPI cards for:

- total staffed acute-care beds
- total occupied acute-care beds
- total available acute-care beds
- statewide acute occupancy %
- total staffed ICU beds
- total occupied ICU beds
- available ICU beds
- statewide ICU occupancy %

Also show:

### Top Hospitals Under Capacity Pressure

A table or bar chart showing hospitals with the highest acute-care occupancy.

Potential columns:

- Facility Name
- County
- DOH Region
- Acute Occupancy %
- ICU Occupancy %
- Available Acute Beds
- Available ICU Beds

Include filters if helpful.

---

# 7. Page 2 — Hospital Explorer

The user should be able to select one hospital from a dropdown.

Example:

```text
Select Hospital:
[ NYU Langone ▼ ]
```

Once selected, show:

### Basic Hospital Information

- facility name
- county
- DOH region
- facility network
- reporting date

### Acute-Care Statistics

- total staffed acute-care beds
- occupied acute beds
- available acute beds
- acute occupancy %

### ICU Statistics

- total staffed ICU beds
- occupied ICU beds
- available ICU beds
- ICU occupancy %

Use Streamlit `st.metric()` components where appropriate.

Example:

```text
Acute Occupancy
94.3%

Available Acute Beds
31

ICU Occupancy
96.1%

Available ICU Beds
2
```

Also compare the selected hospital against:

- statewide occupancy
- its DOH regional occupancy
- its hospital network occupancy

Example:

```text
Hospital Acute Occupancy: 94%
Region: 84%
State: 82%
```

This gives context rather than showing an isolated number.

---

# 8. Page 3 — Regional Analysis

Allow the user to select a DOH region.

For each region calculate:

- number of hospitals
- total staffed acute-care beds
- occupied acute-care beds
- available acute-care beds
- acute occupancy %
- ICU staffed beds
- ICU occupied beds
- ICU available beds
- ICU occupancy %

Create a comparison chart showing all regions.

Possible chart:

```text
Acute Care Occupancy by DOH Region

New York City       ███████████████ 89%
Long Island         █████████████ 84%
Hudson Valley       ████████████ 81%
Western NY          ███████████ 77%
```

Use Plotly.

Also show a table ranking the regions.

Eventually we want a geographic heat map, but do **not make the geographic heat map a blocker for the initial MVP**.

Build the normal regional comparison first.

---

# 9. Page 4 — Hospital Network Analysis

Group hospitals using:

```python
facility_network
```

Allow the user to select a hospital network.

Show:

- number of hospitals in the network
- total staffed acute beds
- total available acute beds
- network acute occupancy %
- total ICU beds
- available ICU beds
- network ICU occupancy %

Then list all hospitals belonging to the selected network.

Rank those hospitals by:

- acute occupancy
- ICU occupancy
- available capacity

Goal:

Help determine whether capacity pressure is:

- isolated to a single hospital
- affecting multiple hospitals in the same network

---

# 10. Page 5 — Priority Dashboard

This is potentially the most important business-decision page.

Purpose:

Answer:

> "Where should officials focus attention first?"

Initially, keep the ranking simple.

Show hospitals ranked by factors such as:

- acute occupancy %
- ICU occupancy %
- number of available acute beds
- number of available ICU beds

Example:

```text
Highest Capacity Pressure

1. Hospital A
Acute: 97%
ICU: 98%
Available acute beds: 14
Available ICU beds: 1

2. Hospital B
Acute: 95%
ICU: 94%
Available acute beds: 21
Available ICU beds: 3

3. Hospital C
Acute: 93%
ICU: 90%
Available acute beds: 37
Available ICU beds: 5
```

Do NOT invent medically validated definitions of "critical" or "safe."

If we create categories such as:

- High
- Medium
- Low

the thresholds should initially be described as **analytical/app thresholds**, not official medical standards unless we later find a credible source supporting specific thresholds.

---

# 11. Pressure Score — Later Feature

Eventually we may create our own **Hospital Capacity Pressure Score**.

Example concept:

```text
Pressure Score = combination of:

Acute occupancy
ICU occupancy
Remaining acute capacity
Remaining ICU capacity
Historical trend
Persistence of high occupancy
```

Potential scale:

```text
0–100
```

Example output:

```text
Hospital A
Pressure Score: 91 / 100

Priority:
High
```

Do NOT implement an arbitrary complicated formula immediately.

First create the underlying metrics and rankings.

We can design and justify the pressure score later.

---

# 12. Historical Data Idea

The official dataset appears mainly to provide a current snapshot.

We want to create our own historical dataset by saving the snapshot each day.

Basic concept:

```text
Today's DataFrame
+
Previously Stored Historical DataFrame
↓
pd.concat()
↓
Remove duplicate facility/date combinations
↓
Save back to CSV
```

Use:

```python
pd.concat()
```

Unique observation should probably be identified using:

```text
as_of_date + facility_pfi
```

Example:

```python
historical_df = pd.concat(
    [historical_df, daily_df],
    ignore_index=True
)

historical_df = historical_df.drop_duplicates(
    subset=["as_of_date", "facility_pfi"],
    keep="last"
)
```

Then save:

```python
historical_df.to_csv(
    "data/hospital_history.csv",
    index=False
)
```

For the university demo, this can initially be run manually.

Do not introduce a complicated database until necessary.

---

# 13. Historical Metrics We Eventually Want

Once multiple reporting days exist, calculate:

### For each hospital:

- current acute occupancy
- previous day's occupancy
- 7-day average acute occupancy
- 7-day average ICU occupancy
- change compared with seven days ago
- highest occupancy in available history
- lowest occupancy
- number of reporting days above a selected threshold
- consecutive high-pressure days
- available-bed trend
- ICU availability trend

Example:

```text
Hospital A

Today:
94%

7-day average:
89%

7-day change:
+5 percentage points

Days above 90%:
5 of last 7
```

This helps distinguish:

> one unusually busy day

from:

> persistent capacity pressure.

---

# 14. Historical Charts

Once sufficient history exists, Hospital Explorer should show a Plotly line chart.

Example:

```text
Hospital A — Acute Occupancy

Aug 17    86%
Aug 18    87%
Aug 19    89%
Aug 20    91%
Aug 21    93%
Aug 22    94%
```

Allow the user to switch between:

- Acute Occupancy
- ICU Occupancy
- Available Acute Beds
- Available ICU Beds

But this is a later feature.

---

# 15. Potential Regional Heat Map

Eventually create a geographic visualization of New York.

Goal:

Color counties or regions by hospital capacity pressure.

For example:

- higher occupancy → higher pressure
- lower occupancy → lower pressure

Potential tools:

- Plotly
- GeoPandas
- New York county GeoJSON

Data would need to be aggregated by:

```python
facility_county
```

or:

```python
doh_region
```

Again:

This is not required for the first working version.

---

# 16. LLM / AI Capacity Advisor — Later Feature

Eventually we want to integrate an LLM, potentially using the OpenAI API.

The LLM should **NOT perform the statistical calculations**.

Python/pandas calculates:

- occupancy
- rankings
- averages
- trends
- comparisons
- pressure scores

Then send a small structured summary to the LLM.

Example:

```json
{
  "hospital": "Hospital A",
  "acute_occupancy": 95.3,
  "icu_occupancy": 97.5,
  "available_acute_beds": 22,
  "available_icu_beds": 2,
  "regional_acute_occupancy": 81.2,
  "state_acute_occupancy": 82.4,
  "seven_day_average": 90.1,
  "change_vs_seven_days_ago": 5.2
}
```

The LLM's role is:

```text
INTERPRET
↓
EXPLAIN
↓
RECOMMEND AN AREA FOR INVESTIGATION
```

NOT:

```text
calculate statistics
```

---

# 17. Example LLM Output

The app might contain a button:

```text
Generate AI Management Brief
```

Output example:

```text
HIGH PRIORITY

Hospital A is currently operating at 95.3% acute-care occupancy,
compared with a regional occupancy rate of 81.2%.

ICU capacity is particularly constrained, with 97.5% of staffed
ICU beds occupied and only two currently available.

The hospital's occupancy has also increased over the past week,
suggesting that the current pressure may not be an isolated event.

Recommendation:
Prioritize Hospital A for further operational review and investigate
whether additional capacity, regional support, or other operational
interventions may be appropriate.

Limitation:
The available dataset does not contain staffing levels, patient
acuity, specialty requirements, transfer feasibility, or operational
cost information.
```

The LLM must only make claims supported by the supplied metrics.

---

# 18. Important AI Guardrail

We should be able to explain during the university presentation:

> "The LLM does not determine which hospital is under pressure. Python calculates all metrics, comparisons, rankings, and trends deterministically. The LLM receives those verified results and translates them into a short management-facing explanation."

This distinction is important.

The app should remain useful even if the LLM API is unavailable.

The core analytics must work without AI.

---

# 19. Potential "Compare Hospitals" Feature

Later, allow the user to select 2–5 hospitals.

Display side-by-side:

| Hospital | Acute Occupancy | ICU Occupancy | Acute Beds Available | ICU Beds Available |
|---|---:|---:|---:|---:|
| Hospital A | 95% | 97% | 25 | 2 |
| Hospital B | 83% | 86% | 88 | 12 |
| Hospital C | 76% | 79% | 104 | 18 |

This can support comparative decision-making.

---

# 20. Potential "Available Capacity Elsewhere" Feature

If one hospital has extremely limited capacity while other facilities in the same region have substantially more available staffed capacity, identify this.

For example:

```text
Hospital A
Acute Occupancy: 97%
Available beds: 12

Hospital B
Acute Occupancy: 72%
Available beds: 103
```

The app may say:

> "Hospital B currently has substantially more available staffed capacity than Hospital A."

However, do NOT automatically recommend transferring patients.

The dataset does not include:

- patient condition
- medical specialty
- distance/travel feasibility
- transfer eligibility
- staffing capabilities

Therefore phrase it as:

> "Officials may wish to investigate whether regional capacity support is operationally feasible."

---

# 21. Recommended Python Libraries

Keep the technology simple.

Initially use:

```python
import pandas as pd
import numpy as np
import streamlit as st
import plotly.express as px
```

Potential later libraries:

```python
import requests
```

for retrieving the NY State dataset automatically.

Potential map tools:

```python
import geopandas as gpd
```

Potential AI:

```python
from openai import OpenAI
```

Do NOT introduce unnecessary frameworks.

---

# 22. Initial Project Structure

Use something simple like:

```text
hospital_capacity_app/
│
├── app.py
├── requirements.txt
├── README.md
│
├── data/
│   ├── hospital_capacity.csv
│   └── hospital_history.csv
│
└── utils/
    ├── data_cleaning.py
    ├── metrics.py
    └── charts.py
```

However, if separating files makes the first version unnecessarily difficult, start with:

```text
hospital_capacity_app/
│
├── app.py
├── requirements.txt
└── data/
    └── hospital_capacity.csv
```

Then refactor once the app works.

---

# 23. Development Order

Please build incrementally.

## Phase 1 — Data

First:

1. Load CSV.
2. Inspect columns.
3. Convert numeric columns safely.
4. Handle missing values.
5. Handle zero-bed facilities.
6. Calculate acute occupancy.
7. Calculate ICU occupancy.
8. Verify that available + occupied approximately matches staffed capacity where appropriate.
9. Check for duplicate hospital/date observations.

Do not build the entire interface until the data pipeline works.

---

## Phase 2 — Basic Streamlit App

Create:

```text
New York Hospital Capacity Dashboard
```

First make sure Streamlit loads successfully.

Then show:

```python
st.dataframe(df.head())
```

Once that works, proceed.

---

## Phase 3 — Hospital Explorer

Add:

```python
st.selectbox()
```

to select a hospital.

Show the selected hospital's basic metrics using:

```python
st.metric()
```

This is the first proper working MVP.

---

## Phase 4 — State Overview

Add statewide KPIs and hospital rankings.

---

## Phase 5 — Regional Analysis

Add regional aggregation and Plotly charts.

---

## Phase 6 — Network Analysis

Add hospital network filtering and comparisons.

---

## Phase 7 — Priority Dashboard

Create rankings highlighting hospitals with the strongest observable capacity pressure.

---

## Phase 8 — Historical Data

Once multiple snapshots are available:

- append data
- build trends
- add rolling calculations
- add historical charts

---

## Phase 9 — LLM

Only after all deterministic analysis works.

---

# 24. Visual Style

The app should feel like a professional healthcare operations dashboard.

Keep the interface:

- clean
- simple
- readable
- executive-friendly
- not overly decorative

Use:

- KPI cards
- clear headings
- simple Plotly charts
- rankings
- concise explanations

Avoid:

- too many charts
- unnecessary animations
- overly complicated navigation
- excessive colors
- clutter

The user should quickly understand:

```text
WHERE IS CAPACITY PRESSURE?
WHY IS IT BEING FLAGGED?
HOW DOES IT COMPARE?
WHAT SHOULD MANAGEMENT INVESTIGATE?
```

---

# 25. Important Data Quality Checks

Please include basic data validation.

Check whether:

```text
Occupied Acute Beds + Available Acute Beds
≈ Total Staffed Acute Beds
```

and:

```text
Occupied ICU Beds + Available ICU Beds
≈ Total Staffed ICU Beds
```

Flag unexpected inconsistencies rather than silently ignoring them.

Also check:

- missing facility names
- missing regions
- duplicated facility IDs
- negative bed counts
- occupancy above 100%
- staffed beds = 0

Document important cleaning decisions.

---

# 26. Important Limitation

The app must prominently acknowledge that hospital bed occupancy alone cannot determine the correct intervention.

The dataset does not tell us:

- staffing shortages
- employee workload
- patient severity
- specialty demand
- hospital finances
- equipment availability
- emergency department waiting times
- reasons for hospital admissions
- transfer suitability

Therefore our recommendation is primarily:

> **Where should officials investigate capacity pressure?**

rather than:

> **Exactly what should that hospital do?**

---

# 27. Final Product Vision

Eventually the workflow should look like:

```text
NY State Department of Health Data
              ↓
          pandas
              ↓
       Data Cleaning
              ↓
    Calculated Capacity Metrics
              ↓
       State Analysis
              ↓
       Hospital Analysis
              ↓
       Region Analysis
              ↓
       Network Analysis
              ↓
      Capacity Prioritization
              ↓
        Streamlit Dashboard
              ↓
     Optional LLM Explanation
```

Historical version:

```text
NY State Data
      ↓
Daily snapshot
      ↓
hospital_history.csv
      ↓
pandas historical analysis
      ↓
current + trends + comparisons
      ↓
Streamlit
      ↓
LLM Management Brief
```

---

# 28. What I Want You to Do First

Do **not build every future feature immediately**.

Start by helping us create a clean, working MVP.

The first version should:

1. Load the NY hospital CSV.
2. Clean the relevant columns.
3. Calculate acute-care occupancy.
4. Calculate ICU occupancy.
5. Create a Streamlit app.
6. Allow selection of a hospital.
7. Display:
   - hospital name
   - region
   - network
   - acute staffed beds
   - acute occupied beds
   - acute available beds
   - acute occupancy %
   - ICU staffed beds
   - ICU occupied beds
   - ICU available beds
   - ICU occupancy %
8. Add a simple statewide overview.
9. Add a table ranking hospitals by current occupancy.
10. Keep the code readable for beginner/intermediate Python students.

Before adding advanced features, make sure this basic version runs successfully with:

```bash
streamlit run app.py
```

Please explain the file structure and major parts of the code clearly so that university students can understand and defend every part during Q&A.

Do not over-engineer the project.