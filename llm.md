# LLM Feature Plan

Not built yet — this is a design doc, written the same way `Instructions.md`
guided the original app build. Two features, both using the class-taught
pattern (a single structured-output call, no agents/tools/multi-turn), but
in opposite directions: one parses messy human input into structure, the
other generates structured output from clean data. Pairing them gives the
project two genuinely different LLM patterns instead of the same trick twice.

---

## What we deliberately did NOT design: an "investment helper"

Early idea was an LLM feature to help decide where to build a new hospital,
using cost/budget data. Rejected — not because it's a bad question, but
because the dataset has zero support for it: no land cost, no construction
cost, no population/demand projections, no NYC capital budget data. An LLM
asked that question would just invent plausible-sounding numbers, which is
exactly the overclaiming `Instructions.md` has been careful to avoid
everywhere else in this app (the "must not claim X needs 20 more nurses"
guardrail). If this is ever worth revisiting, it needs a real new dataset
first — it is not an LLM-prompting problem.

---

## Feature 1 (primary): Natural-language search box

**The idea:** a search box on the Hospitals tab where the user types intent
in plain English — *"critical hospitals in Brooklyn with the fewest ICU
beds free"* — and the LLM's only job is turning that into a strict,
structured filter object. Python then executes the filter against the
dataframe exactly like the existing dropdowns do. The LLM never touches
hospital data, never ranks anything, never writes a recommendation — it's
a translator from words to a query shape.

**Why it's worth building:** it's genuinely new capability, not a
narration of numbers already on screen. Querying by intent instead of
four dropdowns is a real usability upgrade, and it's realistic
"structured input parsing," a different skill than the brief below.

### Flow

```
User types: "critical hospitals in Brooklyn with the fewest ICU beds free"
        |
        v
LLM call with response_format=DashboardQuery (Pydantic model)
        |
        v
DashboardQuery(region="Brooklyn", status="Critical", sort_metric="icu_beds_free", sort_order="asc")
        |
        v
Existing filter_bar() / dataframe filtering logic applies it (same code path as the dropdowns)
        |
        v
Hospitals table updates
```

### Sketch schema

```python
from pydantic import BaseModel
from typing import Literal

class DashboardQuery(BaseModel):
    region: str | None = None          # must match a value in df["Region"], or None
    hospital_type: str | None = None   # must match df["Hospital Type"], or None
    status: Literal["Available", "High occupancy", "Critical"] | None = None
    sort_metric: Literal["Acute Occupancy %", "ICU Occupancy %", "Beds free", "ICU free"] = "Acute Occupancy %"
    sort_order: Literal["asc", "desc"] = "desc"
```

### Guardrails to build in

- The LLM only ever returns a `DashboardQuery` — never hospital names, never
  numbers, never prose. Structured output enforces this at the API level.
- Validate `region` / `hospital_type` against the actual unique values in
  `df` after the LLM call returns, before filtering — an LLM could still
  return a region string that's close-but-not-exact ("Brooklyn" vs. the
  app's real label "New York City" for that borough's DOH region). Match
  loosely (case-insensitive, alias table reusing `NYC_BOROUGH_MAP`) or fall
  back to no filter on that field with a visible note, never silently drop
  the whole query.
- If the LLM API errors or times out, the search box should no-op back to
  the normal dropdown filters — the app must stay fully usable without it,
  same principle already stated in `Instructions.md` section 18 for the
  brief feature.
- Show the parsed filter back to the user ("Showing: Critical hospitals in
  Brooklyn, sorted by ICU beds free") so a wrong parse is obvious and
  correctable, not silently trusted.

---

## Feature 2 (secondary, lower priority): AI Management Brief

Already spec'd in `Instructions.md` sections 16–18 from the original
build. Lower priority than the search box because the numbers it narrates
(occupancy, region avg, state avg, trend) are already visible as KPI cards
on Hospital Detail — it saves one mental-synthesis step, not new
capability. Worth keeping mainly as a second, opposite-direction demo of
structured LLM I/O (generating structured output from clean data, instead
of parsing messy input into structure).

### Sketch schema

```python
class CapacityBrief(BaseModel):
    headline: str
    explanation: str
    recommendation: str
    limitation: str
```

### Flow

```
Python already computed: occupancy, region avg, state avg, 7-day trend
        |
        v
Small JSON packet sent to the LLM (numbers only, nothing else)
        |
        v
LLM call with response_format=CapacityBrief
        |
        v
CapacityBrief rendered as four short blocks on Hospital Detail, behind a "Generate AI Brief" button
```

### Guardrails to build in

- The LLM only ever sees the small computed packet, never the raw
  dataframe — it cannot invent a statistic that wasn't handed to it.
- `limitation` field is not optional — every brief must state what the
  data can't tell you (matches the disclosure already used throughout the
  app, e.g. `OVER_100_NOTE`).
- Same "app works without it" rule as Feature 1 — a failed API call
  degrades to hiding the button/section, not breaking the page.

---

## Open items before building either

- Which LLM SDK the class is using (OpenAI `client.responses.create()` +
  `response_format`, or the Pydantic-native `.parse()` variant) — both
  fit this design, pick based on what's taught.
- API key handling — needs Streamlit secrets (`st.secrets`), not a
  hardcoded key or a committed `.env` file.
- Whether the search box lives only on Hospitals, or also replaces/
  supplements the Overview filter bar.
