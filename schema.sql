-- mpy-triage Database Schema
-- Raw mirror of GitHub data + processed summaries and embeddings

-- Issues
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY,
    number INTEGER NOT NULL,
    repo TEXT NOT NULL,
    title TEXT,
    body TEXT,
    author TEXT,
    state TEXT,
    state_reason TEXT,
    labels TEXT,  -- JSON array of label names
    milestone TEXT,
    created_at TEXT,
    updated_at TEXT,
    closed_at TEXT,
    UNIQUE(repo, number)
);

-- Pull requests
CREATE TABLE IF NOT EXISTS pull_requests (
    id INTEGER PRIMARY KEY,
    number INTEGER NOT NULL,
    repo TEXT NOT NULL,
    title TEXT,
    body TEXT,
    author TEXT,
    state TEXT,
    draft INTEGER DEFAULT 0,
    labels TEXT,  -- JSON array of label names
    created_at TEXT,
    updated_at TEXT,
    closed_at TEXT,
    merged_at TEXT,
    base_branch TEXT,
    changed_files INTEGER,
    additions INTEGER,
    deletions INTEGER,
    UNIQUE(repo, number)
);

-- PR diffs (separate table for large data)
CREATE TABLE IF NOT EXISTS pr_diffs (
    pr_number INTEGER NOT NULL,
    repo TEXT NOT NULL,
    diff_text TEXT,
    UNIQUE(repo, pr_number)
);

-- Issue and PR discussion comments
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY,
    item_number INTEGER NOT NULL,
    item_type TEXT NOT NULL,  -- 'issue' or 'pull_request'
    repo TEXT NOT NULL,
    author TEXT,
    body TEXT,
    created_at TEXT,
    updated_at TEXT
);

-- Inline code review comments on PRs
CREATE TABLE IF NOT EXISTS review_comments (
    id INTEGER PRIMARY KEY,
    pr_number INTEGER NOT NULL,
    repo TEXT NOT NULL,
    author TEXT,
    body TEXT,
    path TEXT,
    diff_hunk TEXT,
    created_at TEXT
);

-- Cross-references extracted from text and GitHub events
CREATE TABLE IF NOT EXISTS cross_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_number INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    source_repo TEXT NOT NULL,
    target_number INTEGER NOT NULL,
    target_type TEXT,
    target_repo TEXT NOT NULL,
    relationship TEXT NOT NULL,  -- 'fixes', 'closes', 'related', 'duplicate_of', 'references'
    extracted_from TEXT NOT NULL,  -- 'body', 'comment', 'event'
    UNIQUE(source_number, source_repo, target_number, target_repo, relationship)
);

-- Known duplicate/related pairs from GitHub (for evaluation)
CREATE TABLE IF NOT EXISTS ground_truth (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_a_number INTEGER NOT NULL,
    item_a_repo TEXT NOT NULL,
    item_b_number INTEGER NOT NULL,
    item_b_repo TEXT NOT NULL,
    relationship TEXT NOT NULL,  -- 'duplicate', 'related'
    source TEXT NOT NULL,  -- 'state_reason', 'comment', 'event'
    discovered_at TEXT,
    UNIQUE(item_a_repo, item_a_number, item_b_repo, item_b_number)
);

-- Sync state for incremental updates
CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Haiku-generated summaries
CREATE TABLE IF NOT EXISTS summaries (
    item_number INTEGER NOT NULL,
    item_type TEXT NOT NULL,
    repo TEXT NOT NULL,
    model_id TEXT,
    components TEXT,       -- JSON array
    item_category TEXT,    -- bug_report, feature_request, refactor, question, ci_build, documentation
    synopsis TEXT,
    affected_code TEXT,    -- JSON array
    error_signatures TEXT,
    concepts TEXT,         -- JSON array
    created_at TEXT,
    UNIQUE(repo, item_number, item_type)
);

-- Assembled XML for embedding
CREATE TABLE IF NOT EXISTS assembled_xml (
    item_number INTEGER NOT NULL,
    item_type TEXT NOT NULL,
    repo TEXT NOT NULL,
    xml_text TEXT,
    xml_hash TEXT,         -- SHA-256 of xml_text
    has_summary INTEGER DEFAULT 0,
    created_at TEXT,
    UNIQUE(repo, item_number, item_type)
);

-- Embedding model metadata
CREATE TABLE IF NOT EXISTS embedding_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_issues_repo ON issues(repo);
CREATE INDEX IF NOT EXISTS idx_issues_updated ON issues(updated_at);
CREATE INDEX IF NOT EXISTS idx_issues_state ON issues(state);
CREATE INDEX IF NOT EXISTS idx_prs_repo ON pull_requests(repo);
CREATE INDEX IF NOT EXISTS idx_prs_updated ON pull_requests(updated_at);
CREATE INDEX IF NOT EXISTS idx_comments_item ON comments(item_number, item_type, repo);
CREATE INDEX IF NOT EXISTS idx_review_comments_pr ON review_comments(pr_number, repo);
CREATE INDEX IF NOT EXISTS idx_crossref_source ON cross_references(source_number, source_repo);
CREATE INDEX IF NOT EXISTS idx_crossref_target ON cross_references(target_number, target_repo);
CREATE INDEX IF NOT EXISTS idx_summaries_repo ON summaries(repo);
CREATE INDEX IF NOT EXISTS idx_assembled_repo ON assembled_xml(repo);
