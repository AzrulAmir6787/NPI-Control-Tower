NPI CONTROL TOWER — STANDARD DASHBOARD — TEST CANDIDATE
19 October 2026 | Phase B | Not deployed or locked

BASELINE
NPI_Control_Tower_V10_Popup_Repeated_Save_Test_Candidate.zip
SHA256: 21cc831371e3cb66b3d7bf0a706f0f633c373de7ae9ca80db5b4a81e01a769e7
Original locked package and files remain unchanged. See BASELINE_REFERENCE.json.

INSTALL — REPLACE app.py ONLY
1. Stop Streamlit (Ctrl+C).
2. Back up your current app.py as app_V10_before_dashboard_layout.py.
3. Copy this candidate's app.py over your project app.py.
4. Keep your existing Drilldown component, support modules and secrets unchanged.
5. Run: py -m streamlit run app.py
6. Reload the browser once. Initial legacy positions are intentionally ignored.

WHAT CHANGED
Only app.py runtime code changed, entirely within render_executive_status.
- Desktop: Delay Commitment dominant top-left; native Enquiry Finder top-right;
  Executive Status bottom-left, Delay Days Bucket centre, Hotspot bottom-right.
- Tablet: full-width Delay Commitment; Executive/Hotspot side-by-side;
  Delay Bucket/Enquiry Finder side-by-side below.
- Mobile: Commitment -> Executive -> Hotspot -> Delay Bucket -> Enquiry Finder.
- One geometry model places all five widgets. Enquiry stays the native Streamlit
  form in its original DOM; the existing form and search callback are unchanged.
- Consistent cards/headers, responsive donut/legend, and wrapping Hotspot labels.
- Desktop drag/resize retained. Changes that overlap another card are rejected.
  Tablet/mobile use automatic geometry and disable drag/resize.
- Default heights favour readability: desktop about 624px, tablet 1086px,
  mobile 1780px. The category uses available dashboard content width, not device
  model or screen resolution: desktop >=1200, tablet 761–1199, mobile <=760.
  Sidebar space and browser zoom can therefore change the responsive arrangement.
- Boundary remains adjustable with minimum 300px. Smaller heights scroll the
  dashboard canvas; native Enquiry follows/clips to its visible slot. The 20px
  dashboard-to-Drilldown gap remains. Desktop max height remains 900px; narrow
  layouts allow their full natural content height rather than clipping at 900px.

PREFERENCES / RESET
- New V5 keys use a stable SHA256-derived account namespace and viewport category.
  No raw user identifier is inserted into layout DOM/storage. Authentication is
  unchanged. Preferences remain local to this browser/origin, not a shared DB.
- Legacy panel, Enquiry and boundary keys are not read or deleted.
- New preferences are written only after explicit drag/resize/boundary gestures.
  Automatic reflow and ResizeObserver sizing do not save custom layout records.
- Geometry is restored only in the matching category and at approximately the
  saved content width (within 2px). At another width safe defaults are shown;
  saved geometry is retained for returning to that width. Boundary preferences
  are independent across desktop/tablet/mobile.
- Reset Layout resets ALL FIVE widgets and boundary, across viewport categories
  for the current account only. Other-account and legacy records remain intact.
- If localStorage is blocked/unavailable, readable defaults still work; custom
  layout preferences cannot persist across reloads in that browser configuration.

LOCAL ACCEPTANCE CHECKLIST
A. Defaults and responsiveness
- Existing browser with old manual positions: refresh and check NEW defaults.
- Desktop: Commitment dominant; Enquiry top-right; Hotspot bottom-right.
- Check full headings, donut, classification labels/counts, bucket tiles and
  native Search button. Long Hotspot labels should wrap rather than be clipped.
- Repeat with sidebar open/closed, different browser widths and zoom.
- Tablet portrait/landscape and phone: confirm responsive order and readability.

B. Five-widget interactions
- Executive legend/donut filtering and reset.
- Delay Bucket tiles, independent Hotspot process selection, Commitment tiles.
- Enquiry search with several full part numbers, unmatched popup and Close.
- Enquiry native form remains usable after filtering and Streamlit reruns.

C. Boundary, custom layout and isolation
- Drag boundary down/up, including minimum 300px. Scroll through all widgets.
- Scroll while Enquiry is partially visible: no overlay over Drilldown/boundary.
- Desktop drag and resize a card, reload and confirm explicit preference restores.
  Overlapping placements should be rejected. Test native Enquiry drag/resize too.
- Change viewport category and return; confirm preferences remain independent.
- Log out account A, log in account B in SAME browser. B must see B's preferences
  or defaults, never A's layout. Set a B preference; return to A and confirm A's.
- Reset as A: all five widgets and boundary reset. B preference remains intact.

D. Existing fixes / desktop smoke test
- Freshness A/B -> C/D -> edit A must still update immediately.
- Save commitments near row 150 / page 2; Drilldown X/Y viewport must remain.
- Keep Part Detail open and Save at least four consecutive edits; verify Saved,
  Latest/Previous and Drilldown values after every Save.
- Check existing customer/process filters, upload/refresh and Excel export.

VALIDATION HERE
PASS: Python compilation and generated dashboard/native installer JS syntax.
PASS: geometry at 11 widths from 320 to 1920, no default card overlaps, correct
responsive order and no rectangles outside the modelled content width.
PASS: per-account/category preferences, explicit-only saving, safe width fallback,
minimum300 boundary, natural content heights and Reset (legacy/B data preserved).
PASS: actual native installer with simulated frame/parent/scroll geometry:
correct overlay coordinates, scroll clipping, controller cleanup, account owner
matching and mobile resize disabled.
PASS: existing 5 freshness + 6 popup-server tests; popup runtime and viewport
component checks, including four consecutive saves and duplicate/timeout handling.
PASS: AST comparisons show all other top-level code/functions unchanged, including
authentication, Drilldown and Part Detail. Native Enquiry form/callback subtree and
widget bridge wiring unchanged. All protected support/component files identical.
Full captured output: TEST_RESULTS.txt.

LIMITATIONS
The UI tests use simulated DOM geometry/events/localStorage, not actual browser
layout. They cannot establish CSS rendering, native resize handles, touch scrolling,
Streamlit frontend compatibility or performance. No live Supabase/Auth, physical
phone/tablet, real multi-account login or Excel export was exercised here.
The native overlay and boundary scrolling are the highest-priority local checks.
No Chromium executable was available for a real browser run. Local acceptance is
required before promoting this candidate to a new baseline.

RE-RUN OFFLINE TESTS (Node and Python required)
py test_dashboard.py
py test_freshness.py
py test_popup_server.py
node test_popup_runtime.cjs
node test_viewport.cjs

ROLLBACK
Stop Streamlit. Restore app_V10_before_dashboard_layout.py as app.py, or restore
app.py from the LOCKED baseline ZIP named above. Restart and reload the browser.
No other file needs restoring: the Drilldown component was not modified.
Legacy positions were retained, so the old layout controller can use them again.
New V5 preference keys are harmless to the old baseline. No SQL/auth/data changes.
Any commitments saved during testing remain in Supabase.

PACKAGE
app.py: candidate runtime change.
dashboard_layout.patch: unified diff against locked baseline.
TEST_RESULTS.txt: captured test output.
SHA256SUMS.txt: checksums for all other files in this package.
Other support files are unchanged copies for completeness; replace app.py only.
