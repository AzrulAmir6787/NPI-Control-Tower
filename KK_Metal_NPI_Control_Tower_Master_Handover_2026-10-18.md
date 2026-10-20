# KK METAL — NPI CONTROL TOWER

## Master Architecture, Current Progress \& Handover

**Updated:** 18 October 2026  
**Purpose:** Single handover document for continuing the NPI Control Tower in a new ChatGPT conversation. This document combines the 9 October 2026 architecture handover with the project's current app, Supabase, performance-diagnosis, and mobile-optimisation status.

\---

# 0\. READ FIRST — CURRENT SOURCE OF TRUTH

## Current app baseline

**V10 Mobile/Tablet Responsive candidate is the known-working baseline.**

Baseline ZIP filename:

`NPI\\\_Control\\\_Tower\\\_V10\\\_Mobile\\\_Tablet\\\_Responsive\\\_candidate.zip`

The user explicitly confirmed on 17 October 2026 that they deleted other `app.py` versions after V10 and are keeping V10 as the known-working version.

Previously verified ZIP contents:

* `app.py` — 265,976 bytes inside the ZIP
* `commitment\\\_store\\\_secure.py` — 4,617 bytes
* `npi\\\_drilldown\\\_component/index.html` — 1,865 bytes
* README

If the ZIP is not attached in a new conversation, first check the available files/Library for this exact filename before asking the user to upload it again. The user has previously encountered upload limits.

## Rejected patch

**Performance Patch 2 is rejected and must not be used.**

The user reported that the attempted patch lost tables, leaving no meaningful controls to click. Do not ask the user to test it further, do not treat it as a baseline, and do not build on top of it. Restart from V10.

No performance fix has yet been validated. The previous benchmark work only provided diagnostic logs and an analysis of likely bottlenecks.

## Non-negotiable guardrails

* Do not modify `calculations.py` unless the user explicitly authorises it. It is locked.
* Do not modify `data\\\_loader.py` unless explicitly authorised. It is working and locked.
* Do not change Supabase SQL, schema, or database functions unless explicitly authorised. These are working.
* Preserve all existing tables, filters, buttons, exports, floating widgets, HTML/JavaScript interactions, and login/access checks.
* Do not rewrite the app wholesale to solve one issue.
* Make one controlled change at a time, based on the exact V10 source.
* Do not push to GitHub or deploy to Streamlit Community Cloud. The user will test locally and decide when to push.
* Deliver code changes as a complete ZIP when practical. Verify the generated file exists and inspect the ZIP contents before sharing it.
* Be clear whether checks were syntax-only or actual browser/device tests. Never claim a browser test was performed if it was not.
* Communicate in natural Malay mixed with common English technical terms. Keep instructions beginner-friendly, sequential, and concrete.

\---

# 1\. PROJECT PURPOSE AND MANAGEMENT PHILOSOPHY

The KK Metal NPI Control Tower is a Streamlit management dashboard for monitoring New Product Introduction (NPI) progress using Epicor Web BAQ data.

Its purpose is to expose operational pain, identify delayed parts and bottleneck processes, and convert those problems into owned corrective actions with commitments and completion dates.

> \\\*\\\*SHOW THE PAIN → THEN OFFER THE MEDICINE.\\\*\\\*

NPI is the primary identity of the application. Production is one branch of NPI, alongside Engineering and Process readiness.

The management workflow is:

1. Upload or refresh the latest Virgin Epicor Web BAQ.
2. Calculate the status of each part and resolve duplicate occurrences.
3. Display executive KPIs, status distribution, aging, and hotspots.
4. Filter by customer.
5. Select a classification or hotspot to drill into affected parts.
6. Review part picture, process, next process, delay, aging, priority, and other details.
7. Capture and maintain commitments.
8. Monitor overdue, due-today, upcoming, and missing commitments.
9. Track completion.

Management should be able to answer:

* Which parts are delayed?
* Which customer is affected?
* Which operation/process is the hotspot?
* Who owns the problem?
* What action will be taken?
* What support is required?
* When is it committed to be settled?
* Has it been completed?

\---

# 2\. PROJECT STRUCTURE AND RESPONSIBILITIES

Expected project files include:

```text
NPI-Control-Tower/
├── app.py
├── data\\\_loader.py
├── calculations.py
├── actions.py                 # legacy management-action module/context
├── commitment\\\_store\\\_secure.py # current Supabase commitment backend
├── npi\\\_drilldown\\\_component/
│   └── index.html
├── requirements.txt
└── .streamlit/
    └── secrets.toml           # local only; never commit secrets
```

The exact files in V10 take precedence. Do not introduce, rename, or remove modules without inspecting imports and actual usage.

## `app.py` — UI/controller

Responsibilities include Streamlit UI, BAQ upload/refresh flow, customer and classification selection, Executive Status, aging buckets, Production Hotspot, Drilldown, Part Detail, Enquiry Finder, commitment interactions, Excel export, and UI state.

`app.py` should orchestrate the application and call the existing calculation/data/backend modules rather than duplicate business rules.

## `data\\\_loader.py` — locked data ingestion

* Reads supported BAQ file formats.
* Uses Excel row 5 as the header row (`header=4` in pandas).
* Extracts embedded/floating Excel pictures with `openpyxl`.
* Associates pictures to the full part identity from Column E (`Subpart Part Num`), not worksheet row number.
* Existing functions include `get\\\_file\\\_type()`, `get\\\_sheet\\\_names()`, `get\\\_file\\\_bytes()`, `load\\\_sheet()`, `extract\\\_embedded\\\_pictures()`, `load\\\_sheet\\\_with\\\_pictures()`, `get\\\_picture\\\_for\\\_part()`, and `get\\\_picture\\\_summary()`.
* Do not change this module without explicit permission.

## `calculations.py` — STRICTLY LOCKED

This is the authoritative calculation engine. It contains existing rules for customer extraction, operations, delay calculation, NPI classification, duplicate resolution, unique-part dataset, aging, production hotspots, and summaries.

Existing functions include:

* `extract\\\_customer()`
* `determine\\\_current\\\_operation()`
* `determine\\\_next\\\_operation()`
* `calculate\\\_delay()`
* `build\\\_calculation\\\_layer()`
* `resolve\\\_unique\\\_parts()`
* `build\\\_unique\\\_part\\\_dataset()`
* `add\\\_aging\\\_fields()`
* `build\\\_production\\\_hotspot()`

Do not redesign or simplify the calculation engine as part of UI, mobile, or performance work.

## `commitment\\\_store\\\_secure.py` — current secure persistence layer

This is the current Supabase commitment backend. It enforces verified user identity and approval status before reading/updating commitment data.

Do not weaken its security checks to improve performance.

## `actions.py` — legacy/earlier management action design

The 9 October handover described an `actions.py` schema with fields such as `ACTION\\\_ID`, `PART\\\_NUMBER`, `OWNER`, `ACTION`, `SUPPORT\\\_REQUIRED`, `COMMITMENT\\\_DATE`, `ACTION\\\_STATUS`, `ACTUAL\\\_COMPLETION`, `REMARKS`, and `PRIORITY`. At that time, persistence was not yet implemented.

**Current status supersedes that earlier status:** commitment persistence has since been implemented with Supabase. Do not assume the older `actions.py` local DataFrame store is the active durable store. Inspect V10's actual imports and workflow before changing anything.

\---

# 3\. AUTHORITATIVE BAQ DATA RULES

## Virgin Epicor Web BAQ

* One BAQ contains all customers.
* Header row is Excel row 5.
* BAQ schema has historically been consistent with the A:CI layout.
* Current control date is derived by the live Python application. Do not reintroduce dependency on the old VBA `JobStat3v2!JB2` cell.
* Do not block BAQ refresh because `JB2` is blank.

## Part identity and duplicate resolution

* **Stable key: `Subpart Part Num` alone.**
* Do not add Customer or PO Number to the commitment key.
* Customer code is derived from the prefix before the first hyphen.
* Preserve the rest of the full part number, including subsequent hyphens.
* Calculate delay for each occurrence before resolving duplicates.
* For duplicate full part numbers, retain the occurrence with the lowest numeric `CALC\\\_DELAY\\\_DAYS`; ties retain the earliest original row.
* The existing unique-part validation/selection logic is the source of truth.
* Do not use alphabetical order, arbitrary `drop\\\_duplicates()`, or a synthetic `Customer + Part Number` key.
* Do not hard-code customer `2000` out of the dataset; it represents an internal KK part/project and is to be left alone.

Examples:

* `ABB-1TGB110025P0610` → customer prefix `ABB`
* `ASM-1330-060-01-000-02-02` → customer prefix `ASM`
* `KBER-1396426-000-01` → customer prefix `KBER`
* `HP-HPM-3FW27-60020-01` → customer prefix `HP`

## Picture mapping

Pictures are embedded/floating Excel objects, not necessarily cell values or image URLs. The data loader extracts them and maps them by full part number in Column E. UI changes must not alter this mapping or the duplicate-resolution rule.

## Important columns

* Column E: `Subpart Part Num`, full authoritative part identity.
* Column F: picture location in the BAQ layout.
* `Order Date`: order/aging reference.
* `JobNum/Asm`: job/assembly identifier.
* `Step 1` through `Step 20`: process routing steps.
* `Job Oper 10 Plan Start Date` through `Job Oper 200 Plan Start Date`.
* `Job Oper 10 Actual Start Date` through `Job Oper 200 Actual Start Date`.
* `PO - POLine`: source for displayed PO Number in the current Drilldown requirement.

## Previous validation figures

These are references from a previously tested BAQ snapshot, not constants:

* Calculation records: 1,507
* Unique full part numbers: 1,058
* Duplicate occurrences removed: 449
* Parts with duplicate occurrences: 247
* Records involved in duplicates: 524
* Unique classifications: 670 RED - PRODUCTION, 217 GREEN - COMPLETE, 171 GREEN - ON TRACK

A new BAQ may legitimately produce different totals.

\---

# 4\. BUSINESS CLASSIFICATION AND DASHBOARD RULES

The older architecture handover documented this classification precedence:

1. `RED - ENGINEERING`
2. `RED - PROCESS`
3. `GREEN - COMPLETE`
4. `RED - PRODUCTION` when delay days are greater than zero
5. `GREEN - ON TRACK`

Priority thresholds in that earlier handover:

* P1: delay greater than 90 days
* P2: 31–90 days
* P3: 8–30 days
* P4: 0–7 days or below the existing threshold

These rules are owned by `calculations.py`. Do not reinterpret them in UI code.

## Executive Status

KPIs:

* TOTAL UNIQUE
* ENGINEERING
* PROCESS
* PRODUCTION
* ON TRACK
* COMPLETE

The donut, floating legend tiles, hidden Streamlit bridge buttons, JavaScript-to-Streamlit interaction, parent-document UI controller, and drilldown jump/scroll controller are important existing behaviours. Do not remove or casually replace them.

## Aging

Stage-specific aging:

* Engineering → Order Age
* Process → Order Age
* Production → Delay Days

Buckets:

* `<30 days`
* `30–90 days`
* `3–6 months`
* `6–12 months`
* `1–2 years`
* `>2 years`

Use existing `add\\\_aging\\\_fields()` output rather than independently recalculating aging in UI code.

## Current Delay Days Bucket behaviour

The current dashboard uses a 2-column × 3-row bucket grid. Bucket counts must reflect **RED PRODUCTION only**, excluding `GREEN - ON TRACK`. A previous example showed Production 344 versus ON TRACK 45; the ON TRACK count must not inflate Production delay buckets.

## Production Hotspot

* Group unique `RED - PRODUCTION` parts by current operation.
* Use the unique-part dataset, not raw BAQ rows.
* Floating/resizable widget.
* Clicking a hotspot filters Drilldown by process.
* Customer change must clear a stale hotspot selection.

## Drilldown table

Current UI includes a modernized Drilldown and commitment editing. Earlier architecture listed operational columns such as customer, part number, picture, engineer, job number, current/next process, delay, order date, aging, and priority, plus commitment fields.

Current specific requirements:

* `PO NUMBER` is displayed immediately after `PICTURE`, sourced from `PO - POLine`.
* `Subpart Qty` is displayed after Picture and before PO Number according to the current approved Part Detail/Drilldown requirements; inspect exact V10 table order before editing.
* Excel export includes all drilldown records, not only the current 200-row page.
* Export should preserve the visible column headers/order and commitment fields, with picture representation/embedding as implemented by the current baseline.
* Commitment group should have Owner, Commitment Date, Completion Date, and Remark subheaders where that current version uses the four-field commitment layout.
* Preserve existing table sticky headers, horizontal scrolling, dynamic part-number width, and navigation behaviour.

## Part Detail

The approved Part Detail flow was corrected in an app-only change because BAQ headers differed from earlier assumptions. It maps exact headers and routes Step 1–Step 20 and paired Job Oper Plan/Actual.

The latest described Part Detail layout baseline before V10 included:

* Process panel on the right.
* Part Informations on the left, with image at the top and large Qty/Engineer.
* PO Number after Picture; Qty after Picture and before PO in the associated layout specification.
* PO/order/exwork/job/nesting/material/plating/finishing/revisions/priority fields.
* Nesting displayed as integer.
* Delay days and aging in Process panel.
* Commitment and remarks panel aligned.
* Actual dates later than plan dates highlighted red.
* First Process Plan Date added.
* Part Detail opening changed from single-click to double-click in prior work.

Treat V10 as current baseline and inspect its actual implementation before assuming every older layout item remains unchanged.

## Commitment editing

Drilldown commitment fields are intended to be double-click editable for Owner, Commitment Date, Completion Date, and Remarks. Preserve save/persistence behaviour.

\---

# 5\. STREAMLIT STATE AND INTERACTION GUARDRAILS

Streamlit reruns the script on widget interactions. Normal reruns must not reset selected customer, classification, or saved commitments.

Keep separate concepts for:

1. Source BAQ data.
2. Customer selection.
3. Classification selection.
4. Hotspot selection.
5. Persistent commitments.
6. One-time navigation/scroll events.

Earlier state keys documented:

* `selected\\\_customer`
* `selected\\\_classification`
* `unique\\\_df`
* `calc\\\_df`
* picture map keyed by full part number
* commitment store
* `drilldown\\\_jump\\\_id`
* upload/refresh state
* customer widget key `control\\\_tower\\\_customer`

Use the existing keys in V10 where present; do not create duplicate state for the same purpose.

Rules:

* Initialise defaults only when a state key is absent.
* Customer and classification are independent filters; effective Drilldown is their intersection.
* When customer actually changes, reset classification to `ALL`, clear stale hotspot state, and do not trigger unwanted auto-scroll.
* Selecting `ALL` resets classification only; it must not reset the customer.
* Consume one-time query parameters after processing; stale query parameters must not override later interactions.
* Preserve hidden legend bridge buttons and callback scope if they remain in V10.
* Keep navigation events separate from filter state.
* Do not store persistent commitments only in a temporary local DataFrame or session state.
* Avoid broad migration from `st.components.v1.html` to `st.iframe` without diagnosing each component's dependencies. The deprecation warning alone is not proof of a runtime defect.

The earlier 9 October handover documented a URL query parameter such as `npi\\\_exec`, a donut-center `ALL` action, hidden legend bridge buttons, and `target="\\\_top"` requirements. Check the current V10 code before changing this mechanism; preserve existing working interaction paths.

\---

# 6\. SUPABASE AUTHENTICATION, APPROVAL AND COMMITMENT PERSISTENCE

## Chosen architecture

* Supabase PostgreSQL for persistence.
* Supabase Auth for individual user accounts.
* User signs up with a full name.
* Email verification was turned off during setup.
* App landing flow offers Login or Sign up.
* Admin approves access in Supabase; approved users can read/update commitments.
* User previously asked to set up `user\\\_access` and backend permission checks before connecting UI save operations.

## Existing database objects

Tables created:

* `commitments`
* `commitments\\\_previous`
* `user\\\_access`

RLS is enabled on `commitments` and `commitments\\\_previous`.

The latest-plus-previous versioning approach was selected rather than full history:

* Saving creates/updates the latest record.
* The prior complete record is placed in Previous.
* Patching one field preserves other fields.
* Previous advances to the immediately prior complete version.

Previously passed tests:

* `test\\\_commitment\\\_store`
* `test\\\_commitment\\\_patch`
* bulk read
* changing Remarks preserves Owner
* Previous contains the complete prior record
* changing Owner preserves Remarks
* Previous advances to the immediately prior version
* bulk read returns saved commitments
* missing part keys are safely omitted
* test records cleaned up

## Secure read path observed in V10

The `commitment\\\_store\\\_secure.py` implementation:

* `\\\_get\\\_client(user\\\_id)` requires a non-empty user identity.
* Reads Supabase configuration from `st.secrets\\\["supabase"]`.
* Creates a client using the configured server secret key.
* Queries `user\\\_access` for the user's `user\\\_id` and `status`.
* Requires `status == "APPROVED"`; otherwise raises `PermissionError`.
* `get\\\_commitments(part\\\_keys, user\\\_id=...)` normalises/deduplicates keys, then queries `commitments` in chunks of 200.

Security requirements:

* Do not remove approval verification.
* Do not expose service-role/secret keys to the browser.
* Do not commit `.streamlit/secrets.toml`.
* Do not assume every signed-up user is approved.
* Admin approval is a one-time access approval, not an approval required at every login. The app may check approval status on protected operations, but users should not need to be re-approved on every login unless the policy is explicitly changed.

The user previously confirmed local checks:

* Supabase client OK
* Auth `sign\\\_up`: True
* Auth `sign\\\_in\\\_with\\\_password`: True
* Auth `sign\\\_out`: True
* Login section and Supabase section configured
* URL and key configured
* Supabase connection OK
* commitments accessible
* SQL function `handle\\\_new\\\_npi\\\_user` was visible as `FUNCTION, DEFINER`
* email confirmation setting was turned OFF during testing
* a `user\\\_access` row was visible after the relevant SQL/setup.

These are historical checks; re-test only if a change makes it necessary.

\---

# 7\. MOBILE AND TABLET OPTIMISATION — CURRENT WORKSTREAM

Mobile optimisation is a separate workstream from performance diagnosis. Do not combine both into a single large patch.

## Devices and observations

* Samsung S22 Ultra, Chrome Android: BAQ uploads of 5–20 MB failed with `AxiosError: Network Error`.
* Samsung S22 Ultra, Firefox Android: BAQ upload succeeded.
* HONOR Pad X9, Chrome Android: BAQ upload succeeded, but dragging/resizing floating widgets was difficult.
* The user intends to use the HONOR Pad X9 at home for local testing because the Samsung phone has a broken screen.

## Outstanding mobile issues

1. **Header + Enquiry Finder**

   * Customer label and selector should be side-by-side on the second row.
   * Title/Control Date line was corrected in prior work.
   * Enquiry Finder still overlapped Drilldown in previous attempts.
   * Mobile Patch 1.2 and Patch 1.3 did not produce a visible fix. Do not repeat them blindly.
   * A possible cause is fixed iframe height (553 px) versus internally positioned/floating content on mobile. This is a hypothesis; inspect V10's HTML/CSS/JS before acting.
2. **Executive Status**

   * Check layout/readability on narrow screens.
3. **Drilldown**

   * Ensure the table remains visible and usable.
   * Preserve horizontal scrolling and important columns.
   * Do not remove the table to make the layout look simpler.
4. **Delay Commitment**

   * Improve tile sizing and touch usability.
   * Preserve commitment edit/save/persistence.
5. **Part Detail**

   * Check popup size and layout on mobile.
6. **Portrait/landscape/tablet**

   * Test both orientations where possible.
7. **QR scanner — last**

   * Work was paused until mobile layout stabilises.
   * V9 showed an empty Part Detail with “Waiting for QR scan...” before the scanner appeared; camera permissions opened but QR capture did not work.
   * Do not resume QR work unless the user asks. Fix core mobile UI first.

## Required order of work

1. Header + Enquiry Finder.
2. Executive Status.
3. Drilldown.
4. Delay Commitment.
5. Part Detail.
6. Portrait/landscape/tablet verification.
7. QR scanner last.

## Local tablet testing over Wi-Fi

From the project folder:

```powershell
py -m streamlit run app.py --server.address 0.0.0.0 --server.port 8501
```

Then:

1. Run `ipconfig`.
2. Find the laptop's Wi-Fi IPv4 address.
3. Ensure laptop and tablet are on the same Wi-Fi.
4. Open `http://<laptop-ip>:8501` in tablet Chrome.
5. If blocked, check Windows Firewall for Python/Streamlit on a Private network and whether the Wi-Fi uses guest/AP isolation.

Do not claim mobile issues are fixed until the user has tested the actual device.

\---

# 8\. PERFORMANCE INVESTIGATION — CURRENT STATUS

## User's benchmark logs

The user ran a temporary Performance Benchmark 1 build and shared these results:

```text
\\\[NPI PERF] click='Delay Bucket: <30 DAYS' | pre\\\_drilldown\\\_ui\\\_ms=134.1
\\\[NPI PERF] click='Delay Bucket: <30 DAYS' | filter\\\_ms=22.3 | filtered\\\_rows=432 | unique\\\_rows=1,626
\\\[NPI PERF] click='Delay Bucket: <30 DAYS' | supabase\\\_read\\\_ms=4937.4 | requested\\\_part\\\_keys=432 | commitments\\\_returned=1

\\\[NPI PERF] click='Delay Bucket: 30–90 DAYS' | pre\\\_drilldown\\\_ui\\\_ms=169.9
\\\[NPI PERF] click='Delay Bucket: 30–90 DAYS' | filter\\\_ms=17.9 | filtered\\\_rows=338 | unique\\\_rows=1,626
\\\[NPI PERF] click='Delay Bucket: 30–90 DAYS' | supabase\\\_read\\\_ms=2506.1 | requested\\\_part\\\_keys=338 | commitments\\\_returned=3
\\\[NPI PERF] click='Delay Bucket: 30–90 DAYS' | drilldown\\\_server\\\_render\\\_ms=7309.2 | click\\\_to\\\_drilldown\\\_server\\\_done\\\_ms=9003.7

\\\[NPI PERF] click='Delay Bucket: 1–2 YEARS' | pre\\\_drilldown\\\_ui\\\_ms=131.9
\\\[NPI PERF] click='Delay Bucket: 1–2 YEARS' | filter\\\_ms=26.3 | filtered\\\_rows=10 | unique\\\_rows=1,626
\\\[NPI PERF] click='Delay Bucket: <30 DAYS' | pre\\\_drilldown\\\_ui\\\_ms=159.7
\\\[NPI PERF] click='Delay Bucket: <30 DAYS' | filter\\\_ms=32.5 | filtered\\\_rows=432 | unique\\\_rows=1,626
\\\[NPI PERF] click='Delay Bucket: <30 DAYS' | supabase\\\_read\\\_ms=2539.9 | requested\\\_part\\\_keys=432 | commitments\\\_returned=1
\\\[NPI PERF] click='Delay Bucket: 30–90 DAYS' | pre\\\_drilldown\\\_ui\\\_ms=154.5
\\\[NPI PERF] click='Delay Bucket: 30–90 DAYS' | filter\\\_ms=33.2 | filtered\\\_rows=338 | unique\\\_rows=1,626
\\\[NPI PERF] click='Delay Bucket: 30–90 DAYS' | supabase\\\_read\\\_ms=3392.1 | requested\\\_part\\\_keys=338 | commitments\\\_returned=3
\\\[NPI PERF] click='Delay Bucket: 30–90 DAYS' | drilldown\\\_server\\\_render\\\_ms=8346.5 | click\\\_to\\\_drilldown\\\_server\\\_done\\\_ms=9608.4
```

## Interpretation

* Filtering takes roughly 18–33 ms for 10–432 rows; it is not the main bottleneck.
* Supabase reads take roughly 2.0–4.9 seconds.
* Drilldown server render takes roughly 7.3–8.3 seconds.
* Click-to-server-done is roughly 9.0–9.6 seconds in the observed clicks.
* The drilldown render timer includes the Supabase read that occurs inside that path. Do not add these times together as independent sequential durations.
* Repeated warning: `Please replace st.components.v1.html with st.iframe.` This is a deprecation warning, not proof of the performance cause. Do not replace every occurrence indiscriminately.

## Next safe performance step

Performance Patch 2 failed because tables disappeared. Do not reuse it.

If performance work resumes:

1. Start from the original V10 ZIP.
2. Inspect the exact `render\\\_drilldown` and component/table code before editing.
3. Add minimal diagnostic-only timers around existing code without changing UI or business logic.
4. Where possible, measure separately:

   * Supabase client creation and approval lookup;
   * each commitment query chunk;
   * dataframe preparation;
   * HTML/JSON payload construction;
   * existing component/table rendering.
5. Keep all tables, controls, buttons, filters, exports, and widgets intact.
6. Do not modify `calculations.py`, `data\\\_loader.py`, Supabase SQL/schema/functions, or approval checks.
7. Run `py\\\_compile`, inspect the diff, and inspect ZIP contents.
8. Deliver a complete ZIP and list exact changed files.
9. Ask the user to confirm all tables/widgets remain visible before relying on new timings.
10. Only after measuring the actual bottleneck should a separate, tightly scoped performance optimisation be considered.

Do not assume the `st.components.v1.html` deprecation warning is responsible for the delay.

\---

# 9\. DEPLOYMENT AND LOCAL DEVELOPMENT

* Project folder used by the user: `C:\\\\Users\\\\azrul\\\\Desktop\\\\NPI-Control-Tower`
* Run command:

```powershell
  py -m streamlit run app.py
  ```

* GitHub repository: private `npi-control-tower`.
* Streamlit Community Cloud app: private / restricted to specifically invited users.
* App has a login gate using `st.secrets\\\["npi\\\_login"]` and `hmac`.
* `.streamlit/secrets.toml` is local and must remain excluded by `.gitignore`.
* `requirements.txt` exists; `extra-streamlit-components` was installed previously after a `ModuleNotFoundError`.
* User is not a programmer and is learning GitHub/Supabase/VS Code. Provide one step at a time and explain exactly what to run/click.
* Never ask the user to paste secrets or publish private keys.

\---

# 10\. HISTORICAL APPROVED VERSIONS AND ROLLBACK CONTEXT

Historical baselines and candidates mentioned across previous work:

* `app\\\_delay\\\_bucket\\\_hotspot\\\_header\\\_fixed.py` — earlier fallback with compact header, Delay Days Bucket 2×3, resizable hotspot, customer-switch reset, fixed duplicate key.
* `app\\\_NPI\\\_Control\\\_Tower\\\_Hotspot\\\_Delay\\\_Bucket\\\_modernized.py` — earlier modernised candidate.
* `app\\\_NPI\\\_Control\\\_Tower\\\_Delay\\\_Commitment\\\_boundary\\\_min\\\_300.py` — earlier approved dashboard/commitment layout with 553 px default height and draggable boundary.
* `app\\\_NPI\\\_Control\\\_Tower\\\_Drilldown\\\_modernized.py` — earlier approved Drilldown styling baseline.
* `app\\\_NPI\\\_Control\\\_Tower\\\_Part\\\_Detail\\\_app\\\_only\\\_fix.py` — earlier approved Part Detail mapping fix.
* `app\\\_NPI\\\_Control\\\_Tower\\\_Part\\\_Detail\\\_layout\\\_v6.py` — earlier Part Detail layout baseline.
* `NPI\\\_Control\\\_Tower\\\_commitment\\\_patch\\\_v6.zip` — known-good fallback approved on 16 October.
* V8 Part Detail layout candidate — syntax passed, but visual layout was not explicitly confirmed by the user.
* V10 Mobile/Tablet Responsive candidate — current known-working baseline, retained by the user.
* Performance Benchmark 1 ZIP — temporary instrumented app used to obtain timings, not the baseline.
* Performance Patch 2 — rejected; tables were missing. Do not use.

Historical names are context only. **V10 takes precedence for current work.** Do not switch to an older baseline without explicit user approval.

\---

# 11\. MOBILE, PERFORMANCE, AND ARCHITECTURE ARE SEPARATE WORKSTREAMS

Keep these as separate tasks:

* **Mobile optimisation:** responsive layout and touch usability.
* **Performance investigation:** diagnostics, then targeted optimisation.
* **Master architecture:** preserve module responsibilities, stable part identity, state model, security, and durable commitments.
* **Business logic:** remains locked unless the user authorises a change.

Do not combine these into a single large patch.

\---

# 12\. IMMEDIATE NEXT STEPS FOR A NEW CHAT

1. Acknowledge that V10 is the baseline and Performance Patch 2 is rejected.
2. Locate/read the V10 ZIP before proposing any code changes.
3. Ask which one task to work on next if the user has not specified it. Do not launch multiple workstreams automatically.
4. If continuing performance work, start with diagnostics only and preserve all UI.
5. If continuing mobile work, begin with Header + Enquiry Finder, inspect the exact V10 code, and patch only that area.
6. After each patch: inspect diff, run syntax checks, verify ZIP contents, deliver ZIP, and wait for user testing.
7. Do not claim browser or device success without confirmation.
8. Keep the user in control of baseline approval and deployment.

\---

# APPENDIX A — EARLIER 9 OCTOBER HANDOVER CONTEXT

The 9 October handover documented the earlier recovery stage:

* Recovery `app.py` could load the Virgin BAQ and render the dashboard.
* Customer filtering and classification filtering had previously been confirmed in edited versions but were not yet restored to that recovery baseline at the time.
* Donut-centre ALL interaction was then still to be implemented and verified.
* Persistent management-action storage was not yet implemented at that time.
* Commitment monitoring status calculation existed in `actions.py`, with integration remaining.

**This status is historical and has been superseded by later progress.** Current work has since advanced to V10, Supabase Auth, `user\\\_access` approval checks, `commitments`/`commitments\\\_previous`, and commitment persistence tests. Use V10 and the current Supabase implementation as the source of truth for present-day code.

The older handover's critical Streamlit state lessons remain relevant:

* Do not reset classification on every rerun.
* Customer and classification are independent dimensions.
* Changing customer resets classification to `ALL`, but only when the customer actually changes.
* Donut-centre ALL resets classification only, not customer.
* Consume one-time query parameters to avoid replaying stale filters.
* Keep JavaScript-to-Streamlit bridge callbacks in scope.
* Separate persistent application state from one-time scroll/navigation events.
* Do not rebuild action records from scratch on every BAQ refresh.
* Do not make indentation-sensitive manual replacements without inspecting surrounding code.
* Treat deprecation warnings separately from actual defects.

\---

# APPENDIX B — DEFINITION OF DONE

The project should not be considered complete until:

* Virgin BAQ loads without endless spinner.
* Executive dashboard and status distribution render.
* Customer and classification filtering work correctly.
* Donut-centre ALL resets classification without resetting customer, if that interaction remains part of the current V10 UI.
* Customer changes reset classification to ALL and clear stale hotspot state.
* Filter state survives ordinary Streamlit reruns.
* Existing donut, floating legend, drilldown navigation, tables, and exports remain functional.
* Pictures map to full part identity.
* Duplicate resolution follows the locked lowest-delay rule.
* UI changes do not alter calculation results.
* Commitments survive BAQ refresh and new app sessions through Supabase.
* Approved-user access control remains enforced.
* Previous-version behaviour preserves the complete immediately preceding commitment record.
* Commitment status correctly reflects the control date.
* Overdue and due-today commitments are visible to management.
* Mobile layout is verified on actual devices for the areas being changed.
* Performance improvements are based on measured bottlenecks, not guesses.
* No failed patch replaces the user's known-working baseline.

**Final handoff rule:** Preserve V10. Inspect before editing. Change one thing. Verify the complete app. Let the user test. Only then promote a new baseline.

