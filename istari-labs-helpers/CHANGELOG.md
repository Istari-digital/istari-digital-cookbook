# Changelog

All notable changes to `istari-labs-helpers` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

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
