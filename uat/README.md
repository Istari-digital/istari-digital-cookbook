# Istari Digital SDK UAT

End-to-end test suite covering the publicly documented SDK surface at
[docs.istaridigital.com/developers/SDK](https://docs.istaridigital.com/developers/SDK).
Each suite also serves as working example code for that API area.

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
├── v2/             — one file per public v2 docs page
└── v3/             — documented v3 endpoints (quick-start page)
```

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

### v3 suites (docs page: V3 Client Quick Start)

| Suite | Methods covered |
|---|---|
| `v3_resources` | `create_resource`, `get_resource` |
| `v3_revisions` | `create_resource_revision`, `list_resource_revisions`, `get_resource_revision` |
| `v3_relationships` | `list_revision_relationship_types`, `create_revision_relationship` |

---

## Known platform issues (not code bugs)

| Endpoint | Error | Tracking |
|---|---|---|
| `list_files`, `list_resources` | 500 after ~30s | CPD-598/601 — SpiceDB `LookupResources` scan times out on large tenants |
| `list_revision_relationships` | 500 | Same root cause (CPD-598) |
| `list_modules` | 503 on dev | Modules service not available in all environments |

---

## Not yet covered — undocumented / experimental

These exist in the SDK but are not yet in public documentation. Files are
present but not wired into the runner. Add to `SUITES` in `runner.py` once documented.

### `v2_change_requests.py`
`ChangeRequest*` model classes exist in the package but `Client` has no
corresponding methods in SDK 10.11.1. Appears to be in-progress, related to
the experimental branching UI.

### `v3_comments.py`
`create_comment`, `list_comments`, `update_comment`, `archive_comment`,
`restore_comment` — present in `V3Client` but not on the public docs site.

### `v3_remotes.py`
`list_sending_remotes`, `list_receiving_remotes`, etc. — present in `V3Client`
but not documented publicly.

### v3 archive/restore
`archive_resource`, `restore_resource` — present in `V3Client` but not
covered on the v3 quick-start docs page.
