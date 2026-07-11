# Changelog

All notable changes to `istari-labs-helpers` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **`BranchView.add_resource` / `BranchView.add_file`** — convenience wrappers that start from the branch HEAD configuration.
- **`ConfigurationView.add_resource` / `TrackedFileSet.add_resource`** — track an already-uploaded `ResourceView` / `ModelView` by file id.
- **`BranchView.configuration`** — configuration behind the branch HEAD snapshot.
- **`BranchView.advance_to(configuration)`** — snapshot a configuration and move this branch tag to it.
- **`IstariPlatform.get_resource_at_revision(revision_id)`** — parent resource pinned to that revision (`doc.id` / `doc.revision_id`).
- **`BranchView`** — wraps a snapshot tag (branch); `list_revisions()`, `download_resources()`.
- **`SystemView.branches()` / `get_branch()` / `find_branch()`** — list and resolve branches (snapshot tags), not configurations.
- **`BranchDownloadResult`** and branch download helpers — download file revisions at a branch HEAD: single file when there is one revision, `.zip` when there are several.
- **`SystemView.download_resources(branch)`**, **`IstariPlatform.get_system_by_id()`**, **`IstariPlatform.download_system_resources(system_id, branch)`**.

### Changed

- **`ConfigurationView.set_baseline()`** — creates a snapshot when needed before moving the baseline tag (so `save().set_baseline()` works without a prior snapshot).
- **Branch downloads** — use the SDK branching API (`get_branch`, `list_branch_revisions`) instead of configuration tracked-file listings.

- **`UserView.tools()`** — now returns tools this **user** may execute (permissions API), including when resolved via `get_user(email)`. Use `platform.tools()` for the full org catalog visible to your token.

## [0.2.0] - 2026-06-29

### Added

- **`IstariPlatform.whoami()`** — returns a `UserView` for the token-authenticated user (`print(me.id)` works out of the box).
- **`UserView`** — fluent wrapper with `id`, `email`, `display_name`, `tools()`, and `granted_tools()`.
- **`ToolView`** and **`ToolQuery`** — `platform.tools()` and `user.tools()` iterate `ToolView` instances; `.with_functions()` / `.include(ToolInclude.FUNCTIONS)` mirror the v2 API.
- **`IstariPlatform.find_user()` / `get_user()`** — org-admin helpers to resolve users by email.
- **`IstariPlatform.v3`** — exposes the v3 `V3Client` alongside v2; internal `SdkClients` pairs both surfaces from one `Configuration`.
- **`CHANGELOG.md`** — tracks helper-library releases (this file).

### Changed

- **`istari-digital-client` dependency** — pinned to **10.14.0** from PyPI.
- **`platform.tools()`** — returns `ToolQuery` (yields `ToolView`) instead of a raw `ItemQuery`.

## [0.1.0] - initial release

- Entity-oriented wrappers: `IstariPlatform`, `SystemView`, `ModelView`, `JobView`, `ResourceView`, lineage, and lazy `ItemQuery` / `ResourceQuery` factories.
