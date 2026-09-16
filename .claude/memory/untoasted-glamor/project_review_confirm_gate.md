---
name: project-review-confirm-gate
description: Two-sub-mode (timed/mandatory) pre-rip review-confirm gate design, and the AWAITING_USER_ID gate-bypass gap it closed.
metadata:
  type: project
---

Added a `awaiting_review` job status (`packages/arm_common/arm_common/enums.py`)
gating every identified disc behind an explicit operator action before ripping
starts, config-gated by `Config.hold_for_review` + `Config.manual_wait_seconds`
(now nullable). Two sub-modes coexist — never replace one with the other:

- **Timed** (`manual_wait_seconds = <N>`): today's original behavior, ripper
  auto-starts after an N-second countdown unless paused
  (`POST /api/jobs/{id}/review-pause?paused=true`).
- **Mandatory** (`manual_wait_seconds = null`): countdown never expires;
  only `POST /api/jobs/{id}/rip-start-review` starts the rip.

**Why:** the owner wanted a way to *always* require a human click before
ripping (no auto-start), but didn't want to lose the existing timed
auto-start behavior other users may rely on — see the design doc at
[docs/plans/2026-09-16-mandatory-review-confirm.md](../../docs/plans/2026-09-16-mandatory-review-confirm.md)
for the full phase-by-phase implementation record. Mid-design the owner
explicitly reversed an earlier "replace timed mode" answer to "keep both
flows" — treat that as the settled decision, not a transient preference.

**Gap closed:** before this change, a disc that missed auto-identify
(`awaiting_user_id`) and was manually resolved via `/resolve` jumped straight
to `ripping`, bypassing the review gate entirely even with
`hold_for_review=true`. `resolve()` in
[services/backend/arm_backend/routers/jobs.py](../../services/backend/arm_backend/routers/jobs.py)
now promotes to `awaiting_review` (not `identified`) when the gate is on and
a `scan_result` is present on the job, using the same
`persist_review_tracks()` (moved to
[services/backend/arm_backend/track_selection.py](../../services/backend/arm_backend/track_selection.py))
as the genuine auto-identify-hit path — so track selection is identical
regardless of which path got the disc there.

**How to apply:** when touching the identify/resolve/rip-start flow, remember
`awaiting_review` has two distinct entry paths (auto-identify hit, and
resolve-driven promotion from `awaiting_user_id`) that must stay in sync —
don't special-case one without checking the other. The ripper's
`_IDENTIFY_WAIT` wait-loop treats `AWAITING_REVIEW` as a success outcome (not
"abandoned") specifically so the resolve-driven path doesn't get misclassified
— see `services/ripper/arm_ripper/job_controller.py`.

Left explicitly out of scope: fixing `_normalize_volume_label`'s search-noise
misses (disc/volume-set tokens like "CS D2" causing TMDB no-results) — the
manual search box added to `IdentifyDiscDialog.vue` in this change is the
interim mitigation, not a fix to the normalizer itself.
