---
name: cookbook-tutorial-voice
description: >-
  Writes and edits istari-digital-client-cookbook samples (notebooks, README)
  in public tutorial companion style — concise, professional, aligned with
  Istari Digital documentation standards. Use when creating or revising
  cookbook notebooks, recipe markdown, or README tutorial rows.
---

# Cookbook tutorial voice

This repository is a **public companion** to Istari Digital tutorials and SDK docs. Readers are engineers following runnable recipes, often on a live call.

## Document type

Cookbook notebooks are **tutorials** (Diataxis): learning-oriented, step-by-step, hands-on. Not marketing copy, not internal runbooks.

## Voice

- **Lead with what the reader will do** — numbered outcomes in the opening cell
- **Concise** — one idea per markdown cell; cut repetition across cells
- **Professional** — complete sentences; no chatty asides unless a deliberate callout
- **Tutorial-oriented** — say when to pause and what to check in the web app
- Prefer capabilities over absences ("External workflows run locally; the platform stores the record" not "Istari doesn't run your script")

## Naming and product terms

Follow Istari Digital documentation standards (see `../istari-documentation/.cursor/rules/documentation-standards.mdc`):

| Use | Avoid |
|-----|--------|
| **Istari Digital** | "Istari" alone (company/product) |
| **Istari Digital web app** | "the UI", "the platform UI" when meaning browser only |
| **Istari Digital Platform** | When covering APIs, agents, SDK, and web app together |
| **Resource** | "file" when meaning a registered platform item (use "file" for bytes on disk) |
| **workflow log entry**, **workflow output** | Invented synonyms |

Link to official docs when a concept has a guide: `https://docs.istaridigital.com/...`

## Notebook structure

```markdown
# [Feature] — [scenario name]

One paragraph: what this recipe demonstrates.

You will:
1. …
2. …

Companion to [other notebook](path.ipynb) if applicable.

> Callout only when it prevents a common mistake.

### Prerequisites
- `uv sync` command with correct dependency groups
- `.env` variables
- Experimental flags / permissions when relevant
- Minimum platform/registry version when a feature is release-gated
- Kernel name

### Running order
Run top to bottom; note pause points.
```

Section headings: `## N. [Verb phrase]` (e.g. `## 3. Upload workflow outputs`).

End with **Learn more** (doc links) and **Teardown (optional)** when the recipe creates demo data.

## Code cells

- **Connect** cell: load `samples/.env`, assert SDK/registry compatibility when using workflow or version-sensitive APIs
- Keep imports in the cell that first needs them unless the repo pattern groups Connect imports together
- Short comment only for non-obvious steps

## Dependency groups (`pyproject.toml`)

| Group | Scope |
|-------|--------|
| `dev` | `istari-digital-client`, `python-dotenv`, notebook/lint tooling — most recipes |
| `advanced` | `pandas`, `jinja2`, `matplotlib`, `numpy`, `pytest`, `ipython` — workflow-logs recipes only |
| `ai` | `anthropic`, `istari-labs-helpers`, `pdfplumber`, `openpyxl`, `python-docx` — AI-assisted validation recipes only |

Document the exact `uv sync --group …` command in Prerequisites.

## Markdown in notebooks

- Use bullet lists for parallel steps or API responsibilities
- Use `>` blockquotes for pause-on-call and experimental-feature notes
- Do not rely on single newlines inside a paragraph for visual breaks (they collapse in renderers)
- Tables sparingly — prerequisites package matrix is OK

## README recipe rows

One line per notebook: link, client style, one-sentence outcome. Note non-`dev` dependency groups when required.

## Review checklist

Before finishing edits:

- [ ] Opening cell states outcomes, not backstory
- [ ] Prerequisites list correct `uv sync` groups
- [ ] Experimental features called out with web app path
- [ ] Cross-links between related notebooks
- [ ] No stale references to deleted combined notebooks
- [ ] Tone matches [org-admin-tasks.ipynb](../../samples/org-admin/org-admin-tasks.ipynb) density
