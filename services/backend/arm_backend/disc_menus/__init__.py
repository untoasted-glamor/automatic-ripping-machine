"""Disc menus in ARM (fork only; design `docs/plans/2026-11-10-disc-menus-v5.md`).

- `preview_tasks`: the `menu_preview` transcode task, queued by the dispatcher
  sweep once a job's menu track is copied and its rip is complete.
- `proposals`: names for the job's titles, from the preview's button labels
  and what each button plays.
- `rename`: applying an accepted name to a finished encode, and the bonus
  layout by role (`Season 00/`, `trailers/`, `other/`, `extras/`).
- `views`: the job page's Disc menu panel.

Hooks in shared code stay one-line calls into these modules, so an upstream
sync conflict is easy to redo.
"""
