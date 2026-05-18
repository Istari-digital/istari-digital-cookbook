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

The notebooks cover the same platform concepts whether they use **`istari_fluent`** or the official **`istari-digital-client`** directly. Pick the style you prefer; pairs such as [`chaining_jobs.ipynb`](samples/chaining_jobs.ipynb) and [`chaining_jobs_no_helper.ipynb`](samples/chaining_jobs_no_helper.ipynb) illustrate the same flow both ways.

| Notebook | API style | What it shows |
|---|---|---|
| [`samples/chaining_jobs.ipynb`](samples/chaining_jobs.ipynb) | `istari_fluent` | Connect, register a spreadsheet as a model, run two chained extraction jobs, trace lineage. |
| [`samples/chaining_jobs_no_helper.ipynb`](samples/chaining_jobs_no_helper.ipynb) | Official client (`istari_digital_client`) | Same chained-job story as above, using the v2 `Client` API only. |
| [`samples/misc_recipes.ipynb`](samples/misc_recipes.ipynb) | `istari_fluent` | Short, independent snippets (agents, resources, archiving, and similar). |
| [`samples/resources/resources_misc_fluent.ipynb`](samples/resources/resources_misc_fluent.ipynb) | `istari_fluent` | Model registration, text uploads, search, and bulk patterns for platform resources. |
| [`integrations/basic_catia_catpart_extraction.ipynb`](integrations/basic_catia_catpart_extraction.ipynb) | Official client (`istari_digital`) | Extract metadata from a CATIA `.CATPart`. |

Each notebook is self-contained and explains its own setup in the first cell — open the one you want and follow the prerequisites there.

## Prerequisites

- Access to an **Istari Digital instance**
- A **Personal Access Token**. See [Personal Access Tokens](https://docs.istaridigital.com/users/user-guide/settings#developer-settings--personal-access-tokens).
- A [supported Python version](https://pypi.org/project/istari-digital-client/) (see the classifiers on the `istari-digital-client` PyPI page), and [uv](https://docs.astral.sh/uv/) (recommended) or `pip` for installing dependencies.

## Repository layout

```
samples/         Tutorial notebooks — start here
integrations/    Notebooks for specific tool integrations (CATIA, ...)
fluent/          istari_fluent — helper library and package source (see below)
```

### The `istari_fluent` helper library

**Fluent** here means a [fluent interface](https://en.wikipedia.org/wiki/Fluent_interface): methods return objects you can **chain** in one expression, and the API is **object-oriented** around platform concepts (for example `IstariPlatform`, `Job`, queries) instead of scattering raw `Client` calls. `istari_fluent` is a thin convenience layer **on top of** the official [`istari-digital-client`](https://docs.istaridigital.com/developers/SDK/setup); it does not replace the SDK.

Where it helps day to day:

- **Less boilerplate** for common flows (connect from env, register files, run jobs, follow lineage) with readable, sequential code.
- **Composable queries** (for example resource search) that read like the task you are performing, not like manual pagination glue.
- **Notebook-friendly ergonomics** so recipes stay short and focused on the platform behavior, not on orchestration details.

`istari_fluent` is maintained in this cookbook repository alongside the samples; for production integrations, keep **`istari-digital-client`** as your supported source of truth. Many recipes here demonstrate **either** the fluent layer **or** the official client directly — same platform, different surface area.

### Using the fluent package in your own scripts

The Python distribution name in [`fluent/pyproject.toml`](fluent/pyproject.toml) is **`istari-digital-fluent-client`**, but you **`import istari_fluent`**. The package is **not published to PyPI** (as of this repo); install it **from a checkout of this repository** next to your project, or copy the `fluent/` tree if your policy allows.

**Recommended: editable install** (picks up changes when you `git pull` the cookbook):

From the directory that contains `fluent/` (the cookbook root):

```bash
# uv
uv pip install -e ./fluent

# or pip
pip install -e ./fluent
```

That pulls in **`istari-digital-client`**, **`python-dotenv`**, and **Pydantic** per the package metadata. Use your own virtual environment; the cookbook notebooks often use `uv sync --project fluent --extra experiment` for a kernel with Jupyter, which also installs `istari_fluent` in editable mode.

**Install a built wheel** (frozen snapshot of whatever you built):

```bash
cd fluent
uv build   # or: pip install build && python -m build
pip install dist/istari_digital_fluent_client-*.whl
```

**Without installing the package**, you can add the `fluent/` directory to `PYTHONPATH` when running a script (fragile if paths move, but fine for quick experiments):

```bash
PYTHONPATH="/path/to/istari-digital-client-cookbook/fluent:$PYTHONPATH" python your_script.py
```

In code, the usual entry point matches the notebooks:

```python
from istari_fluent import IstariPlatform

platform = IstariPlatform.from_env()
```

## Feedback and contributions

Found a bug, have a recipe you'd like to see, or want to contribute one? [Open an issue or pull request](https://github.com/Istari-digital/istari-digital-client-cookbook) on GitHub.

## License

See the [license file](./LICENSE) for details.

No license is hereby implied or granted to any patent or patent application relating to the Istari Digital platform itself. The list of patents applicable to the Istari Digital platform may be found at [istaridigital.com/patent-list](https://istaridigital.com/patent-list).
