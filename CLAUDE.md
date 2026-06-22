# UAT + Perf — branch `gio/uat` handoff

Context for reviewing the work on this branch. Everything below lives under `uat/`
plus three small files in `istari-labs-helpers/`. Committed through `cc25170`; the
perf two-phase + v3 relationship changes are staged in the working tree. **Nothing is
pushed** (the user always pushes).

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
3. **Perf measurement** (`uat/perf/`) — separate harness: run endpoints N times per
   env, record per-call latency as a time series. CLI: `python -m uat.perf` (`perf`).
   Two op styles:
   - **Independent upload testers** — `add_model`, `add_file` (raw v2 endpoints) and
     `create_resource` (v3): each timed call creates one resource. (`add_model` replaces
     the old `upload_model`, which went through the platform helper.)
   - **Relationship chain** — `create_resource` also pools its revision ids, then
     `create_revision_relationship` (v3-only) links them as a `produces` chain *after*
     the uploads: N pooled revisions → N−1 timed links, each a consecutive distinct pair
     (never self-referential, never a duplicate edge). Its rep count is **pool-derived,
     not `--repeat`**; run it without `create_resource` (or `--repeat 1`) and it skips
     with a logged reason. Reads reuse a once-only `setup` fixture.

   Mechanism: `Operation` gained a `setup` (untimed, once) and an `iterations(ctx, repeat)`
   hook (rep count from run state); `measure()` honors both.

   **Heavy uploads:** `--make-junk MB` writes `uat/data/junk_{MB}mb.bin` (random bytes,
   gitignored) and exits; `--upload-mb MB` makes the upload ops send that file instead of
   `dummy.txt`. Dedup caveat — identical repeated uploads may measure storage dedup, not
   full transfer (token-SHA conflicts seen); salt per upload for true transfer numbers.
4. **Visualization** (`uat/visualize.py`) — reads a run JSON + the perf JSONL and writes
   one self-contained HTML report (plotly via CDN): step latency (FAIL = red ✕), entity
   counts (`-1`/uncountable plotted below the axis), perf latency series with error
   overlay. `python -m uat.visualize [--run ID --env E --open]`. Needs `experiment` extras.
   (Was a notebook; switched to a plain `.py` — Helix has no LSP in `.ipynb` cells.)

## The one domain insight that drives the design

**Archive is a soft-delete.** `archive_*` only flips a flag; the row persists and is
still walked by the SpiceDB `LookupResources` permission scan on every list call.
So:
- Cleanup (which archives) **never shrinks the platform footprint**. Every run
  permanently grows it.
- That growth is what degrades list/permission latency over time (Jira CPD-598/601,
  the SpiceDB scaling issue — see `memory/project_list_resources_500.md`).
- Baseline counts originally used `archive_status="all"` to count archived rows too —
  **but that was reversed 2026-06-22**: passing `archive_status` (even `active`) forces a
  slow query path that *times out* on a populated env, while the default path returns
  (verified on perf: default `list_models`=1019 where `archive_status` all/active both
  timed out). So `_measure_counts` now passes **no** `archive_status` (default scope =
  active; on `--no-cleanup` benchmarking nothing is archived, so active == all). Counts
  also run in **parallel** against one `--baseline-timeout` deadline (default 90s), so the
  baseline waits once, not ×7. `models`/`systems`/`documents`/`jobs` return; `files`/
  `artifacts`/`v3_resources` still hit the CPD-598 500 and record `-1`.
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
  visualize.py     run JSON + perf JSONL → one self-contained HTML report (plotly/CDN)
  perf/
    operations.py  WHAT — Operation registry; upload testers + the `produces` chain
                   (setup fixtures + iterations hook for pool-derived rep counts)
    measure.py     HOW — timed loop → list[Sample]; count = iterations(ctx) or --repeat
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

## Relationships: the `produces` type

v3 parent/child is the `produces` revision relationship (inverse `produced_by`), model
revision → artifact revision; left = source/parent, right = derived/child. **Both the v3
correctness suite and the perf chain select it by name** (`produces`, fallback to the
first type), never `items[0]` blindly, and link two *distinct* resources — the old code
linked two revisions of the same resource (self-parent), now fixed.

Caveat: the SDK source docstrings document `produces` + left/right semantics explicitly
(generated from the OpenAPI contract), but the **rendered docs site does not** — it only
shows `items[0].id`. And whether the platform rejects a duplicate `(left,right,type)`
edge is **unverified** (the chain sidesteps it by using only distinct consecutive pairs).
A live `--ops create_resource,create_revision_relationship` run on perf settles both —
that's the one thing offline tests can't confirm.

## Known platform issues — expected failures, not code bugs

Per-env behavior is inconsistent and shifts day to day; treat this as a snapshot:
- `list_files`, v3 `list_resources`, `list_revision_relationships` → 500 (CPD-598/601).
  In baseline these hit the 15s timeout → recorded `-1`.
- `list_modules` → 503 on dev (service not in all envs).
- **`@istari:extract` is NOT on perf** → the v2 jobs suite skips entirely there; it IS on
  dev (jobs run). This contradicts the `v2.py` jobs docstring claim "available on all
  environments" — don't trust that comment.
- **v3 comments**: PASS on perf, but `create_comment`/`list_comments` → 500 on dev.
- **v3 remotes**: dev has remotes configured (get/list pass); on perf the list calls 500.
- **perf is severely degraded**: a full `--baseline` run came back all `-1` (every count
  timed out); `create_control_tag` shifted from 403 → 500 (error mode itself is unstable).
- **dev `--baseline` drift is real but benign**: a full run showed `models Δ+1, artifacts
  Δ+13` over expected — job *outputs* create resources the suite doesn't track. Not a code
  bug; it's an accounting gap (see open decisions).
- dev broadly degraded: `get_resource` 500 after 166s — contradicts the memory note that
  `get_resource` (single CheckPermission) is unaffected. Flag if reviewing dev.

## What is verified vs. not

- **Verified:** full UAT runs on dev (81/10/0, incl. jobs + the parent/child relationship
  pair) and perf; baseline returns counts (parallel, no `archive_status`); drift detection (caught the orphaned
  `job_model` and the dev job-output drift); v3 comments PASS live on perf; v3 remotes PASS
  live on dev; `visualize.py` against real run JSONs (3 charts); offline smoke of the
  perf two-phase flow with fakes (chain N−1, consecutive distinct links, empty-pool skip,
  reads honor `--repeat`); `py_compile` clean on all of `uat/`.
- **Not verified live:** the perf `create_revision_relationship` chain against a real env
  (offline-tested only) — i.e. whether `produces` exists on the env and whether the chained
  links are accepted; `add_model`/`create_resource`/`get_model`-fixture perf ops live.

## Open decisions for the reviewer

1. **Exit code:** known-broken endpoints (e.g. `list_revision_relationships`) are
   recorded as hard `FAIL`, so `runner` exits 1 even when only known-platform issues
   failed. Should these become `SKIP`/xfail so exit code reflects real regressions?
2. **`control_tags` 403/500 on perf:** expected (skip when perm absent) or should the perf
   token get the permission?
3. **Job-output drift:** on envs where jobs actually run (dev), job outputs create
   resources the suite doesn't track → `--baseline` reports drift. Track job outputs, or
   document the expected drift as benign?
4. **Duplicate `produces` edges:** unverified whether the platform rejects them; the chain
   avoids the question by construction (distinct consecutive pairs only).

Note: the style/simplicity pass is **done** (v2/v3 consolidated into one file each, line
count reduced). The suite files stay deliberately repetitive (copy-paste examples) — do
**not** DRY them into a generic helper; that hides the SDK calls they exist to show.

## Run-context capture — latency is attributable (network vs platform) [done]

Each upload now carries the context needed to split its latency between the uplink and the
platform, and `visualize`'s per-run table surfaces it:
- **`upload_mb` per sample** — payload size (`--upload-mb`, else dummy.txt). Effective
  throughput = payload ÷ median latency.
- **`resource_id` / `revision_id` per sample** — the created id, for server-log correlation
  (shown in the latency-chart hover).
- **Upstream bandwidth per run** — macOS `networkQuality` runs once at start (default on;
  `--no-network` to skip) and stamps `ul_mbps`/`dl_mbps`/`rpm` on the baseline row.
- **Verdict so far:** uplink ~100 Mbps, effective upload throughput ~7–16 Mbps → the
  platform dominates (uploads use <15% of the pipe). A 10 MB upload's ~5–11s is **not** the
  network.

**Gotcha (encoded):** the network test saturates the link, so it runs once *before* uploads,
never during — the runner enforces this. Two robustness guards also live in `measure.py`
now: per-call timeout (`--call-timeout`, default 300s → a hung socket becomes a `timeout`
sample, not a 4-day stall) and incremental `on_sample` flush (a crash loses only the
in-flight call). Both were added after a run hung 4 days and lost everything.

## Upload paths — what's measurable where (researched 2026-06-22)

Three ways to get bytes in; only the presigned ones work on perf:
- **Presigned single PUT** (default) — what every perf run here measures. The ~28s/10 MB on
  a populated perf is almost all platform overhead (uplink ~100 Mbps → ~0.8s transfer).
- **Bulk presigned** — `generate_upload_urls_bulk`, `create_revisions_bulk`,
  `add_artifacts_bulk`. Batch presigned-URL issuance; **no special creds**, so this is the
  realistic "bulk upload" tester path on perf. (Prong of CPD-393.)
- **Direct-S3 via boto3** — `storage/s3_client.py`, opt-in via `ISTARI_CLIENT_S3_DIRECT_UPLOAD`
  + `ISTARI_CLIENT_S3_BUCKET_NAME` + `[s3]` extra. Bypasses presigned, concurrent multipart.
  **Needs AWS IAM creds to the registry bucket — NOT the Istari token.** Built as a Blue
  Origin hotfix (CPD-393, PR #362); only ever provisioned on **dev**. On perf → 403 → silent
  presigned fallback, so **not testable on perf** without ops provisioning bucket creds.
  Not in public docs (SDK README only). Source repo is at 10.11.0; installed client 10.9.5.

**Bulk share** (separate feature, v2-only): `POST /api/v2/bulk/access` via
`System.bulk_share_by_{snapshot,configuration,system}`. No feature flag — gated by
per-resource share permission on the caller's token + needs a *second* USER id to share to.
viewer/editor only (executor → 500, CPD-537). Heavy: each grant writes a 2nd v3 RESOURCE
relationship → grows the SpiceDB footprint (feeds CPD-598). v3 has no user-access API at all.

## Running

```bash
PY=istari-labs-helpers/.venv/bin/python
$PY -m uat.runner --list
$PY -m uat.runner --env perf --baseline
$PY -m uat.perf   --env perf --ops add_model --repeat 10
$PY -m uat.perf   --env perf --ops create_resource,create_revision_relationship --repeat 10
$PY -m uat.perf   --make-junk 100                              # writes uat/data/junk_100mb.bin
$PY -m uat.perf   --env perf --ops add_model --repeat 20 --upload-mb 100
$PY -m uat.perf   --env dev,stage --repeat 20
$PY -m uat.visualize --env perf --open        # HTML report of the latest run (needs experiment extras)
```
Credentials: `istari-labs-helpers/.env.{env}` (dev/stage/demo/perf/prod exist).
Visualization needs `uv sync --extra experiment` (plotly/pandas) in `istari-labs-helpers`.
