---
name: project-review-confirm-gate
description: Personal fork (untoasted-glamor). The review gate's two sub-modes (timed countdown, mandatory confirm via Config.review_requires_confirm), where each is decided, and the AWAITING_USER_ID resolve() path into the gate.
metadata:
  type: project
  scope: personal-fork
---

The pre-rip review gate parks an identified disc in `awaiting_review` when
`Config.hold_for_review` is on. Two sub-modes coexist; never replace one with
the other:

- **Timed** (`review_requires_confirm = false`, upstream's design): the ripper
  auto-starts the rip `manual_wait_seconds` after `Job.wait_start_time`, unless
  globally paused (`ripping_paused`) or paused per job (`Job.manual_pause`,
  `POST /api/jobs/{id}/review-pause`).
- **Mandatory** (`review_requires_confirm = true`, migration
  `0045_review_requires_confirm`): no countdown; only Start
  (`POST /api/jobs/{id}/rip-start-review`) proceeds.

`manual_wait_seconds` stays `int NOT NULL` and PATCH refuses `null` (upstream
D-096). Mandatory mode is the separate boolean so the fork never fights that.

**Why:** the owner wants an always-click-to-rip mode without losing the timed
auto-start; on 2026-10-18 they chose a new boolean over a nullable countdown so
upstream syncs stay clean. The live DB was in mandatory mode before the sync,
so `review_requires_confirm` is set true there.

**Where the mode is decided (keep these in sync):**
- Ripper: `_review_countdown_expired` in
  `services/ripper/arm_ripper/job_controller.py` returns False while
  `RipperConfigView.review_requires_confirm` is on, next to the
  `ripping_paused` / `manual_pause` checks. Re-read every poll, so a mode change
  applies to a disc already held. The 30-minute wait ceiling still drops the
  drive lock; Start flips the job to `ripping` and `maybe_reacquire_current_job`
  picks it up.
- Backend: `GET /api/ripper/drives/{id}/held-job` reports a held disc as
  `paused` in this mode, so a ripper restart re-parks it. Turning the mode off
  in `PATCH /api/config` gives held discs a fresh countdown, like un-pause.
- UI (ui-neu): Settings > Ripping > Review gate is `ReviewWaitField` (widget
  `review_wait`, owns `manual_wait_seconds`). `ReviewWaitStatus` shows
  "Waiting for your confirmation" or the countdown on both the dashboard review
  card (`DiscReviewWidget`) and the job page (`ReviewGatePanel`); both read the
  mode from the dashboard store.

**Two entry paths into `awaiting_review`:** a genuine auto-identify hit
(`routers/ripper.py` identify) and a manual resolve of an `awaiting_user_id`
disc (`routers/jobs.py` `resolve()`), which parks it with a countdown anchor
and review tracks from the stored `scan_result` via `persist_review_tracks`.
The ripper's `_IDENTIFY_WAIT` treats `awaiting_review` as success so the
pipeline falls through into the review wait. A `ripped_awaiting_identify`
placeholder never enters the gate (its rip is done). The dashboard's Start rip
follows the resolve with `rip-start-review` whenever the resolve lands the disc
in `awaiting_review`.

**How to apply:** when touching identify, resolve, rip-start or the countdown,
check both entry paths and both sub-modes. A third park reason exists:
`on_previously_ripped = hold` parks with `wait_start_time = null` and waits for
Rip again regardless of the sub-mode.

**Open:** upstream's sidebar Pause/Resume (`setRippingEnabled` in ui-neu
`lib/api/dashboard.ts`) deliberately sets `hold_for_review` together with
`ripping_paused` (upstream `5dd6442c`: Pause means "park every disc for
review" instead of refusing it). Resume therefore turns `hold_for_review` off
and discs rip unreviewed until it is turned back on; `review_requires_confirm`
itself survives. Not changed in the fork; needs an owner decision.
