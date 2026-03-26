# Branch Review: main (full implementation)

**Base:** f1c3f9f (scaffold) | **Commits:** 23 | **Files Changed:** 25 | **Lines:** +4685 / -88
**Date:** 2026-03-26

## Summary

The pipeline architecture is clean and the modules map 1:1 to pipeline stages with correct dependency flow. However, three functional bugs prevent the tool from producing correct results: the query item is never excluded from its own search results (self-match), RRF fusion deduplication is broken due to an int/string type mismatch between FTS5 and vec0, and cross-reference extraction is implemented but never wired into the pipeline. These must be fixed before the tool is usable. Beyond these, there are several missing repo filters, dead code from the search-to-list migration, and minor code quality issues.

## Findings

### Architecture

- [CRITICAL] **Self-exclusion filters silently ignored — query item matches itself** — `src/mpy_triage/cli.py:221`, `src/mpy_triage/search.py:50-53` — commit: `9a8801e`
  `_triage_item()` passes `filters={"exclude_number": number, "exclude_repo": repo}` but `dense_search()` only allows `{"repo", "item_type"}`. Both keys are silently dropped. The query item will always be its own top match. `keyword_search` has no filtering at all.
  Recommendation: Implement post-filter exclusion in `search()` before reranking, removing the query item from merged results.

- [WARNING] **Cross-reference extraction not wired into pipeline** — `src/mpy_triage/cli.py`, `src/mpy_triage/collect.py` — commit: `9a8801e`
  `extract_cross_references()` and `build_ground_truth()` are never called from any CLI command or `collect_all()`. The summarizer queries the `cross_references` table for linked items but it will always be empty.
  Recommendation: Call both at the end of `collect_all()` or add a dedicated CLI command.

- [WARNING] **`assemble_item` called but not persisted in `_triage_item`** — `src/mpy_triage/cli.py:209` — commit: `9a8801e`
  `_triage_item` calls `assemble_item()` which returns XML but doesn't call `_assemble_and_store()` to persist it. When `assess_candidates` later looks up `assembled_xml`, the query item won't be there.
  Recommendation: Use `_assemble_and_store` or call `assemble_all` for the single item.

- [WARNING] **System prompt mixed into user prompt for claude subprocess** — `src/mpy_triage/assess.py:150`, `src/mpy_triage/summarize.py:200` — commit: `e7992dc`
  Both modules concatenate system + user prompt into a single stdin input. The model doesn't get system-vs-user separation.
  Recommendation: Verify if `claude -p` supports `--system-prompt`. If not, document as known limitation.

- [WARNING] **`_full_sync_via_search` is dead code** — `src/mpy_triage/collect.py:24` — commit: `47812d8`
  No longer called after switch to list endpoints. `gh_search` import in `collect.py` is also unused.
  Recommendation: Remove dead function and unused import.

### Code Quality

- [CRITICAL] **RRF dedup broken due to int/string type mismatch** — `src/mpy_triage/search.py:93`, `src/mpy_triage/embed.py:188` — commit: `8c540f0`
  FTS5 returns `item_number` as string, vec0 returns integer. RRF fusion keys `(1, "issue", "repo")` vs `("1", "issue", "repo")` never match. Items found by both methods are double-counted as separate entries instead of having scores merged.
  Recommendation: Cast to int in `keyword_search`: `"item_number": int(row[0])`.

- [WARNING] **`extract_cross_references` ignores repo filter** — `src/mpy_triage/crossref.py:122-131` — commit: `b254213`
  SQL queries scan all repos regardless of `repo` parameter. Items from wrong repo get incorrect `source_repo` assignment.
  Recommendation: Add `WHERE repo = ?` to all three source queries.

- [WARNING] **`build_ground_truth` ignores repo filter** — `src/mpy_triage/crossref.py:186` — commit: `b254213`
  `SELECT ... WHERE state_reason = 'duplicate'` has no repo filter. Issues from all repos are processed with the same `source_repo`.
  Recommendation: Add `AND repo = ?` to the WHERE clause.

- [WARNING] **Duplicated `_clean_env()` function** — `src/mpy_triage/summarize.py:49`, `src/mpy_triage/assess.py:45` — commit: `16920f6`, `e7992dc`
  Identical function in both modules.
  Recommendation: Extract to a shared helper.

- [WARNING] **Inconsistent logger naming** — multiple files — various commits
  `db.py` and `gh.py` use `log`, other files use `logger`.
  Recommendation: Standardize on `logger`.

- [INFO] **`embed.py:218` accesses private `_config`** — `src/mpy_triage/embed.py:218` — commit: `403f15a`
  `rebuild_index` calls `embedder._config`. Recommendation: Add a public property.

- [INFO] **`EmbeddingConfig.device` type annotation is `str` but default is `None`** — `src/mpy_triage/config.py:20` — commit: `f14015d`
  Recommendation: Annotate as `str | None`.

- [INFO] **`format_human` uses `getattr` for non-existent Assessment fields** — `src/mpy_triage/format.py:39-40` — commit: `9a8801e`
  `title`, `created_at`, `state` don't exist on Assessment. Always returns defaults.
  Recommendation: Either add fields to Assessment or remove dead display logic.

- [INFO] **Duplicate `tmp_db` fixtures** — `tests/test_db.py:13`, `tests/test_crossref.py:220` — commit: `f14015d`
  Shadow the `conftest.py` fixture with different behavior (file-based vs in-memory).
  Recommendation: Rename to avoid shadowing.

### Completeness

- [WARNING] **`keyword_search` has no filter support** — `src/mpy_triage/search.py:78-102` — commit: `8c540f0`
  Dense search supports repo/item_type filters but BM25 doesn't. Items excluded from dense results still appear via BM25.
  Recommendation: Add filter support or at minimum post-filter for exclusion.

- [WARNING] **Reranker instantiated per-search call** — `src/mpy_triage/search.py:220-222` — commit: `8c540f0`
  Model re-loaded on every call. Embedder is cached but Reranker is not.
  Recommendation: Accept optional Reranker instance or cache at module level.

- [WARNING] **No test for `summarize_all`** — `tests/test_summarize.py` — commit: `16920f6`
  Batch processing with checkpointing has no test coverage.

- [WARNING] **`discovered_at` in ground_truth never set** — `src/mpy_triage/crossref.py:247` — commit: `b254213`
  Always NULL. Recommendation: Set to current timestamp.

- [INFO] **`_triage_item` searches only by title** — `src/mpy_triage/cli.py:219` — commit: `9a8801e`
  Ignores body content. Recommendation: Use assembled XML or title + body.

- [INFO] **Plugin/skill definitions incomplete vs plan** — `.claude-plugin/plugin.json` — commit: `562a029`
  Plan calls for `skills/triage/SKILL.md` and `.mcp.json` but `plugin.json` dropped the `skills` key.

- [INFO] **`collect_issues` doesn't use `sort=updated`** — `src/mpy_triage/collect.py:65` — commit: `47812d8`
  Default sort is `created`, but incremental sync filters by `updated_at`.

### Security & Robustness

- [WARNING] **No timeout on `gh api` subprocess** — `src/mpy_triage/gh.py:56` — commit: `f14015d`
  Unlike summarize/assess (300s/120s timeouts), `gh_api` has no timeout. Network issues cause indefinite hang.
  Recommendation: Add 120s timeout.

- [WARNING] **Unbounded in-memory collection** — `src/mpy_triage/collect.py:73`, `src/mpy_triage/gh.py:83` — commit: `47812d8`
  50k+ comments loaded into a single list. Hundreds of MB potential.
  Recommendation: Accept as known trade-off or consider streaming.

- [WARNING] **No repo name validation at CLI boundary** — `src/mpy_triage/cli.py` — commit: `9a8801e`
  `--repo` value passed directly to API endpoints without format validation.
  Recommendation: Validate `^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$` pattern.

- [WARNING] **Prompt injection from untrusted GitHub content** — `src/mpy_triage/summarize.py:218`, `src/mpy_triage/assess.py:167` — commit: `16920f6`
  Arbitrary GitHub issue text passed to claude subprocess. Accepted design constraint but no sanitization.
  Recommendation: Document as accepted risk, validate schema keys on output.

- [INFO] **f-string table name interpolation** — `src/mpy_triage/assemble.py:99`, `src/mpy_triage/cli.py:291` — commit: `7170de3`
  Safe (hardcoded values) but fragile. Recommendation: Add assertions.

- [INFO] **`_find_duplicate_targets` comment query lacks repo filter** — `src/mpy_triage/crossref.py:227` — commit: `b254213`
  Low risk but inconsistent with other queries.

## Action Items

- [ ] [CRITICAL] Fix self-exclusion: post-filter query item from search results — `src/mpy_triage/search.py` / `src/mpy_triage/cli.py:221`
- [ ] [CRITICAL] Fix RRF type mismatch: cast `item_number` to int in `keyword_search` — `src/mpy_triage/search.py:93`
- [ ] [WARNING] Wire cross-reference extraction into pipeline — `src/mpy_triage/collect.py` / `src/mpy_triage/cli.py`
- [ ] [WARNING] Fix `assemble_item` not persisted in `_triage_item` — `src/mpy_triage/cli.py:209`
- [ ] [WARNING] Add repo filter to `extract_cross_references` queries — `src/mpy_triage/crossref.py:122-131`
- [ ] [WARNING] Add repo filter to `build_ground_truth` query — `src/mpy_triage/crossref.py:186`
- [ ] [WARNING] Add repo filter to `_find_duplicate_targets` — `src/mpy_triage/crossref.py:227`
- [ ] [WARNING] Remove dead `_full_sync_via_search` and unused `gh_search` import — `src/mpy_triage/collect.py`
- [ ] [WARNING] Extract shared `_clean_env()` to avoid duplication — `src/mpy_triage/summarize.py`, `src/mpy_triage/assess.py`
- [ ] [WARNING] Add timeout to `gh_api` subprocess — `src/mpy_triage/gh.py:56`
- [ ] [WARNING] Add repo name validation at CLI — `src/mpy_triage/cli.py`
- [ ] [WARNING] Fix `keyword_search` filter asymmetry with `dense_search` — `src/mpy_triage/search.py`
- [ ] [WARNING] Cache Reranker instance across search calls — `src/mpy_triage/search.py:220`
- [ ] [WARNING] Set `discovered_at` timestamp in ground_truth — `src/mpy_triage/crossref.py:247`
- [ ] [WARNING] Standardize logger naming (`log` vs `logger`) — multiple files
- [ ] [WARNING] Add test for `summarize_all` batch processing — `tests/test_summarize.py`
- [ ] [INFO] Fix `EmbeddingConfig.device` type annotation — `src/mpy_triage/config.py:20`
- [ ] [INFO] Remove dead `getattr` calls in `format_human` — `src/mpy_triage/format.py:39-40`
- [ ] [INFO] Add public `config` property to Embedder — `src/mpy_triage/embed.py:218`
- [ ] [INFO] Use title + body for search query — `src/mpy_triage/cli.py:219`
- [ ] [INFO] Add `sort=updated` to list endpoint params — `src/mpy_triage/collect.py:65`
- [ ] [INFO] Restore plugin skills path in plugin.json — `.claude-plugin/plugin.json`
- [ ] [INFO] Add table name assertions for f-string SQL — `src/mpy_triage/assemble.py:99`, `src/mpy_triage/cli.py:291`

## Statistics
| Dimension | Critical | Warning | Info |
|-----------|----------|---------|------|
| Architecture | 1 | 4 | 0 |
| Code Quality | 1 | 4 | 4 |
| Completeness | 0 | 4 | 3 |
| Security | 0 | 4 | 2 |
| **Total** | **2** | **16** | **9** |
