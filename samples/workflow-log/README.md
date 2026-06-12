# External Workflow Logs — interactive walkthrough

[`workflow_log_demo.ipynb`](workflow_log_demo.ipynb) walks through Istari's **External Workflow Log** feature end to end: an external verification battery whose runs (output files + a pass/fail verdict) are recorded on a system as durable, queryable log entries tied to the configuration they ran against. It covers two scenarios — a fail → revise → pass design loop, and a tradespace sweep logged as a campaign.

## Setup

Create a `.env` file next to the notebook:

```
ISTARI_REGISTRY_URL=<your registry URL>
ISTARI_UI_URL=<your instance UI URL>
ISTARI_USER_PAT=<your personal access token>
```

The **Workflow log** tab is experimental — enable it on your instance under **Application Settings → Experimental — External Workflows**.

## Files

| File | Role |
|---|---|
| `workflow_log_demo.ipynb` | The walkthrough — run it cell by cell. |
| `bracket_step.py` | Generates the synthetic parametric STEP files (R3 / R5 bracket). |
| `run_workflow.py` | The local verification battery — the "work outside Istari". |
| `tradespace_tests.py` | pytest requirement suite used by the tradespace sweep (Scenario B). |
| `istari_helpers.py` | Best-effort snapshot helpers so the design surfaces in the UI. |

The notebook creates its own demo system and writes scratch files to `_notebook_run/`; the final cell archives the system again (reversible).
