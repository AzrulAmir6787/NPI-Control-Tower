# M7 physical-device acceptance — pending

Run separately on **Samsung Galaxy S22 Ultra** and **HONOR Pad X9**. Record OS, browser/version, Desktop-site setting, date, initial orientation and result. Test Chrome first, then any other browser actually used. Verify the testing build has propagated before judging results. Keep M6 as the locked fallback.

## Preparation

- [ ] Local desktop app loads with existing authentication and BAQ data.
- [ ] Desktop/laptop Main Header has no Scan QR control; original mouse/double-click Part Detail still works.
- [ ] On each mobile/tablet, use the HTTPS testing link and log in normally.
- [ ] Prepare QR A containing exactly an available Part Number; QR B for an available part outside the selected Customer/classification/process/page; QR U for a nonexistent number.
- [ ] Choose a test part with an existing Completion Date and note its exact value. Use approved test records for Save tests.

## First opening: repeat in all four device/orientation combinations

| Device | Portrait initial open | Landscape initial open |
| --- | --- | --- |
| Galaxy S22 Ultra | [ ] | [ ] |
| HONOR Pad X9 | [ ] | [ ] |

For every combination:

- [ ] Tap Scan QR in the Main Dashboard Header. A complete scanner tab appears; no empty Part Detail is preopened.
- [ ] Keep the camera pointed away from a QR first. Scanner remains usable, with Cancel/Close; Part Detail is absent.
- [ ] Grant camera permission. Confirm the **rear camera** is actually being used; microphone is not requested.
- [ ] Present QR A. Exactly one match opens the accepted M6 Part Detail **in the same scanner tab**, using the available width immediately. No rotation, zoom or refresh is needed.
- [ ] No extra Part Detail tab/window opens after asynchronous matching.
- [ ] Check picture, Qty, Engineer, full routing, plan/actual highlighting, compact header and information layout.
- [ ] Check the OS/browser camera indicator stops after success.
- [ ] Close Part Detail and repeat with QR B, then QR A again. Each opening starts at the correct size.

## Rotation, cancellation and camera lifecycle

- [ ] While scanning, perform Portrait → Landscape → Portrait and Landscape → Portrait → Landscape. Preview and controls remain usable.
- [ ] Cancel active scanning, close via the app Close button and close via browser tab controls in separate cycles. Camera stops each time.
- [ ] Cancel while permission is pending; if permission later resolves, no orphan stream or Part Detail opens.
- [ ] Background the scanner or switch apps. Camera stops; returning shows an explicit Resume Camera action.
- [ ] In Part Detail, enter unsaved Owner, Commitment Date and Remark. Perform both rotation round trips; all inputs remain intact without document reload.

## Error and fallback states

- [ ] Scan QR U: Part Number not found, no Part Detail, Scan Again works in the same scanner tab.
- [ ] Scan a URL-looking or multiline QR: treat it as literal/invalid Part Number; never follow the URL.
- [ ] Deny camera permission: understandable error, Retry and Close remain usable. Allow permission in browser settings, then Retry.
- [ ] If camera is unavailable/busy, show a recoverable error without a blank page or repeated permission loop.
- [ ] If the browser blocks Scan QR tab opening, show an error on the header; no blank placeholder window remains.
- [ ] If automatic matched-document navigation is rejected, Open Part Detail is available and works on a tap. If navigation is never rejected in normal testing, mark this case “not triggered”; the deterministic VM fallback check is separate evidence.

## Full-dataset lookup and dashboard state

- [ ] Set Customer, classification and Current Process to exclude QR B. Move to a later page and scroll the Drilldown horizontally/vertically.
- [ ] Scan QR B and open Part Detail directly. Enquiry Finder and Drilldown are not used as intermediate search/filter steps.
- [ ] Return to dashboard: same Customer/classification/process, same page, main scroll and Drilldown scroll position.
- [ ] Produce an empty Drilldown view. Scan an existing part and Save successfully; the header bridge must still work.

## Authenticated Save and data integrity

- [ ] QR-open a part with an existing Completion Date. Edit Owner/Commitment Date/Remark and Save; observe acknowledgment and verify Latest in Supabase.
- [ ] Verify Completion Date is unchanged and Previous contains the complete prior record, with correct UpdatedBy/timestamp.
- [ ] Save again without reopening; verify the second change appears immediately in Drilldown when that part is visible.
- [ ] Leave the QR-opened form open. From Drilldown (or another authorized session), change that part's Completion Date. Then Save a new Remark in the older form. The **new Completion Date must remain** and Previous must contain the correct preceding complete record.
- [ ] Confirm no Completion Date control exists in Part Detail and no popup-specific schema change is needed.

## Protected regression

- [ ] R1 main page touch scrolling; R2 only PICTURE frozen; R3/R4 header controls and pagination; R5 rotation; R6 Enquiry Finder instruction/search.
- [ ] Dashboard drag/resize/boundary and all five widgets; Customer reset rules; Excel download; authentication/BAQ processing.
- [ ] Original Drilldown double-click Part Detail, repeated Save, immediate refresh and viewport restoration.

**Acceptance gate:** record PASS/FAIL for both devices. Do not lock this candidate until first-open sizing, camera lifecycle, data integrity and protected regressions pass. Physical-device results have not been supplied with this delivery.
