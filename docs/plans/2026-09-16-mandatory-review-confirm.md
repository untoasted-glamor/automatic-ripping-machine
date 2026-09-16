# Mandatory review-confirm gate (alongside existing timed auto-start)

Tracking doc for adding a second review-gate mode. Today `hold_for_review` +
`manual_wait_seconds` park a genuinely auto-identified disc in
`AWAITING_REVIEW` and self-start the rip after a countdown
(`services/ripper/arm_ripper/job_controller.py` `_review_countdown_expired`).
That mechanism is real but has two gaps this doc closes:

1. **No mandatory (non-expiring) mode.** There's no way to say "always wait
   for an explicit human click, never auto-start."
2. **The gate doesn't cover the failed-auto-identify path.** A disc that
   lands in `AWAITING_USER_ID` and gets manually resolved via `/resolve`
   jumps straight to `IDENTIFIED` and rips immediately — it never passes
   through the review gate even when `hold_for_review` is on.
3. **The UI has zero wiring for any of this.** No Start button, no
   `hold_for_review`/`manual_wait_seconds` fields in Config, no
   `AWAITING_REVIEW` handling in JobDetail, no search-and-select UI — only a
   blind manual-override form (`IdentifyDiscDialog.vue`).

Target flow (per owner): insert disc → attempt automatic metadata extraction
→ if extracted, **confirm with user** → whether extracted or not, expose a
manual search box + retry-search button + manual override fields → confirm
metadata and proceed to rip. Two configured sub-modes coexist:

- **Timed** (`hold_for_review=true`, `manual_wait_seconds=<N>`): today's
  behavior — auto-starts after N seconds unless paused.
- **Mandatory** (`hold_for_review=true`, `manual_wait_seconds=null`): never
  auto-starts; only an explicit "Confirm & start rip" click proceeds.
- **Off** (`hold_for_review=false`): today's no-gate behavior, unchanged.

Also fixes the unrelated TMDB-auth bug that kicked off this session (v4
bearer token vs v3 key — already resolved by the user reconfiguring their
key; no code change needed there) and leaves the volume-label
disc/CS-token search-noise issue (`ACCA CS D2`) for a future pass — the
manual search box shipped here is the mitigation for that in the meantime.

Mark items `[x]` as they land. Each item names its file(s) so a fresh
agent/subagent can pick up a single unchecked item without re-deriving
context from this whole doc.

---

## Phase 1 — Data model + schemas

- [x] `packages/arm_common/arm_common/models/config.py`: change
      `manual_wait_seconds` from `int` (`nullable=False, server_default="60"`)
      to `int | None` (`Column(Integer, nullable=True)`, keep
      `server_default="60"` so existing rows are unaffected — only new
      explicit `null` writes opt into mandatory mode). Update the comment
      above `hold_for_review`/`manual_wait_seconds` to describe both
      sub-modes.
- [x] New Alembic migration `services/backend/migrations/versions/0029_config_wait_nullable.py`
      (revision ids are capped at 32 chars by `alembic_version.version_num`):
      `op.alter_column('config', 'manual_wait_seconds', nullable=True)` /
      downgrade sets it back to `nullable=False` (backfill any existing NULL
      to 60 first in the downgrade, since 0001-era rows can't have NULL
      today but be defensive).
- [x] `packages/arm_common/arm_common/schemas/auth.py`: `ConfigView.manual_wait_seconds: int | None`. `ConfigUpdateRequest.manual_wait_seconds` stays `int | None = None` — the "unset" sentinel is `model_dump(exclude_unset=True)` in `routers/config.py:113`, so an explicit `null` in the PATCH body IS distinguishable from omitting the key and will persist as NULL. Confirm this with a test (Phase 6).
- [x] `packages/arm_common/arm_common/schemas/ripper.py` `RipperConfigView.manual_wait_seconds`: `int` → `int | None`, default stays `60` for old-backend compat.
- [x] `packages/arm_common/arm_common/config_metadata.py`: update the `manual_wait_seconds` `ConfigFieldMeta.help` text to mention "leave blank to require a manual confirm click instead of auto-starting", and `hold_for_review`'s help text to mention both sub-modes exist. `type` stays `"int"` — the metadata registry doesn't need a new type, the UI just needs to allow clearing the field (Phase 5).

## Phase 2 — Backend: close the AWAITING_USER_ID gate gap

- [x] `services/backend/arm_backend/routers/jobs.py` `resolve()`: when
      `job.status in _RESOLVABLE_STATUSES_PROMOTE`, fetch `Config` and
      promote to `AWAITING_REVIEW` (+ stamp `wait_start_time` + persist
      review tracks) when `cfg.hold_for_review` and a `scan_result` is on
      the job; otherwise promote to `IDENTIFIED` as before.
- [x] **Track-persistence gap** closed: moved `_persist_review_tracks` out
      of `routers/ripper.py` into `arm_backend/track_selection.py` as public
      `persist_review_tracks()` (alongside `DEFAULT_RIP_PRESET_BY_DISC_TYPE`,
      also promoted to public), so both `routers/ripper.py` and
      `routers/jobs.py` import the same implementation instead of
      duplicating it. `resolve()` reconstructs `ScanResult.model_validate(new_metadata["scan_result"])`
      — confirmed always present (`routers/ripper.py` identify handler
      unconditionally writes it into `metadata_json` regardless of hit/miss).
      Falls back to `IDENTIFIED` if `scan_result` is somehow missing.
- [x] Emits the same `identify.resolved` WS event as today (unchanged) —
      the ripper-side fix in Phase 3 makes it interpret an `AWAITING_REVIEW`
      result correctly instead of treating it as "abandoned".
- [x] Updated the comment block above `_RESOLVABLE_STATUSES_PROMOTE`/`_PRESERVE`
      to describe the new hold_for_review-gated branch.

## Phase 3 — Ripper: wait-loop changes

- [x] `services/ripper/arm_ripper/job_controller.py` `_IDENTIFY_WAIT`:
      added `JobStatus.AWAITING_REVIEW` to `success`.
- [x] `_review_countdown_expired`: treats `cfg.manual_wait_seconds is None`
      as "never expires".
- [x] Updated the `_WaitSpec.timed` docstring and the `_REVIEW_WAIT` comment
      to note the mandatory-mode degenerate case.
- [x] Confirmed `RESOLUTION_WAIT_TIMEOUT_SECONDS` (30 min) applies the same
      way to mandatory mode as it already does to `awaiting_user_id` —
      accepted existing limitation, not a new gap. Log message already uses
      `spec.label` so it reads correctly for the review-gate case.

## Phase 4 — Backend: confirm PATCH semantics for nullable field

- [x] `services/backend/arm_backend/routers/config.py` PATCH handler
      (`fields = req.model_dump(exclude_unset=True)` + `setattr(cfg, key, value)`
      loop) confirmed to apply an explicit `None` correctly — no fix needed.
- [x] Removed the `int(...) if ... is not None else 60` coercion in both
      `routers/ripper.py::get_ripper_config` and `routers/config.py::_to_view`
      — `manual_wait_seconds` now passes through as `int | None` end to end.

## Phase 5 — UI

- [x] `services/ui/src/views/configFormFields.ts`: add `'hold_for_review'`
      and `'manual_wait_seconds'` to `CONFIG_FORM_KEYS` (also update
      `services/ui/src/__tests__/config-fields-guard.spec.ts` expectations).
- [x] `services/ui/src/views/Config.vue`: add a "Hold discs for review"
      checkbox (`form.hold_for_review`) and, shown only when checked, a
      countdown-seconds number input bound to `form.manual_wait_seconds`
      plus a "Require manual confirm (no auto-start)" checkbox that, when
      checked, sets `form.manual_wait_seconds = null` and disables the
      number input (and restores a sane default, e.g. `60`, when
      unchecked). Mirror the existing checkbox/field markup style already
      in this file.
- [x] New/extended component for the review + search + override UI. Extend
      `services/ui/src/components/IdentifyDiscDialog.vue` in place (it
      already branches on CD vs video and on edit-mode vs first-identify;
      add a third dimension for "review gate active") rather than forking a
      parallel component:
      - Add a search section (title text input + movie/tv radio, "Search"
        button) that calls `GET /api/metadata/search?title=...&type=...`
        (existing endpoint, `services/backend/arm_backend/routers/metadata.py:137`,
        already typed in the OpenAPI schema / TS client) and renders
        returned `MetadataCandidate[]` (poster thumbnail via existing
        `Poster.vue`, title, year). Clicking a candidate populates
        `title`/`year` (and stash `provider_id`/full payload into the
        `metadata` object sent on submit, similar to how the CD path
        builds its `metadata` object today).
      - Keep the existing manual title/year (and CD album/artist/track)
        fields as the fallback/override path — unchanged behavior, just
        now pre-fillable from a search pick.
      - "Save" always calls `jobs.resolve(...)` as today (metadata write;
        for `awaiting_review` this preserves status per
        `_RESOLVABLE_STATUSES_PRESERVE`, for `awaiting_user_id` it now
        promotes to `awaiting_review` when `hold_for_review` is on, per
        Phase 2 — same button, new backend behavior, no UI branching
        needed here).
      - Add a distinct, always-visible-when-relevant "Confirm & start rip"
        button, shown when `job.status === 'awaiting_review'`, calling
        `POST /api/jobs/{id}/rip-start-review` (new store action needed in
        `services/ui/src/stores/jobs.ts` if one doesn't already exist for
        this endpoint — check first).
      - In the **timed** sub-mode only (`job.wait_start_time` set and the
        job's effective `manual_wait_seconds` from config is non-null),
        show a simple countdown/pause affordance calling
        `POST /api/jobs/{id}/review-pause?paused=<bool>` (check for an
        existing store action first). In **mandatory** mode, hide the pause
        control entirely (nothing to pause).
- [x] `services/ui/src/views/JobDetail.vue`: add `'awaiting_review'` to
      whatever status list currently gates showing the identify/review
      dialog (`IDENTIFY_OK`, line ~53-58, currently does NOT include
      `awaiting_review` as a *dialog-showing* status — confirm and fix),
      and make sure the button label logic (`identifyButtonLabel`,
      line ~59-65) has sensible copy for the review-gate case (e.g. "Review
      & confirm").
- [x] Any UI test fixtures/mocks that hardcode a `ConfigView`/`ConfigUpdateRequest`
      shape (e.g. `services/ui/src/__tests__/config.spec.ts`) need
      `hold_for_review`/`manual_wait_seconds` added.

## Phase 6 — OpenAPI + generated types

- [x] `bash devtools/regen-openapi-snapshot.sh` — refreshed
      `services/ui/openapi.snapshot.json` (`manual_wait_seconds` now
      `integer | null` in both `ConfigView` and `ConfigUpdateRequest`) and
      regenerated `services/ui/src/api/generated.ts` (gitignored, built
      from the snapshot).
- [ ] Commit the regenerated snapshot alongside the code change per
      CLAUDE.md's wire-contract rule — CI's `openapi-drift` job fails
      otherwise. (Deferred to the final commit at the end of this doc.)

## Phase 7 — Tests

Skip writing new unit tests.

- [-] Backend: `services/backend/tests/test_jobs_router.py` — new cases:
      resolving an `AWAITING_USER_ID` job with `hold_for_review=true` lands
      it in `AWAITING_REVIEW` with tracks persisted and `rip-start-review`
      pre-flight-able; with `hold_for_review=false` behavior is unchanged
      (promotes to `IDENTIFIED`).
- [-] Backend: a test for the `ConfigUpdateRequest`/PATCH nullable
      round-trip (`manual_wait_seconds: null` persists as NULL, not
      coerced) — likely in `test_config_apprise_validation.py`'s
      neighborhood or a new `test_config_manual_wait_seconds.py`.
- [x] Backend: `test_config_metadata.py` guard test — confirmed still
      passes after the `config_metadata.py` help-text edits (inert, as
      expected — part of the 1817-passed full-suite run below).
- [-] Ripper: `services/ripper/tests/` — extend whatever exercises
      `_review_countdown_expired`/`_IDENTIFY_WAIT` (search for existing
      `test_job_controller*` files) with: countdown never expires when
      `manual_wait_seconds is None`; `_IDENTIFY_WAIT` success set accepts
      `AWAITING_REVIEW` and the pipeline falls through into the
      `_REVIEW_WAIT` branch correctly (i.e. a resolve-driven promotion
      still ends up waiting for `rip-start-review`, not ripping
      immediately).
- [-] UI: `services/ui/src/__tests__/` — new/updated specs for
      `IdentifyDiscDialog.vue` (search box + candidate select + Confirm
      button visibility rules) and `Config.vue` (new fields, null-vs-number
      toggle) and the `config-fields-guard.spec.ts` key list.
- [x] Run the full suite: `uv run python -m pytest -q` → 1817 passed, 0
      failed (backend/ripper/transcode, zero-infra). UI: `npm run test --
      --run` → 77 passed, 0 failed. `npx vue-tsc --noEmit` and
      `npx eslint` clean on all touched UI files.

## Phase 8 — Docs

- [x] `docs/arch/02-job-lifecycle.md`: already stale per the research
      (predates `awaiting_review`/`ripped_awaiting_identify` entirely) —
      update the state diagram to include both, and note the two
      `AWAITING_REVIEW` sub-modes and the two entry paths (genuine
      auto-identify hit, and resolve-driven promotion from
      `awaiting_user_id`).
- [x] `.claude/memory/`: added `project_review_confirm_gate.md` (+ `MEMORY.md`
      index entry) summarizing the two-sub-mode design and the
      AWAITING_USER_ID-gap fix, per this repo's memory-in-source-control
      convention.

---

## Out of scope for this doc

- The volume-label search-noise issue (`ACCA CS D2` → TMDB no-match) —
  the manual search box in Phase 5 mitigates it for the operator, but no
  change to `_normalize_volume_label` (`services/backend/arm_backend/metadata/dispatcher.py:38`)
  is planned here.
- Any change to `RESOLUTION_WAIT_TIMEOUT_SECONDS` (30 min ripper-side
  give-up) — treated as an existing, accepted constraint shared with the
  `AWAITING_USER_ID` gate today.
