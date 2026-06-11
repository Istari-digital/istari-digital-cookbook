# UAT + Perf — branch `gio/uat` handoff

Context for reviewing the work on this branch. Everything below lives under `uat/`
plus three small files in `istari-labs-helpers/`. **All changes are uncommitted** in
the working tree (nothing pushed).

## What this branch adds

1. **UAT correctness suites** (`uat/v2.py`, `uat/v3.py`) — one file per SDK version,
   one function per documented docs topic, sections in the docs pages' order; each
   suite is also working example code. Driven by `uat/runner.py`
   (`python -m uat.runner` / `uat` script).

   **Scope rule: only documented endpoints get suites.** Source of truth is the live
   docs site, re-checked when adding suites (last verified 2026-06-10): v2 = sections of
   [api_reference/client](https://docs.istaridigital.com/developers/SDK/api_reference/client/),
   v3 = sections of [v3/v3-client](https://docs.istaridigital.com/developers/SDK/v3/v3-client/).
   That check moved `v3_comments`/`v3_remotes` into `SUITES` (now documented) and deleted
   `v2_change_requests` (absent from docs and SDK). Remaining documented-but-unsuited gaps
   are listed in `uat/README.md` (v2 infosec levels, v3 workflow logs, v3 archive/restore
   as steps, v3 remote create/update).
2. **Baselining** (`--baseline`) — measures platform entity-count footprint before
   and after a run; records per-step `platform_state` and post-run drift.
3. **Perf measurement** (`uat/perf/`) — new, separate harness: run a subset of
   endpoints N times per env and record per-call latency as a time series for
   dashboards. CLI: `python -m uat.perf` (`perf` script).

## The one domain insight that drives the design

**Archive is a soft-delete.** `archive_*` only flips a flag; the row persists and is
still walked by the SpiceDB `LookupResources` permission scan on every list call.
So:
- Cleanup (which archives) **never shrinks the platform footprint**. Every run
  permanently grows it.
- That growth is what degrades list/permission latency over time (Jira CPD-598/601,
  the SpiceDB scaling issue — see `memory/project_list_resources_500.md`).
- Therefore baseline counts use `archive_status="all"` (active + archived) — an
  active-only count understates the real load. Verified this session on `perf`:
  active-only said `models=1`, total footprint was `models=4→6`.
- And `recheck_baseline` expects `final == baseline + resources_created` (cleanup
  does not reduce the count), warning on any other drift.

**Observed degradation (verified this session):** repeated runs visibly degraded
`perf` — a clean ~23s UAT run early in the day became ~6 min, then ~21 min; a
10×upload perf run had each upload at 16–33s. This is the environment, not the code.
There is **no hard-delete in the SDK surface** seen, so the only way to keep an env
clean for benchmarking is a disposable/reset tenant.

## File map

```
uat/
  common.py        TestContext, @ctx.step, baseline (take_baseline/recheck_baseline),
                   cleanup (_archive_one dispatch), write_results, build_context
  runner.py        UAT CLI: suite registry, --baseline, end summary (failures + drift)
  v2.py v3.py      correctness suites, one function per docs topic
                   (intentionally repetitive = example code)
  perf/
    operations.py  WHAT to measure — Operation registry (+ optional setup fixtures)
    measure.py     HOW — timed repeat loop → list[Sample]
    store.py       persistence — append-only JSONL, the dashboard contract
    report.py      console summary: per-(env,op) min/med/max + within-run trend
    runner.py      perf CLI + multi-env loop;  __main__.py / _perf_entry shim
  results/         gitignored; perf/ holds {env}.samples.jsonl + {env}.baseline.jsonl
istari-labs-helpers/
  istari_labs_helpers/_uat_entry.py, _perf_entry.py   console-script shims
  pyproject.toml   [project.scripts] uat + perf
```

## Perf time-series schema (dashboard contract)

Append-only, one file per env under `uat/results/perf/`:
- `{env}.samples.jsonl`: `run_id, env, operation, iteration, started_at, duration_s, status, error`
- `{env}.baseline.jsonl`: `run_id, env, taken_at, <entity counts>`

`started_at` (ISO-8601 UTC) is the x-axis; `duration_s` the latency series; baseline
stream is the footprint to plot alongside. Envs share the schema → comparison is a
filter. Multi-env in one run: `--env dev,stage` (shared `run_id`, optional). Dashboards
themselves are **not built** — JSONL is the handoff point (plotly/pandas are in the
`experiment` extras).

## Known platform issues — expected failures, not code bugs

- `list_files`, v3 `list_resources`, `list_revision_relationships` → 500 (CPD-598/601).
  In baseline these hit the 15s timeout → recorded `-1`.
- `list_modules` → 503 on dev (service not in all envs).
- `create_control_tag` → 403 on `perf` (token lacks permission; cascades to 3 dependents).
- dev is broadly degraded: `get_resource` 500 after 166s — **contradicts** the memory
  note that `get_resource` (single CheckPermission) is unaffected. Flag if reviewing dev.

## What is verified vs. not

- **Verified:** UAT runs on dev/stage/perf; baseline `archive_status="all"`; drift
  detection (caught a real orphaned model on perf); perf 10×upload run end-to-end
  (console + JSONL); offline smoke of baseline-write + missing-env skip; `py_compile`
  clean on all of `uat/`.
- **Not verified:** the `perf` console script (needs `uv sync`/reinstall; `python -m
  uat.perf` works now); perf ops other than `upload_model`/`list_*` against a live env
  (e.g. `create_resource`, `get_model` fixture path) — compiled + registry-correct but
  not each exercised live.

## Open decisions for the reviewer

1. **Exit code:** known-broken endpoints (e.g. `list_revision_relationships`) are
   recorded as hard `FAIL`, so `runner` exits 1 even when only known-platform issues
   failed. Should these become `SKIP`/xfail so exit code reflects real regressions?
2. **`control_tags` 403 on perf:** expected (skip when perm absent) or should the perf
   token get the permission?
3. Style/simplicity pass requested: target fewer lines but readable. Note the v2/v3
   suite files are deliberately repetitive (they double as copy-paste examples) — do
   **not** DRY them into a generic helper; that hides the SDK calls they exist to show.

## Running

```bash
PY=istari-labs-helpers/.venv/bin/python
$PY -m uat.runner --list
$PY -m uat.runner --env perf --baseline
$PY -m uat.perf   --env perf --ops upload_model --repeat 10
$PY -m uat.perf   --env dev,stage --repeat 20
```
Credentials: `istari-labs-helpers/.env.{env}` (dev/stage/demo/perf/prod exist).
