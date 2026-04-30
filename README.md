# Istari Digital Client Cookbook

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

| Notebook | What it shows |
|---|---|
| [`samples/01_getting_started.ipynb`](samples/01_getting_started.ipynb) | End-to-end walkthrough: connect, find or create a system, register a spreadsheet, run two chained extraction jobs, and trace the resulting lineage. |
| [`integrations/basic_catia_catpart_extraction.ipynb`](integrations/basic_catia_catpart_extraction.ipynb) | Extract metadata from a CATIA `.CATPart` using the official Python client. |

Each notebook is self-contained and explains its own setup in the first cell — open the one you want and follow the prerequisites there.

## Prerequisites

- Access to an **Istari Digital instance**
- A **Personal Access Token**. See [Personal Access Tokens](https://docs.istaridigital.com/users/user-guide/settings#developer-settings--personal-access-tokens).
- A [supported Python version](https://pypi.org/project/istari-digital-client/) (see the classifiers on the `istari-digital-client` PyPI page), and [uv](https://docs.astral.sh/uv/) (recommended) or `pip` for installing dependencies.

## Repository layout

```
samples/         Tutorial notebooks — start here
integrations/    Notebooks for specific tool integrations (CATIA, ...)
fluent/          istari_fluent — an ergonomic wrapper around the official client, used by the samples
```

`istari_fluent` is an opinionated productivity layer maintained alongside the official client; for production integrations, keep [`istari-digital-client`](https://docs.istaridigital.com/developers/SDK/setup) as your source of truth.

## Feedback and contributions

Found a bug, have a recipe you'd like to see, or want to contribute one? [Open an issue or pull request](https://github.com/Istari-digital/istari-digital-client-cookbook) on GitHub.

## License

See the [license file](./LICENSE) for details.

No license is hereby implied or granted to any patent or patent application relating to the Istari Digital platform itself. The list of patents applicable to the Istari Digital platform may be found at [istaridigital.com/patent-list](https://istaridigital.com/patent-list).
