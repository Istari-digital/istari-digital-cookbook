# Istari Digital SDK UAT

End-to-end test suite covering the publicly documented SDK surface at
[docs.istaridigital.com/developers/SDK](https://docs.istaridigital.com/developers/SDK).
Each suite also serves as working example code for that API area.

**Scope rule: only documented endpoints get suites.** A suite exists iff its methods
appear on the public docs site — v2 suites map to the method sections of the single
[v2 client reference page](https://docs.istaridigital.com/developers/SDK/api_reference/client/),
v3 suites to the sections of the
[V3Client reference page](https://docs.istaridigital.com/developers/SDK/v3/v3-client/).
SDK methods that exist but are undocumented are out of scope until the docs ship.
Re-check the docs when adding suites — they move (docs inventory last verified 2026-06-10).

## Running

```bash
# From repo root, using the istari-labs-helpers venv:
/path/to/istari-labs-helpers/.venv/bin/python -m uat.runner --list
/path/to/istari-labs-helpers/.venv/bin/python -m uat.runner --env demo
/path/to/istari-labs-helpers/.venv/bin/python -m uat.runner --env dev --suite v2_models,v2_jobs
/path/to/istari-labs-helpers/.venv/bin/python -m uat.runner --env stage --no-cleanup
```

Credentials are read from `istari-labs-helpers/.env.{env}`.

## Structure

```
uat/
├── runner.py       — argparse CLI entry point
├── common.py       — TestContext, @ctx.step decorator, logging, results
├── data/           — upload fixtures (dummy.txt)
├── results/        — rotating log (uat.log) + per-run JSON (gitignored)
├── v2.py           — all v2 suites, sections in the docs page's order
└── v3.py           — all v3 suites, sections in the v3-client page's order
```

Suite names on the CLI are `{file}_{function}` (e.g. `--suite v2_models,v3_comments`).

### v2 suites (docs pages)

| Suite | Docs page |
|---|---|
| `v2_files` | Files, Models & Artifacts |
| `v2_models` | Files, Models & Artifacts |
| `v2_artifacts` | Files, Models & Artifacts |
| `v2_revisions` | Files, Models & Artifacts |
| `v2_jobs` | Jobs |
| `v2_systems` | Systems & Snapshots |
| `v2_snapshots` | Systems & Snapshots |
| `v2_documents` | Documents |
| `v2_access` | Access Control |
| `v2_control_tags` | Access Control |
| `v2_agents` | Agents, Modules & Tools |
| `v2_tools` | Agents, Modules & Tools |
| `v2_users` | Admin |

### v3 suites (docs page: [V3Client reference](https://docs.istaridigital.com/developers/SDK/v3/v3-client/))

| Suite | Methods covered |
|---|---|
| `v3_resources` | `create_resource`, `get_resource` |
| `v3_revisions` | `create_resource_revision`, `list_resource_revisions`, `get_resource_revision`, `get_content` |
| `v3_relationships` | `list_revision_relationship_types`, `create_revision_relationship`, `list_revision_relationships` |
| `v3_comments` | `create_comment`, `get_comment`, `list_comments`, `update_comment`, `archive_comment`, `restore_comment` |
| `v3_remotes` | `list_sending_remotes`, `get_sending_remote`, `list_receiving_remotes`, `get_receiving_remote` |

---

## Baselining (`--baseline`)

`--baseline` measures the platform's **total entity footprint** before and after the
run (attaching per-step `platform_state` and writing `baseline` + `final_counts` to
the results JSON).

Counts use `archive_status="all"` on purpose: **archive is a soft-delete** — archived
rows still exist and are still walked by the SpiceDB permission scan on every list call
(CPD-598/601), so they are what drives latency. Cleanup only archives, so it never
shrinks the footprint: each run permanently grows the env and repeated runs degrade it
(on `perf`, a ~23s run became 6 min after a few rounds). Because created resources
persist, the post-run check expects `final == baseline + created` and warns on any drift
(another user's activity, an untracked resource, or a hard delete). Run on a disposable
tenant — never trust baseline numbers from a shared env.

---

## Perf measurement (`uat.perf`)

Separate from the correctness suites: run a subset of endpoints N times per env and
record per-call latency as a time series, for dashboards.

```bash
python -m uat.perf --list                                   # available operations
python -m uat.perf --env perf --ops upload_model --repeat 10
python -m uat.perf --env dev,stage --repeat 20              # several envs, one run_id
```

The package separates concerns: `operations.py` (*what* to measure — the endpoint
catalog), `measure.py` (*how* — the timed repeat loop), `store.py` (persistence — the
dashboard contract), `report.py` (console summary), `runner.py` (CLI + multi-env loop).

Each run appends to two per-env JSONL streams under `results/perf/` (gitignored):

| File | One line per | Key fields |
|---|---|---|
| `{env}.samples.jsonl` | measured call | `run_id, env, operation, iteration, started_at, duration_s, status` |
| `{env}.baseline.jsonl` | run | `run_id, env, taken_at, <entity counts>` |

`started_at` (ISO-8601 UTC) is the dashboard x-axis; `duration_s` the latency series;
the baseline stream is the footprint to plot alongside it. One env = one file; envs
share the schema, so comparing envs is a filter. A failed call is recorded as a
`status="error"` sample (with the message) rather than aborting the run.

### Visualization (`visualize.py`)

Renders one self-contained HTML report (plotly via CDN): UAT step latency (failures
as red ✕), baseline counts (`-1` = uncountable, dips below the axis), perf latency
time series with error overlay, footprint over runs. Needs the `experiment` extras
(`uv sync --extra experiment`).

```bash
python -m uat.visualize                                # latest run, env perf
python -m uat.visualize --run 20260610_104751 --env dev --open
```

Plain `.py`, not a notebook: Helix has no LSP inside `.ipynb` cells — it opens
notebooks as JSON ([helix#6927](https://github.com/helix-editor/helix/pull/6927))
and lacks LSP notebook sync, so even ruff's notebook support never engages
([ruff#22809](https://github.com/astral-sh/ruff/issues/22809)).

---

## Known platform issues (not code bugs)

| Endpoint | Error | Tracking |
|---|---|---|
| `list_files`, `list_resources` | 500 after ~30s | CPD-598/601 — SpiceDB `LookupResources` scan times out on large tenants |
| `list_revision_relationships` | 500 | Same root cause (CPD-598) |
| `list_modules` | 503 on dev | Modules service not available in all environments |

---

## Coverage gaps — documented but not yet suited

Verified against the live docs 2026-06-10. All previously-undocumented suites
(`v3_comments`, `v3_remotes`) are now on the
[V3Client reference page](https://docs.istaridigital.com/developers/SDK/v3/v3-client/)
and are registered in the runner. `v2_change_requests` was deleted: change requests
are absent from both the docs and the SDK `Client`.

Still documented but uncovered:

- **v2 Infosec levels** — its own section on the
  [v2 client page](https://docs.istaridigital.com/developers/SDK/api_reference/client/); no suite yet.
- **v3 workflow logs** — own page at
  [v3/workflow-logs](https://docs.istaridigital.com/developers/SDK/v3/workflow-logs/); no suite yet.
- **v3 `archive_resource`/`restore_resource`** — documented; exercised only by cleanup, not as steps.
- **v3 remote create/update** (`create_sending_remote` etc.) — documented; the suite stays
  read-only because creating remote connections mutates cross-tenant config.
