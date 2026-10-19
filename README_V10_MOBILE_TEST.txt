NPI CONTROL TOWER - V10 MOBILE/TABLET RESPONSIVE CANDIDATE

BASE
- Built from the V8 Part Detail baseline ZIP.
- QR Scanner is intentionally NOT included in this candidate. QR work is paused until the responsive layout is tested.

CHANGES
- Responsive Streamlit page spacing and controls for narrow screens.
- Mobile floating dashboard panels switch from overlapping/draggable placement to a vertical touch-friendly stack.
- Mobile dashboard iframe height is measured from the stacked content.
- Drilldown table wrapper explicitly supports horizontal touch scrolling.
- Part Detail popup switches to a single-column mobile layout and allows page scrolling.

UNCHANGED
- calculations.py
- data_loader.py
- actions.py
- commitment_store_secure.py
- Supabase SQL / schema
- npi_drilldown_component/index.html

IMPORTANT
- This is a candidate build. Python syntax compilation passed, but this patch has not been live-tested in a browser or on physical phones/tablets.
- Test locally first. Do not replace the production GitHub app until verified.
- Package includes app.py, commitment_store_secure.py, npi_drilldown_component/index.html and this README.
