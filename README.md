# Istari Digital Cookbook

Runnable recipes for driving the [Istari Digital Platform](https://istaridigital.com) from Python — small notebooks you can open, run, and adapt into your own workflows.

This is a companion to the official [Istari Digital Documentation](https://docs.istaridigital.com): the docs explain **what** and **why**; the cookbook shows **how**, end to end.

## New to the platform?

If you have not driven Istari Digital from code before, do these two short tutorials first — they are the foundation the cookbook builds on:

1. [**Platform 101**](https://docs.istaridigital.com/tutorials/platform/platform-101) — register a file, run a job, and compare revisions from the UI.
2. [**Python Client 201**](https://docs.istaridigital.com/tutorials/python-client/201) — the same flow, now from a short Python script.

Then come back here for end-to-end notebooks on specific tasks and integrations.

## Background reading

Keep these three pages handy while you work through the recipes — they are the reference the notebooks assume you know:

- [**Key Concepts**](https://docs.istaridigital.com/intro/key-concepts)
- [**Terminology**](https://docs.istaridigital.com/intro/terminology)
- [**Python Client — Quick Start**](https://docs.istaridigital.com/developers/SDK/setup) — installing `istari-digital-client`, configuring it from environment variables, and the basic `Client` / `Configuration` pattern used under the hood by every recipe.

## Recipes

The notebooks cover the same platform concepts whether they use **`istari_labs_helpers`** (this repo’s labs helper library) or the official **`istari-digital-client`** directly. Pick the style you prefer; pairs such as [`chaining_jobs.ipynb`](samples/chaining_jobs.ipynb) and [`chaining_jobs_no_helper.ipynb`](samples/chaining_jobs_no_helper.ipynb) illustrate the same flow both ways.

| Notebook | API style | What it shows |
|---|---|---|
| [`samples/chaining_jobs.ipynb`](samples/chaining_jobs.ipynb) | `istari_labs_helpers` | Connect, register a spreadsheet as a model, run two chained extraction jobs, trace lineage. |
| [`samples/chaining_jobs_no_helper.ipynb`](samples/chaining_jobs_no_helper.ipynb) | Official client (`istari_digital_client`) | Same chained-job story as above, using the v2 `Client` API only. |
| [`samples/misc_recipes.ipynb`](samples/misc_recipes.ipynb) | `istari_labs_helpers` | Short, independent snippets (agents, resources, archiving, and similar). |
| [`samples/resources/using-resources.ipynb`](samples/resources/using-resources.ipynb) | Official client (`V3Client` + v2 `Client`) | Files (resources): upload, search, versions, comments, sharing, and cleanup. |
| [`samples/resources/connect-resources-twc.ipynb`](samples/resources/connect-resources-twc.ipynb) | Official client (`istari_digital_client`) | Teamwork Cloud: upload pointer file (connected `mdel://` link), TWC auth, `@istari:twc_extract`. |
| [`samples/resources/connect-resources-catia-3dx.ipynb`](samples/resources/connect-resources-catia-3dx.ipynb) | Official client (`istari_digital_client`) | 3DEXPERIENCE CATIA: register connected pointer, run `@istari:extract`. |
| [`samples/cad/update_catia_parameter.ipynb`](samples/cad/update_catia_parameter.ipynb) | `istari_labs_helpers` | Update a CATIA parameter via `@istari:update_parameters` (resource ID as input). |
| [`samples/resources/download-system-resources.ipynb`](samples/resources/download-system-resources.ipynb) | `istari_labs_helpers` | Download all tracked resources on a system branch (file or zip). |
| [`samples/validation/ai-validation.ipynb`](samples/validation/ai-validation.ipynb) | `istari_labs_helpers` + Anthropic | Compare two text models with Claude, write an HTML diff report, track it on the system branch. `uv sync --group dev --group ai`. |
| [`samples/validation/ui-sample.ipynb`](samples/validation/ui-sample.ipynb) | ipywidgets | Notebook form: pick a `(label, id)` from a dropdown and reuse the selection in later cells. |
| [`samples/org-admin/org-admin-tasks.ipynb`](samples/org-admin/org-admin-tasks.ipynb) | Official client (`istari_digital_client`) | Org admin: find user by email, list tools, grant executor access to all tools. |
| [`samples/workflow-logs/workflow_log_scenario_a.ipynb`](samples/workflow-logs/workflow_log_scenario_a.ipynb) | Official client (`istari_digital_client` + `V3Client`) | External workflow logs — Scenario A: verification battery, fail/revise/pass loop. Registry Service **> 10.17.3** (2026-05+). `uv sync --group dev --group advanced`. |
| [`samples/workflow-logs/workflow_log_scenario_b.ipynb`](samples/workflow-logs/workflow_log_scenario_b.ipynb) | Official client (`istari_digital_client` + `V3Client`) | External workflow logs — Scenario B: pytest tradespace sweep and campaign entries. Registry Service **> 10.17.3** (2026-05+). `uv sync --group dev --group advanced`. |
| [`samples/resources/resources_misc_labs_helpers.ipynb`](samples/resources/resources_misc_labs_helpers.ipynb) | `istari_labs_helpers` | Model registration, text uploads, search, and bulk patterns for platform resources. |
| [`integrations/basic_catia_catpart_extraction.ipynb`](integrations/basic_catia_catpart_extraction.ipynb) | Official client (`istari_digital`) | Extract metadata from a CATIA `.CATPart`. |

Each notebook is self-contained and explains its own setup in the first cell — open the one you want and follow the prerequisites there.

## Prerequisites

- Access to an **Istari Digital instance**
- A **Personal Access Token**. See [Personal Access Tokens](https://docs.istaridigital.com/users/user-guide/settings#developer-settings--personal-access-tokens).
- A [supported Python version](https://pypi.org/project/istari-digital-client/) (see the classifiers on the `istari-digital-client` PyPI page), and [uv](https://docs.astral.sh/uv/) (recommended) or `pip` for installing dependencies.

From the cookbook root, install notebook dependencies (most recipes):

```bash
uv sync --group dev
```

Workflow-logs recipes also need `--group advanced`. The AI validation recipe needs `--group ai` (Anthropic SDK + `istari-labs-helpers`). See each notebook’s prerequisites cell for the exact command.

Match the installed **`istari-digital-client`** version to your Istari Digital Platform release — see [Python client version](#python-client-version) below.

## Python client version

Cookbook notebooks depend on **`istari-digital-client`** from the root [`pyproject.toml`](pyproject.toml) (`dev` dependency group). The pinned version should match the Python SDK shipped with your Istari Digital Platform release.

### Which version to use

Open the release notes for your Istari Digital Platform version in the [Istari Digital documentation](https://docs.istaridigital.com) (**Releases** in the sidebar — for example [2026.05.01](https://docs.istaridigital.com/releases/2026-05-01-Release) or [2026.04.01](https://docs.istaridigital.com/2026.04/releases/2026-04-01-Release)). Under **Assets → SDK Clients**, note the **Python** version — that is the `istari-digital-client` release to install.

At runtime, `Client.check_compatibility()` reports the registry’s expected client version as `server_version`; your installed package should match it before you call version-sensitive APIs.

### Update the pin for notebooks

From the cookbook root:

1. Edit the pin in [`pyproject.toml`](pyproject.toml) (`dependency-groups.dev`, key `istari-digital-client`), or run:

   ```bash
   uv add --group dev "istari-digital-client==<version-from-release-notes>"
   ```

2. Refresh the environment:

   ```bash
   uv sync --group dev
   ```

3. **Restart the Jupyter kernel** so notebooks pick up the new package.

If you use `istari_labs_helpers` via `uv sync --project istari-labs-helpers --extra experiment`, run a cookbook-root `uv sync --group dev` first (or reinstall the client explicitly) so the helper venv gets the same `istari-digital-client` version.

## Repository layout

```
samples/              Tutorial notebooks — start here
integrations/         Notebooks for specific tool integrations (CATIA, ...)
istari-labs-helpers/  istari_labs_helpers — helper library package source (see below)
```

### The `istari_labs_helpers` package (`istari-labs-helpers`)

The API follows a **fluent interface** style: methods return objects you can **chain**, and the surface is **object-oriented** around platform concepts (for example `IstariPlatform`, `Job`, queries) instead of scattering raw `Client` calls everywhere. **`istari_labs_helpers`** is a thin convenience layer **on top of** the official [`istari-digital-client`](https://docs.istaridigital.com/developers/SDK/setup); it does not replace the SDK.

Where it helps day to day:

- **Less boilerplate** for common flows (connect from env, register files, run jobs, follow lineage) with readable, sequential code.
- **Composable queries** (for example resource search) that read like the task you are performing, not like manual pagination glue.
- **Notebook-friendly ergonomics** so recipes stay short and focused on the platform behavior, not on orchestration details.

This helper library is maintained in this cookbook repository alongside the samples; for production integrations, keep **`istari-digital-client`** as your supported source of truth. Many recipes here demonstrate **either** `istari_labs_helpers` **or** the official client directly — same platform, different surface area.

### Using `istari_labs_helpers` in your own scripts

The Python distribution name in [`istari-labs-helpers/pyproject.toml`](istari-labs-helpers/pyproject.toml) is **`istari-labs-helpers`**; you **`import istari_labs_helpers`**. The package is **not published to PyPI** (as of this repo); install it **from a checkout of this repository** next to your project, or copy the `istari-labs-helpers/` tree if your policy allows.

**Recommended: editable install** (picks up changes when you `git pull` the cookbook):

From the directory that contains `istari-labs-helpers/` (the cookbook root):

```bash
# uv
uv pip install -e ./istari-labs-helpers

# or pip
pip install -e ./istari-labs-helpers
```

That pulls in **`istari-digital-client`**, **`python-dotenv`**, and **Pydantic** per the package metadata. Use your own virtual environment; the cookbook notebooks often use `uv sync --project istari-labs-helpers --extra experiment` for a kernel with Jupyter, which also installs `istari_labs_helpers` in editable mode.

**Install a built wheel** (frozen snapshot of whatever you built):

```bash
cd istari-labs-helpers
uv build   # or: pip install build && python -m build
pip install dist/istari_labs_helpers-*.whl
```

**Without installing the package**, you can add the `istari-labs-helpers/` directory to `PYTHONPATH` when running a script (fragile if paths move, but fine for quick experiments):

```bash
PYTHONPATH="/path/to/istari-digital-client-cookbook/istari-labs-helpers:$PYTHONPATH" python your_script.py
```

In code, the usual entry point matches the notebooks:

```python
from istari_labs_helpers import IstariPlatform

platform = IstariPlatform.from_env()
```

## Feedback and contributions

Found a bug, have a recipe you'd like to see, or want to contribute one? [Open an issue or pull request](https://github.com/Istari-digital/istari-digital-client-cookbook) on GitHub.

## License

See the [license file](./LICENSE) for details.

No license is hereby implied or granted to any patent or patent application relating to the Istari Digital platform itself. The list of patents applicable to the Istari Digital platform may be found at [istaridigital.com/patent-list](https://istaridigital.com/patent-list).
