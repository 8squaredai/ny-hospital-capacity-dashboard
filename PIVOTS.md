# Project Pivots & Decision Log

A record of the points in this project where we changed direction — what we
tried first, why it didn't hold up, and what we did instead. Kept for anyone
picking this project back up (including future us) who wants the reasoning,
not just the final code.

---

## 1. Data access: API vs. downloaded CSV

**Initial approach:** connect to the NY Health data portal via its API.

**Pivot:** the API caps results at 1,000 rows, and the full dataset (a
historical export, one row per facility per reporting date) is far larger
than that. We switched to downloading the full CSV directly
(`New_York_State_Statewide_Hospital_Bed_Capacity_20260817.csv`, ~72,000 rows,
340 unique reporting dates) and loading it from disk instead. This is also
why the app never made a live API call for the core dataset — everything
loads from a committed file.

---

## 2. Tab navigation: `st.tabs()` vs. a session-state radio bar

**Initial approach:** Streamlit's built-in `st.tabs()`.

**Problem discovered:** `st.tabs()` has no memory of which tab was active.
Any widget interaction inside a tab (e.g. picking a different hospital in a
selectbox) triggers a full script rerun, and the tab strip snaps back to
index 0 — reported directly by the user as "changing a hospital jumps me
back to State Overview."

**Pivot:** replaced `st.tabs()` with a `st.radio(..., horizontal=True)` bar
whose selection is stored in `st.session_state`, styled with CSS to look
like a tab strip. Because widget state persists across reruns (unlike
`st.tabs()`'s internal state), the active tab now survives any interaction.

**Side effect this pivot caused (and fixed):** under `st.tabs()`, every
`with tab_x:` block executes on *every* script run regardless of which tab
is visible — so a function like `fmt_pct()` defined inside one tab's block
was still available in every other tab. Once tabs became an `if/elif` chain,
only the active branch executes, which silently broke `fmt_pct()` in
Network Analysis (and later, the new NYC 3D map) until we caught it and
hoisted the function to module scope.

---

## 3. Visual style: bland → color-coded by design system rules

**Initial approach:** default Streamlit `st.metric()` tiles — flagged
directly by the user as "bland" and hard to scan.

**Pivot:** rebuilt KPI tiles as custom HTML (`kpi_card` for neutral grouped
metrics with a colored top border identifying Acute vs. ICU; `kpi_badge` for
occupancy percentages, filled solid with a status color). Colors follow a
fixed rule set (not chosen ad hoc): categorical blue/orange identify
Acute/ICU groups, and a 4-step status ramp (green/yellow/orange/red) encodes
occupancy pressure at fixed thresholds (75% / 85% / 95%), reused identically
across every table, chart, and map in the app so pressure always means the
same color everywhere.

---

## 4. The geo map saga — the biggest pivot chain

This was the single most-revised feature in the project. Documenting the
full chain because each step genuinely changed the plan, not just the code.

### 4a. Reference image → reality check

**Starting point:** the user wanted a 3D map like a deck.gl/Uber-style demo
(hexbin/column bars, small multiples by location).

**Reality check before building anything:** inspected the actual CSV
columns. No address, no zip code, no latitude/longitude — only county names.
A hospital-level point map was not possible with the data on hand as-is.

### 4b. Option A vs. Option B

Two options were laid out before writing any code:
- **Option A** — geocode each hospital's address to get real coordinates,
  render individual 3D columns per hospital (closest to the reference image).
- **Option B** — skip geocoding, extrude the 5 borough polygons we already
  had (from the existing county GeoJSON) in 3D instead.

**Decision:** Option A, because it was the visual actually being asked for
and the geocoding was framed as a one-time cost.

### 4c. The "what about new hospitals" objection

**User's concern:** a static, one-time geocoded lookup file would go stale
the moment a new hospital opened and wasn't in it.

**Pivot:** designed a self-healing cache-aside pattern — a seed CSV of known
coordinates, with any hospital missing from the cache geocoded on the fly
via `geopy` + Nominatim (rate-limited), then written back to the cache so
each hospital is only ever geocoded once, automatically covering future
hospitals with no manual maintenance.

### 4d. Built it — then reverted all of it

Implemented in full: added `pydeck` + `geopy` to `requirements.txt`, wrote
`get_hospital_coords()` (the self-healing geocode cache function), a
`hex_to_rgba()` color helper, and a full 3D `ColumnLayer` map in the NYC
Boroughs tab using Nominatim-geocoded coordinates.

**Then the user asked a better question:** "what if we could get the zip
code rather, or something, idk." That prompted a search for an authoritative
source instead of live geocoding — which turned up NY State's own **"Health
Facility General Information"** dataset
(`health.data.ny.gov`, id `vn5v-hh5r`), a directory of every licensed NY
health facility that already includes address, zip code, latitude, and
longitude. Spot-checking confirmed its `Facility ID` column is the *exact
same identifier* as our dataset's `Facility PFI` (verified against 6
hospitals by name/location before trusting the join).

**Pivot:** this authoritative join is strictly better than geocoding —
exact government-sourced coordinates instead of fuzzy name-matching, no
rate limiting, one bulk file instead of ~40+ individual API calls, and
still self-healing for free (a new hospital just needs a PFI that exists in
both datasets, no re-geocoding logic required).

**Explicit instruction from the user:** "remove all the stuff from the last
try to make this work." Reverted: `geopy`/`pydeck` imports, the
`HOSPITAL_COORDS_PATH` constant, `get_hospital_coords()`, `hex_to_rgba()`,
and the entire NYC 3D map block. Kept only the `fmt_pct` scope fix from
section 2, since that was an unrelated, genuine pre-existing bug.

### 4e. Live API fetch vs. committed local file

**Considered:** fetching the facility directory live via `@st.cache_data`
on every app process start.

**Pivot:** the user asked to download it once and commit it locally instead
(`data/ny_health_facilities.csv`, 6,011 rows / ~3 MB), matching how the main
bed-capacity CSV is already handled — avoids a runtime dependency on
`health.data.ny.gov` being reachable at deploy time.

### 4f. Where the coordinates get joined: `df` vs. `history_df`

**Considered:** joining `Latitude`/`Longitude`/`Zip Code` only onto `df`
(the latest-date snapshot used by most tabs), since that's all the map
strictly needs.

**Decision:** join once at the source, inside `load_data()`, onto the full
`history_df` before it gets sliced down to the latest date. A hospital's
coordinates don't change day to day, so this makes them available for every
historical row for free (enabling a future zip-code-level trend view)
without having to redo the join later.

### 4g. Animated statewide map: Plotly `animation_frame` vs. pydeck + slider

**Considered:** Plotly's built-in `animation_frame` (native play/pause
button, smoother transitions) for a "scrub through time" statewide view.

**Decision:** stuck with `pydeck` + a manual `st.select_slider`, because
switching to Plotly's animation would have meant giving up the 3D column
look that was the actual point of this feature — the tradeoff (a normal
Streamlit rerun per slider tick instead of client-side animation) was
judged acceptable to keep the right visual.

### 4h. Weekly steps, not daily

**Decision made alongside 4g:** the date slider uses weekly steps (anchored
on the latest date, stepping backward by 7 days) rather than all 340 daily
positions, since day-to-day hospital occupancy doesn't swing enough to
justify that much granularity, and 340 slider positions would be unusable
to scrub through.

### 4i. Boroughs-only first, statewide later

**Decision:** build the NYC-boroughs-only version first (matches near-total
coordinate coverage — 58 of 59 hospitals — and the original ask), and treat
a statewide version as a deliberate follow-up rather than bundling both at
once. The statewide version was in fact added later, once the boroughs
version was confirmed working, and reused the same code via two shared
helper functions (`weekly_slider()`, `render_hospital_3d_map()`) instead of
copy-pasting the ~50-line block a second time.

---

## 5. Testing approach: automated browser vs. manual

**Initial approach:** after building the NYC 3D map, started using the
`chrome-devtools` MCP tools to navigate the running app, click through tabs,
and check the browser console for errors before declaring anything done.

**Pivot:** the user directly questioned this ("why are you accessing
chrome?") and said they'd rather check the UI themselves. Stopped using
browser automation entirely for the rest of the session; verification
shifted to (a) clean server restarts + log grepping for tracebacks, and
(b) standalone Python scripts that exercise the actual `load_data()` /
join / map-building logic outside of Streamlit, to catch data-shape bugs
before the user even opens the browser.

---

## 6. Statewide 3D map tuning: low-and-wide → tall-and-thin

**Initial values:** `radius=4000`, `elevation_scale=40` — chosen as a
starting guess for a statewide-zoom view with no way to verify visually
without a browser.

**User feedback after checking it themselves:** bars looked "low and wide."

**Pivot:** `radius=1500` (2.7x thinner), `elevation_scale=200` (5x taller).
A concrete example of a value that could only be right-sized after someone
actually looked at the rendered result — no amount of data-pipeline testing
would have caught this.

---

## 7. The data-quality audit that found a real, shipped bug

**Prompt:** the user asked, unprompted, "what did we forget for data check
and quality" — a deliberate audit pass rather than a reaction to something
broken.

**What it found:**

1. **Facility PFI numbers get reassigned over time.** 4 hospitals changed
   PFI mid-dataset (e.g. Claxton Hepburn Medical Center: `798` → `15684`).
   This explained a previously-observed gap (5 "unmatched" hospitals when
   joining coordinates) as old/retired PFI numbers rather than a data
   problem. Checked for actual double-counting from this — found exactly
   one overlapping row-pair (Claxton Hepburn, 2026-06-11), and both rows
   had blank bed counts, so no aggregate was actually inflated. Low impact,
   left as-is.

2. **Facility Name text is *not* a stable identifier — and this one was
   live and broken.** On 2025-12-24, NY DOH started appending
   `(PFI number)` to most facility names mid-history. 76 of 217 hospitals
   have more than one name string across the 340-day history: 72 are pure
   formatting changes, 4 are genuine renames (e.g. Golisano Children's
   Hospital of Buffalo → John R. Oishei Children's Hospital).

   The Historical Trends tab's "Select Hospital" dropdown matched on
   `Facility Name`, not `Facility PFI` — so it listed **290 entries for
   what are really 217 hospitals**, and selecting any of the 76 affected
   ones silently showed a **truncated trend line**. Concretely: Albany
   Medical Center South Campus only showed 234 of the possible 340 days,
   missing the first 106 with no indication anything was missing.

**Fix:** rebuilt the hospital selector to key on `Facility PFI` (stable),
using the latest snapshot (`df`) to build a clean PFI-to-display-name
mapping, then filtering `history_df` by PFI instead of by name string.
Verified directly: dropdown count went from 290 → 211 (one per
currently-reporting hospital), and Albany Medical Center South Campus's
trend now correctly spans the full 340 days.

---

## 8. Smaller bug-driven pivots along the way

- **Python 3.9 union syntax (`int | None`) crashed at runtime.** The local
  `.venv` runs Python 3.9, which doesn't support that syntax being
  evaluated eagerly. Fixed by adding `from __future__ import annotations`
  as the first import, deferring annotation evaluation — a one-line fix
  rather than rewriting every type hint in the file.
- **`priority_label()` crashed with a `TypeError` on a float bucket value.**
  A Python list assigned into a pandas column that mixes `None` and `int`
  gets silently upcast to `float64`, so a valid bucket like `3` shows up as
  `3.0` — indexing a list with a float raises. Fixed by checking `pd.isna()`
  and explicitly casting to `int()` before indexing.
- **Self-caught, not user-flagged:** an early version of
  `priority_row_style()` routed bucket values through fake percentage
  numbers just to reuse the existing `pressure_style()` function. Rewritten
  directly against the bucket/color table before it was ever shown to the
  user, since the indirection made the logic harder to follow for no
  benefit.

---

## 9. Deployment: paused, not abandoned

**Plan:** `git init` → commit → `gh repo create --public --push` → deploy
via Streamlit Community Cloud.

**Pivot:** the push failed twice with GitHub API `503` errors. Verified via
`githubstatus.com` that this was a real, ongoing GitHub outage
("Partial System Outage"), not a local misconfiguration, before reporting
back. The user said "no let's wait" — this remains explicitly paused. The
local git commit predates most of the work in this document (Network
Analysis, Priority Dashboard, NYC Boroughs, both geo maps, Historical
Trends, the KPI redesign, and the facility-coordinates join), so a fresh
commit is needed whenever the push is resumed.

---

## Quick-reference table

| # | Started with | Ended with | Why it changed |
|---|---|---|---|
| 1 | Live API (1,000-row cap) | Downloaded full CSV | API cap too low for full historical export |
| 2 | `st.tabs()` | `st.radio` + `session_state` | Tabs reset to index 0 on any widget rerun |
| 3 | Default `st.metric()` tiles | Custom colored HTML tiles | User: app looked "bland" |
| 4a | 3D map like reference image | Checked data first | No address/lat-long in source CSV |
| 4b | — | Chose Option A (geocode) over B (borough extrusion) | Matched the actual visual request |
| 4c | Static geocode cache | Self-healing cache-aside design | User: "what if a new hospital appears" |
| 4d | `geopy`/Nominatim geocoding | NY DOH official facility directory join | Authoritative, exact, no rate limits, still self-healing |
| 4e | Live-fetch facility directory | Committed local CSV | Avoid runtime dependency on external host |
| 4f | Join onto `df` only | Join onto `history_df` at the source | Free reuse for historical/trend views |
| 4g | Plotly `animation_frame` | pydeck + manual slider | Kept the 3D bar look that was the point |
| 5 | Automated browser testing (chrome-devtools) | Manual user testing + data-pipeline scripts | User preferred to check the UI themselves |
| 6 | `radius=4000`, `elevation_scale=40` | `radius=1500`, `elevation_scale=200` | Visual check: bars were "low and wide" |
| 7 | Name-matched hospital trend selector | PFI-matched selector | Name text changes mid-history truncated 76 hospitals' trend lines |
| 9 | Push to GitHub now | Paused | Live GitHub API outage, user said wait |
