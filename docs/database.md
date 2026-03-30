# Database Reference

The database is a single SQLite file at `data/triage.db`. Schema is defined in `schema.sql`.

## Schema Overview

### Raw Mirror Tables

These tables store GitHub data as-is from the `gh` CLI.

| Table | Purpose | Key |
|-------|---------|-----|
| `issues` | Issue metadata: title, body, author, state, labels, milestone | `UNIQUE(repo, number)` |
| `pull_requests` | PR metadata: title, body, author, state, draft, labels, diff stats | `UNIQUE(repo, number)` |
| `pr_diffs` | Full unified diff text per PR (separate table due to size) | `UNIQUE(repo, pr_number)` |
| `comments` | Issue and PR discussion comments | `id` (GitHub ID) |
| `review_comments` | Inline code review comments on PRs, with file path and diff hunk | `id` (GitHub ID) |

### Derived Tables

Built from raw data during collection or processing.

| Table | Purpose | Key |
|-------|---------|-----|
| `cross_references` | Links extracted from text: "Fixes #N", "Duplicate of #N", etc. | `UNIQUE(source_number, source_repo, target_number, target_repo, relationship)` |
| `ground_truth` | Known duplicate/related pairs from GitHub close reasons and comments | `UNIQUE(item_a_repo, item_a_number, item_b_repo, item_b_number)` |
| `summaries` | LLM-generated structured metadata per item | `UNIQUE(repo, item_number, item_type)` |
| `assembled_xml` | Structured XML combining raw data and summaries, used as embedding input | `UNIQUE(repo, item_number, item_type)` |

### Index Tables

Created by the `embed` stage.

| Table | Purpose | Key |
|-------|---------|-----|
| `vec_items` | sqlite-vec virtual table storing vector embeddings | `(item_number, item_type, repo)` |
| `item_fts` | FTS5 full-text index over assembled XML for BM25 keyword search | — |
| `embedding_meta` | Key-value store for model ID, dimensions, record count, build timestamp | `key` |

### Other Tables

| Table | Purpose | Key |
|-------|---------|-----|
| `sync_state` | Checkpoint tracking for incremental collection updates | `key` |
| `eval_summaries` | Summarization evaluation data (separate from production summaries) | `UNIQUE(repo, item_number, item_type, model_id)` |

## Key Relationships

Items are identified by the tuple `(repo, number, item_type)` across tables. There are no foreign key constraints; joins use these columns.

- `comments.item_number` + `comments.item_type` + `comments.repo` join to either `issues` or `pull_requests`
- `review_comments.pr_number` + `review_comments.repo` join to `pull_requests`
- `pr_diffs.pr_number` + `pr_diffs.repo` join to `pull_requests`
- `cross_references` links a source item to a target item, both identified by `(number, repo)`
- `summaries`, `assembled_xml`, and `vec_items` all key on `(repo, item_number, item_type)`

## Indexes

The schema defines indexes on frequently queried columns:

- `issues(repo)`, `issues(updated_at)`, `issues(state)`
- `pull_requests(repo)`, `pull_requests(updated_at)`
- `comments(item_number, item_type, repo)`
- `review_comments(pr_number, repo)`
- `cross_references(source_number, source_repo)`, `cross_references(target_number, target_repo)`
- `summaries(repo)`, `assembled_xml(repo)`

## Size Expectations

The full MicroPython corpus (both repos, all issues/PRs/comments/diffs/summaries/embeddings) produces a database of approximately 350 MB.

## Git LFS Tracking

The database file is tracked with Git LFS. From `.gitattributes`:

```
data/*.db filter=lfs diff=lfs merge=lfs -text
```

The WAL and SHM files (`data/triage.db-wal`, `data/triage.db-shm`) are not tracked in git.

## WAL Mode

The database uses SQLite WAL (Write-Ahead Logging) mode. This allows concurrent reads during long-running write operations (e.g., embedding thousands of items). Multiple readers can access the database while a single writer is active.

WAL produces two sidecar files:
- `triage.db-wal` — write-ahead log
- `triage.db-shm` — shared memory index

These files are transient. They are folded back into the main database on clean connection close or via `PRAGMA wal_checkpoint(TRUNCATE)`.

## Direct Queries

The database can be queried directly with `sqlite3`. Load the sqlite-vec extension if you need to query the vector table.

### Examples

Item counts per repo:

```sql
SELECT repo, COUNT(*) FROM issues GROUP BY repo;
SELECT repo, COUNT(*) FROM pull_requests GROUP BY repo;
```

Find items missing summaries:

```sql
SELECT i.repo, i.number, 'issue' AS item_type
FROM issues i
LEFT JOIN summaries s ON s.repo = i.repo AND s.item_number = i.number AND s.item_type = 'issue'
WHERE s.item_number IS NULL

UNION ALL

SELECT p.repo, p.number, 'pull_request'
FROM pull_requests p
LEFT JOIN summaries s ON s.repo = p.repo AND s.item_number = p.number AND s.item_type = 'pull_request'
WHERE s.item_number IS NULL;
```

Check embedding coverage:

```sql
SELECT value FROM embedding_meta WHERE key = 'model_id';
SELECT value FROM embedding_meta WHERE key = 'record_count';
```

List known duplicates:

```sql
SELECT item_a_repo, item_a_number, item_b_repo, item_b_number, source
FROM ground_truth
WHERE relationship = 'duplicate';
```

Sync state:

```sql
SELECT * FROM sync_state;
```

Full-text search (requires no extensions):

```sql
SELECT item_number, item_type, repo
FROM item_fts
WHERE item_fts MATCH 'SPI DMA HardFault'
ORDER BY rank
LIMIT 10;
```
