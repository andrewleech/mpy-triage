"""HTML rendering for the triage workbench web UI.

Provides markdown rendering, pair detail data fetching, and templates for
the index and pair-detail pages served by the `serve` command.
"""

import html
import json
import sqlite3

import nh3
from markdown_it import MarkdownIt
from mdit_py_plugins.tasklists import tasklists_plugin

from .format import github_url

_CLASSIFICATION_ORDER = {
    "DUPLICATE": 0,
    "LIKELY_DUPLICATE": 1,
    "RELATED": 2,
    "OFF_TOPIC": 3,
    "UNRELATED": 4,
    "": 5,
}

_CLASSIFICATION_LABEL = {
    "DUPLICATE": "Duplicate",
    "LIKELY_DUPLICATE": "Likely Duplicate",
    "RELATED": "Related",
    "OFF_TOPIC": "Off-topic",
    "UNRELATED": "Unrelated",
    "": "Pending",
}

# CSS class slugs (safe for HTML attribute values)
_CLASSIFICATION_SLUG = {
    "DUPLICATE": "dup",
    "LIKELY_DUPLICATE": "likely",
    "RELATED": "related",
    "OFF_TOPIC": "off",
    "UNRELATED": "unrel",
    "": "pending",
}


# --- markdown rendering ----------------------------------------------------

_MD = (
    MarkdownIt("commonmark", {"linkify": True, "html": False, "breaks": True})
    .enable(["table", "strikethrough"])
    .use(tasklists_plugin)
)

# nh3 allowlist: start from defaults, add some GFM-friendly extras.
_ALLOWED_TAGS = set(nh3.ALLOWED_TAGS) | {
    "details",
    "summary",
    "figure",
    "figcaption",
    "input",  # for task list checkboxes
    "del",
    "kbd",
    "hr",
}

_ALLOWED_ATTRS = dict(nh3.ALLOWED_ATTRIBUTES)
_ALLOWED_ATTRS.setdefault("input", set()).update({"type", "checked", "disabled"})
_ALLOWED_ATTRS.setdefault("code", set()).update({"class"})
_ALLOWED_ATTRS.setdefault("span", set()).update({"class"})
_ALLOWED_ATTRS.setdefault("div", set()).update({"class"})
_ALLOWED_ATTRS.setdefault("li", set()).update({"class"})
_ALLOWED_ATTRS.setdefault("ul", set()).update({"class"})
_ALLOWED_ATTRS.setdefault("ol", set()).update({"class"})
_ALLOWED_ATTRS.setdefault("pre", set()).update({"class"})
# Ensure anchors carry rel=noopener when we rewrite them later.
_ALLOWED_ATTRS.setdefault("a", set()).update({"href", "title", "target"})


def render_markdown(text: str | None) -> str:
    """Render GFM markdown to sanitised HTML."""
    if not text:
        return '<p class="empty">(no description)</p>'
    raw = _MD.render(text)
    cleaned = nh3.clean(
        raw,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        link_rel="noopener noreferrer",
    )
    return cleaned


# --- data fetching ---------------------------------------------------------

def _parse_labels(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(val, list):
        return [str(x) for x in val if x]
    return []


def _fetch_item(
    conn: sqlite3.Connection, number: int, item_type: str, repo: str
) -> dict:
    """Load body/metadata for an issue or PR. Returns a dict or {}."""
    table = "pull_requests" if item_type == "pull_request" else "issues"
    if item_type == "pull_request":
        cols = (
            "title, body, author, state, labels, created_at, updated_at, "
            "closed_at, merged_at, draft"
        )
    else:
        cols = (
            "title, body, author, state, state_reason, labels, "
            "created_at, updated_at, closed_at, "
            "NULL as merged_at, 0 as draft"
        )
    row = conn.execute(
        f"SELECT {cols} FROM {table} WHERE repo = ? AND number = ?",
        (repo, number),
    ).fetchone()
    if not row:
        return {}
    d = dict(row)
    d["labels"] = _parse_labels(d.get("labels"))
    d["number"] = number
    d["item_type"] = item_type
    d["repo"] = repo
    # derive a display state (merged PRs report state=closed but have merged_at)
    if item_type == "pull_request" and d.get("merged_at"):
        d["display_state"] = "merged"
    else:
        d["display_state"] = d.get("state") or "unknown"
    return d


def _fetch_comments(
    conn: sqlite3.Connection, number: int, item_type: str, repo: str
) -> list[dict]:
    rows = conn.execute(
        "SELECT author, body, created_at FROM comments "
        "WHERE item_number = ? AND item_type = ? AND repo = ? "
        "ORDER BY created_at ASC",
        (number, item_type, repo),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_pair_detail(conn: sqlite3.Connection, pair: dict) -> dict:
    """Load full data for both sides of a pair."""
    q = _fetch_item(
        conn, pair["query_number"], pair["query_type"], pair["query_repo"]
    )
    c = _fetch_item(
        conn,
        pair["candidate_number"],
        pair["candidate_type"],
        pair["candidate_repo"],
    )
    q_comments = _fetch_comments(
        conn, pair["query_number"], pair["query_type"], pair["query_repo"]
    )
    c_comments = _fetch_comments(
        conn,
        pair["candidate_number"],
        pair["candidate_type"],
        pair["candidate_repo"],
    )
    return {
        "pair": pair,
        "query": q,
        "candidate": c,
        "query_comments": q_comments,
        "candidate_comments": c_comments,
    }


# --- pairs sorting ---------------------------------------------------------

def sort_pairs(results: list[dict]) -> list[dict]:
    """Sort results by classification priority then value_score desc."""
    results.sort(key=lambda r: (
        _CLASSIFICATION_ORDER.get(r.get("classification", ""), 5),
        -r.get("value_score", 0),
    ))
    return results


def suggested_comment(pair: dict) -> str | None:
    """Return the maintainer comment string for this pair, or None."""
    cls = pair.get("classification", "")
    c_num = pair["candidate_number"]
    c_type = pair["candidate_type"]
    kind = "PR" if c_type == "pull_request" else "issue"
    if cls == "DUPLICATE":
        return f"Closing as duplicate of {kind} #{c_num}."
    if cls == "LIKELY_DUPLICATE":
        return (
            f"This may be a duplicate of {kind} #{c_num} — can someone confirm?"
        )
    if cls == "RELATED":
        return f"Related to {kind} #{c_num}."
    return None


# --- HTML helpers ----------------------------------------------------------

def _h(text: str | None) -> str:
    """HTML-escape plain text."""
    return html.escape(text or "", quote=True)


def _fmt_date(s: str | None) -> str:
    """Return YYYY-MM-DD from an ISO8601 string, or empty."""
    if not s:
        return ""
    return s[:10]


# --- CSS -------------------------------------------------------------------

_GRAIN_SVG = (
    "url(\"data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' width='240' height='240'>"
    "<filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.9'"
    " numOctaves='2' seed='7'/><feColorMatrix values='"
    "0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 0.6 0'/></filter>"
    "<rect width='100%25' height='100%25' filter='url(%23n)'/></svg>\")"
)

_FONT_IMPORT = (
    "@import url('https://fonts.googleapis.com/css2?"
    "family=Fraunces:opsz,wght,SOFT@9..144,300..900,0..100"
    "&family=JetBrains+Mono:wght@400;600"
    "&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600"
    "&display=swap');"
)

STYLE_CSS_TEMPLATE = r"""
__FONT_IMPORT__

:root {
  --bg: #f5f1e8;
  --bg-deep: #ebe5d4;
  --bg-raised: #fdfbf5;
  --ink: #1a1a1a;
  --ink-mute: #6b6354;
  --ink-soft: #9b937f;
  --line: #d8d0bd;
  --line-strong: #bfb69a;

  --dup: #2d5a3d;
  --dup-soft: #d7e5dc;
  --likely: #8b4513;
  --likely-soft: #ebdacb;
  --related: #1e4d8b;
  --related-soft: #d2dbe8;
  --off: #9a2a2a;
  --off-soft: #ebd2cf;
  --unrel: #6b6354;
  --unrel-soft: #e4dcc8;

  --accent: var(--dup);
  --accent-soft: var(--dup-soft);

  --chrome-h: 60px;
  --footer-h: 54px;

  --serif: "Fraunces", "Source Serif 4", Georgia, serif;
  --body-serif: "Source Serif 4", "Fraunces", Georgia, serif;
  --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;

  --grain-svg: __GRAIN_SVG__;
}

html, body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: var(--body-serif);
  font-size: 16px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

/* paper grain overlay */
body::before {
  content: "";
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 1000;
  opacity: 0.035;
  mix-blend-mode: multiply;
  background-image: var(--grain-svg);
}

a {
  color: var(--accent);
  text-decoration: underline;
  text-decoration-thickness: 1px;
  text-underline-offset: 2px;
  text-decoration-color: color-mix(in srgb, var(--accent) 40%, transparent);
}
a:hover { text-decoration-color: var(--accent); }

/* ----- classification accent variables per detail page ----- */
body.cls-DUPLICATE        { --accent: var(--dup);     --accent-soft: var(--dup-soft); }
body.cls-LIKELY_DUPLICATE { --accent: var(--likely);  --accent-soft: var(--likely-soft); }
body.cls-RELATED          { --accent: var(--related); --accent-soft: var(--related-soft); }
body.cls-OFF_TOPIC        { --accent: var(--off);     --accent-soft: var(--off-soft); }
body.cls-UNRELATED        { --accent: var(--unrel);   --accent-soft: var(--unrel-soft); }

/* ============ INDEX PAGE ============ */

.index-wrap {
  max-width: 1400px;
  margin: 0 auto;
  padding: 48px 36px 96px;
}

.index-masthead {
  border-bottom: 2px solid var(--ink);
  padding-bottom: 28px;
  margin-bottom: 40px;
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: end;
  gap: 24px;
}

.wordmark {
  font-family: var(--serif);
  font-variation-settings: "opsz" 144, "SOFT" 50;
  font-weight: 400;
  font-size: clamp(40px, 6vw, 72px);
  line-height: 1;
  letter-spacing: -0.015em;
  color: var(--ink);
}
.wordmark em {
  font-style: italic;
  font-variation-settings: "opsz" 144, "SOFT" 100;
  color: var(--ink-mute);
}

.masthead-meta {
  font-family: var(--mono);
  font-size: 11px;
  line-height: 1.6;
  text-align: right;
  color: var(--ink-mute);
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.masthead-meta b { color: var(--ink); font-weight: 600; }
.masthead-meta .chip {
  display: inline-block;
  padding: 2px 7px;
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  margin-left: 6px;
}
.masthead-meta .chip.sonnet { background: #e9daf7; }
.masthead-meta .chip.qwen   { background: #d7e8d2; }

section.group { margin-bottom: 56px; }

.group-head {
  display: flex;
  align-items: baseline;
  gap: 16px;
  border-top: 1px solid var(--line);
  padding-top: 18px;
  margin-bottom: 18px;
}
.group-head .group-num {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--ink-soft);
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.group-head h2 {
  font-family: var(--serif);
  font-weight: 500;
  font-variation-settings: "opsz" 72;
  font-size: 28px;
  margin: 0;
  letter-spacing: -0.01em;
  color: var(--ink);
}
.group-head .group-count {
  font-family: var(--mono);
  color: var(--ink-mute);
  font-size: 13px;
  margin-left: auto;
}
.group-head .rule {
  flex: 1;
  height: 1px;
  background: var(--line);
}

.group[data-cls="DUPLICATE"] .group-head h2,
.group[data-cls="DUPLICATE"] .group-num { color: var(--dup); }
.group[data-cls="LIKELY_DUPLICATE"] .group-head h2,
.group[data-cls="LIKELY_DUPLICATE"] .group-num { color: var(--likely); }
.group[data-cls="RELATED"] .group-head h2,
.group[data-cls="RELATED"] .group-num { color: var(--related); }
.group[data-cls="OFF_TOPIC"] .group-head h2,
.group[data-cls="OFF_TOPIC"] .group-num { color: var(--off); }

.pair-list {
  display: grid;
  grid-template-columns: 1fr;
  gap: 0;
}

.pair-row {
  display: grid;
  grid-template-columns: 56px 1fr 1fr 96px;
  gap: 18px;
  padding: 14px 0;
  border-bottom: 1px solid var(--line);
  text-decoration: none;
  color: var(--ink);
  align-items: baseline;
  transition: background 120ms ease;
}
.pair-row:hover {
  background: var(--bg-raised);
  text-decoration: none;
}
.pair-row:hover .pair-n {
  color: var(--accent);
}
.pair-row .pair-n {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--ink-soft);
  letter-spacing: 0.03em;
}
.pair-row .pair-query .num,
.pair-row .pair-candidate .num {
  font-family: var(--mono);
  font-weight: 600;
  color: var(--accent);
  margin-right: 8px;
}
.pair-row .pair-query .title,
.pair-row .pair-candidate .title {
  font-family: var(--body-serif);
  font-size: 15px;
  color: var(--ink);
}
.pair-row .pair-query .kind,
.pair-row .pair-candidate .kind {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--ink-mute);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-right: 6px;
}
.pair-row .pair-score {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--ink-mute);
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.pair-row .src-badge {
  display: inline-block;
  width: 14px;
  height: 14px;
  border-radius: 2px;
  font-family: var(--mono);
  font-size: 9px;
  font-weight: 700;
  line-height: 14px;
  text-align: center;
  color: var(--ink);
  margin-left: 6px;
  vertical-align: middle;
}
.src-sonnet { background: #d7aefb; }
.src-qwen   { background: #a5d6a7; }

.group[data-cls="DUPLICATE"] .pair-row .num { color: var(--dup); }
.group[data-cls="LIKELY_DUPLICATE"] .pair-row .num { color: var(--likely); }
.group[data-cls="RELATED"] .pair-row .num { color: var(--related); }
.group[data-cls="OFF_TOPIC"] .pair-row .num { color: var(--off); }

/* ============ DETAIL PAGE ============ */

body.detail {
  overflow: hidden;
  height: 100vh;
}

.detail-layout {
  display: grid;
  grid-template-rows: var(--chrome-h) 1fr var(--footer-h);
  height: 100vh;
}

/* --- chrome (top bar) --- */

.chrome {
  position: relative;
  background: var(--bg-deep);
  border-bottom: 1px solid var(--line-strong);
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  padding: 0 24px;
  gap: 20px;
  z-index: 10;
}
.chrome::before {
  content: "";
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 3px;
  background: var(--accent);
}

.chrome-left {
  display: flex;
  align-items: center;
  gap: 14px;
  font-family: var(--mono);
  font-size: 12px;
  color: var(--ink-mute);
}
.chrome .back {
  font-family: var(--serif);
  font-variation-settings: "opsz" 9;
  font-weight: 500;
  font-size: 15px;
  color: var(--ink);
  text-decoration: none;
  letter-spacing: -0.005em;
  border-right: 1px solid var(--line-strong);
  padding-right: 14px;
}
.chrome .back:hover { color: var(--accent); text-decoration: none; }
.chrome .pair-id {
  font-family: var(--mono);
  color: var(--ink);
  font-weight: 600;
  letter-spacing: 0.02em;
}
.chrome .pair-id .arrow { color: var(--ink-soft); padding: 0 4px; }

.chrome-center {
  display: flex;
  align-items: center;
  gap: 14px;
  justify-self: center;
}
.chrome .classification {
  font-family: var(--serif);
  font-variation-settings: "opsz" 144, "SOFT" 20;
  font-weight: 500;
  font-size: 20px;
  color: var(--accent);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  border-top: 1px solid var(--accent);
  border-bottom: 1px solid var(--accent);
  padding: 3px 14px;
}
.chrome .confidence {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--ink-mute);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.chrome .score {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--ink-mute);
  font-variant-numeric: tabular-nums;
}

.chrome-right {
  display: flex;
  align-items: center;
  gap: 10px;
  justify-self: end;
}
.chrome button {
  font-family: var(--mono);
  font-size: 11px;
  padding: 7px 14px;
  background: var(--bg-raised);
  border: 1px solid var(--line-strong);
  color: var(--ink);
  cursor: pointer;
  letter-spacing: 0.02em;
  transition: all 120ms ease;
}
.chrome button.primary {
  background: var(--accent);
  color: var(--bg);
  border-color: var(--accent);
}
.chrome button:hover { background: var(--accent); color: var(--bg); border-color: var(--accent); }
.chrome button.copied { background: var(--accent); color: var(--bg); }
.chrome button .kbd {
  display: inline-block;
  margin-left: 8px;
  padding: 1px 5px;
  border: 1px solid currentColor;
  border-radius: 2px;
  font-size: 9px;
  opacity: 0.7;
}

/* --- split panes --- */

.split {
  display: grid;
  grid-template-columns: 1fr 1fr;
  overflow: hidden;
  position: relative;
}

.split::before {
  content: "";
  position: absolute;
  top: 0;
  bottom: 0;
  left: 50%;
  width: 1px;
  background: var(--accent);
  opacity: 0.35;
  transform: translateX(-0.5px);
  pointer-events: none;
}

.pane {
  overflow-y: auto;
  overflow-x: hidden;
  padding: 36px 44px 80px;
  position: relative;
  scrollbar-color: var(--line-strong) transparent;
  scrollbar-width: thin;
}
.pane::-webkit-scrollbar { width: 10px; }
.pane::-webkit-scrollbar-track { background: transparent; }
.pane::-webkit-scrollbar-thumb {
  background: var(--line-strong);
  border-radius: 6px;
  border: 2px solid var(--bg);
}

.pane-query { border-right: none; }

.kicker {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--ink-soft);
  text-transform: uppercase;
  letter-spacing: 0.12em;
  margin-bottom: 8px;
}
.pane-query .kicker::before { content: "◆ "; color: var(--accent); }
.pane-candidate .kicker::before { content: "◇ "; color: var(--accent); }

.pane h1.item-title {
  font-family: var(--serif);
  font-variation-settings: "opsz" 144, "SOFT" 30, "wght" 450;
  font-size: clamp(24px, 2.8vw, 36px);
  line-height: 1.15;
  letter-spacing: -0.015em;
  margin: 0 0 16px;
  color: var(--ink);
}
.pane h1.item-title a {
  color: inherit;
  text-decoration: none;
}
.pane h1.item-title a:hover { color: var(--accent); }

.pane .item-meta {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--ink-mute);
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 20px;
}
.pane .item-meta .state {
  font-weight: 600;
  padding: 2px 7px;
  border-radius: 10px;
  font-size: 10px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.state.open   { background: #d7e8d2; color: #1a3f18; }
.state.closed { background: #ebd2cf; color: #5b1919; }
.state.merged { background: #e6d7f7; color: #3d1963; }
.state.draft  { background: #ebe5d4; color: #6b6354; }
.state.unknown { background: var(--line); color: var(--ink-mute); }

.pane .item-labels {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 28px;
}
.pane .item-labels .label {
  font-family: var(--mono);
  font-size: 10px;
  padding: 2px 8px;
  background: var(--bg-raised);
  border: 1px solid var(--line);
  color: var(--ink-mute);
  border-radius: 10px;
  letter-spacing: 0.02em;
}

.pane .body {
  font-family: var(--body-serif);
  font-size: 15px;
  line-height: 1.65;
  color: var(--ink);
}
.pane .body p { margin: 0 0 1em; }
.pane .body > p:first-of-type::first-letter {
  font-family: var(--serif);
  font-variation-settings: "opsz" 144, "wght" 500;
  float: left;
  font-size: 3.4em;
  line-height: 0.85;
  padding: 0.04em 0.08em 0 0;
  color: var(--accent);
}
.pane .body h1, .pane .body h2, .pane .body h3 {
  font-family: var(--serif);
  font-variation-settings: "opsz" 72, "wght" 500;
  letter-spacing: -0.01em;
  margin: 1.4em 0 0.5em;
}
.pane .body h1 { font-size: 1.4em; }
.pane .body h2 { font-size: 1.25em; }
.pane .body h3 { font-size: 1.1em; }
.pane .body code {
  font-family: var(--mono);
  font-size: 0.88em;
  background: var(--bg-raised);
  padding: 1px 5px;
  border-radius: 3px;
  border: 1px solid var(--line);
}
.pane .body pre {
  font-family: var(--mono);
  background: #faf6ea;
  border: 1px solid var(--line);
  border-left: 3px solid var(--accent);
  padding: 12px 14px;
  overflow-x: auto;
  font-size: 12.5px;
  line-height: 1.5;
  border-radius: 2px;
}
.pane .body pre code {
  background: transparent;
  border: 0;
  padding: 0;
  font-size: inherit;
}
.pane .body blockquote {
  margin: 1em 0;
  padding: 0.2em 16px;
  border-left: 3px solid var(--line-strong);
  color: var(--ink-mute);
  font-style: italic;
}
.pane .body ul, .pane .body ol { padding-left: 1.5em; }
.pane .body li { margin: 0.2em 0; }
.pane .body img { max-width: 100%; height: auto; border: 1px solid var(--line); }
.pane .body table {
  border-collapse: collapse;
  font-size: 13px;
  margin: 1em 0;
}
.pane .body th, .pane .body td {
  border: 1px solid var(--line);
  padding: 6px 10px;
  text-align: left;
}
.pane .body th { background: var(--bg-raised); }
.pane .body hr {
  border: 0;
  border-top: 1px solid var(--line);
  margin: 1.6em 0;
}

/* comments */
.comments {
  margin-top: 48px;
  border-top: 1px solid var(--line);
  padding-top: 24px;
}
.comments-head {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--ink-soft);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin-bottom: 20px;
}
.comment {
  margin-bottom: 28px;
  padding-left: 16px;
  border-left: 2px solid var(--line);
}
.comment:hover { border-left-color: var(--accent); }
.comment .comment-meta {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--ink-mute);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-bottom: 8px;
}
.comment .comment-meta .author { color: var(--ink); font-weight: 600; }
.comment .comment-body {
  font-family: var(--body-serif);
  font-size: 14px;
  line-height: 1.6;
}
.comment .comment-body p { margin: 0 0 0.8em; }
.comment .comment-body pre {
  font-family: var(--mono);
  font-size: 11.5px;
  background: var(--bg-raised);
  border: 1px solid var(--line);
  padding: 8px 10px;
  overflow-x: auto;
}
.comment .comment-body code {
  font-family: var(--mono);
  font-size: 0.88em;
  background: var(--bg-raised);
  padding: 1px 4px;
  border-radius: 3px;
}

.empty {
  font-family: var(--body-serif);
  font-style: italic;
  color: var(--ink-soft);
}

/* --- footer nav --- */

.footer {
  background: var(--bg-deep);
  border-top: 1px solid var(--line-strong);
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  padding: 0 24px;
  position: relative;
  z-index: 10;
}
.footer .nav-prev, .footer .nav-next {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--ink);
  text-decoration: none;
  letter-spacing: 0.04em;
  display: flex;
  align-items: center;
  gap: 10px;
}
.footer .nav-prev { justify-self: start; }
.footer .nav-next { justify-self: end; }
.footer .nav-prev:hover, .footer .nav-next:hover { color: var(--accent); }
.footer .nav-prev[aria-disabled="true"],
.footer .nav-next[aria-disabled="true"] {
  color: var(--ink-soft);
  pointer-events: none;
}
.footer .arrow {
  font-family: var(--serif);
  font-size: 20px;
  font-weight: 400;
}
.footer .kbd {
  display: inline-block;
  padding: 2px 6px;
  border: 1px solid var(--line-strong);
  background: var(--bg-raised);
  border-radius: 2px;
  font-size: 9px;
  color: var(--ink-mute);
}
.footer .position {
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  font-size: 14px;
  color: var(--ink);
  letter-spacing: 0.04em;
}
.footer .position .cur { color: var(--accent); font-weight: 600; }
.footer .position .sep { color: var(--ink-soft); padding: 0 6px; }
.footer .position .tot { color: var(--ink-mute); }

/* reasoning drawer */

.drawer {
  position: fixed;
  top: var(--chrome-h);
  right: 0;
  width: 360px;
  max-width: 90vw;
  bottom: var(--footer-h);
  background: var(--bg-raised);
  border-left: 1px solid var(--line-strong);
  box-shadow: -6px 0 24px rgba(20, 20, 20, 0.06);
  transform: translateX(100%);
  transition: transform 220ms cubic-bezier(0.4, 0, 0.2, 1);
  z-index: 20;
  overflow-y: auto;
  padding: 28px 28px 48px;
}
.drawer.open { transform: translateX(0); }
.drawer h2 {
  font-family: var(--serif);
  font-variation-settings: "opsz" 72, "wght" 500;
  font-size: 18px;
  margin: 0 0 6px;
  color: var(--ink);
}
.drawer .drawer-meta {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--ink-soft);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-bottom: 18px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.drawer .drawer-body {
  font-family: var(--body-serif);
  font-size: 14px;
  line-height: 1.6;
  color: var(--ink);
}
.drawer .drawer-body p { margin: 0 0 1em; }
.drawer .suggested {
  margin-top: 20px;
  padding: 14px 16px;
  background: var(--bg);
  border: 1px dashed var(--line-strong);
  border-left: 3px solid var(--accent);
  font-family: var(--body-serif);
  font-size: 13px;
  color: var(--ink);
}
.drawer .suggested-head {
  font-family: var(--mono);
  font-size: 9px;
  color: var(--ink-soft);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin-bottom: 6px;
}

/* keyboard help overlay */

.help-overlay {
  position: fixed;
  inset: 0;
  background: rgba(26, 26, 26, 0.6);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 100;
  backdrop-filter: blur(3px);
}
.help-overlay.open { display: flex; }
.help-card {
  background: var(--bg);
  border: 1px solid var(--line-strong);
  padding: 36px 44px;
  max-width: 480px;
  box-shadow: 0 30px 60px rgba(20, 20, 20, 0.2);
}
.help-card h2 {
  font-family: var(--serif);
  font-variation-settings: "opsz" 144, "wght" 500;
  margin: 0 0 20px;
  font-size: 28px;
  letter-spacing: -0.015em;
}
.help-card dl {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 8px 24px;
  font-family: var(--body-serif);
  font-size: 14px;
  margin: 0;
}
.help-card dt {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--ink);
}
.help-card dt kbd {
  display: inline-block;
  padding: 2px 8px;
  border: 1px solid var(--line-strong);
  background: var(--bg-raised);
  border-radius: 3px;
  font-family: var(--mono);
  font-size: 11px;
}
.help-card dd {
  margin: 0;
  color: var(--ink-mute);
  align-self: center;
}
.help-card .hint {
  margin-top: 24px;
  font-family: var(--mono);
  font-size: 10px;
  color: var(--ink-soft);
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

/* toast / copy confirmation */

.toast {
  position: fixed;
  bottom: calc(var(--footer-h) + 20px);
  left: 50%;
  transform: translateX(-50%) translateY(20px);
  padding: 10px 22px;
  background: var(--ink);
  color: var(--bg);
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0.04em;
  border-radius: 2px;
  opacity: 0;
  transition: opacity 180ms ease, transform 180ms ease;
  pointer-events: none;
  z-index: 50;
}
.toast.show {
  opacity: 1;
  transform: translateX(-50%) translateY(0);
}

/* ================================================================ */
/*  MOBILE TAB LAYOUT                                                 */
/*                                                                    */
/*  Two tiered breakpoints:                                           */
/*    - phone (<= 720px): tab-switched panes, card-style index rows   */
/*    - tablet portrait (721–960px): 50/50 vertical stack             */
/* ================================================================ */

/* tab switcher is hidden by default (desktop side-by-side) */
.pane-tabs { display: none; }

/* --- tablet portrait: vertical 50/50 stack --- */

@media (max-width: 960px) and (min-width: 721px),
       (max-aspect-ratio: 1/1) and (min-width: 721px) {
  .split {
    grid-template-columns: 1fr;
    grid-template-rows: 1fr 1fr;
  }
  .split::before {
    top: 50%;
    bottom: auto;
    left: 0;
    right: 0;
    width: 100%;
    height: 1px;
    transform: translateY(-0.5px);
  }
  .pane-query {
    border-right: none;
    border-bottom: 1px solid var(--line);
  }
  .pane { padding: 28px 30px 60px; }
}

/* --- phones: tab switcher --- */

@media (max-width: 720px) {
  :root {
    --footer-h: 58px;
  }

  /* --- detail layout: chrome (auto) / main (fills) / footer --- */
  .detail-layout {
    grid-template-rows: auto 1fr var(--footer-h);
  }

  /* compact 2-row chrome */
  .chrome {
    display: flex;
    flex-wrap: wrap;
    padding: 10px 14px 12px;
    gap: 6px 14px;
    align-items: baseline;
    min-height: 0;
  }
  .chrome::before { height: 2px; }
  .chrome-left {
    flex: 1 1 auto;
    display: flex;
    align-items: baseline;
    gap: 10px;
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
  }
  .chrome .back {
    border-right: none;
    padding-right: 0;
    font-size: 13px;
  }
  .chrome .pair-id { font-size: 11px; letter-spacing: 0.01em; }
  .chrome-center {
    flex: 1 1 100%;
    order: 3;
    justify-self: start;
    display: flex;
    align-items: baseline;
    gap: 10px;
    padding-top: 2px;
  }
  .chrome .classification {
    font-size: 14px;
    padding: 2px 10px;
    letter-spacing: 0.06em;
  }
  .chrome .confidence, .chrome .score { font-size: 9px; }
  .chrome-right {
    flex: 0 0 auto;
    order: 2;
    gap: 6px;
    justify-self: end;
  }
  .chrome button {
    font-size: 10px;
    padding: 6px 10px;
  }
  .chrome button .kbd { display: none; }

  /* --- tab switcher --- */

  .pane-tabs {
    display: grid;
    grid-template-columns: 1fr 1fr;
    background: var(--bg-deep);
    border-bottom: 1px solid var(--line-strong);
    border-top: 1px solid var(--line);
    position: relative;
    z-index: 8;
  }
  .pane-tab {
    appearance: none;
    background: transparent;
    border: 0;
    padding: 12px 10px 10px;
    font-family: var(--mono);
    font-size: 10px;
    color: var(--ink-mute);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    font-weight: 600;
    min-height: 44px;  /* tap target */
    -webkit-tap-highlight-color: transparent;
    transition: color 120ms ease, border-color 120ms ease;
  }
  .pane-tab .mark {
    font-family: var(--serif);
    font-size: 14px;
    color: var(--accent);
  }
  .pane-tab .ref {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--ink);
    font-weight: 700;
  }
  .pane-tab.active {
    color: var(--accent);
    border-bottom-color: var(--accent);
  }
  .pane-tab.active .ref { color: var(--accent); }
  .pane-tab + .pane-tab {
    border-left: 1px solid var(--line);
  }

  /* single-pane display */
  .split {
    grid-template-columns: 1fr;
    grid-template-rows: auto 1fr;
    overflow: hidden;
  }
  .split::before { display: none; }
  .pane-tabs { grid-row: 1; grid-column: 1; }
  .pane-query, .pane-candidate {
    grid-row: 2;
    grid-column: 1;
    display: none;
    padding: 22px 18px 80px;
    border: 0;
  }
  .split[data-active="query"] .pane-query { display: block; }
  .split[data-active="candidate"] .pane-candidate { display: block; }
  .split:not([data-active="candidate"]) .pane-query { display: block; }

  /* per-pane header density */
  .pane .kicker { font-size: 9px; }
  .pane h1.item-title {
    font-size: 22px;
    line-height: 1.18;
    margin: 0 0 12px;
  }
  .pane .item-meta {
    font-size: 10px;
    gap: 10px 12px;
    padding-bottom: 10px;
    margin-bottom: 14px;
  }
  .pane .item-labels { margin-bottom: 18px; }
  .pane .item-labels .label { font-size: 9px; padding: 1px 6px; }
  .pane .body { font-size: 14.5px; line-height: 1.6; }
  .pane .body > p:first-of-type::first-letter {
    font-size: 2.5em;
    padding: 0.03em 0.08em 0 0;
  }
  .pane .body pre { font-size: 11.5px; padding: 10px 12px; }
  .pane .body h1 { font-size: 1.3em; }
  .pane .body h2 { font-size: 1.18em; }
  .pane .body h3 { font-size: 1.06em; }

  .comments { margin-top: 36px; padding-top: 18px; }
  .comment { margin-bottom: 22px; padding-left: 12px; }
  .comment .comment-body { font-size: 13.5px; }

  /* --- footer: bigger touch targets --- */
  .footer { padding: 0 14px; }
  .footer .nav-prev, .footer .nav-next {
    font-size: 11px;
    padding: 8px 10px;
    min-height: 44px;
    min-width: 44px;
  }
  .footer .position { font-size: 13px; }
  .footer .kbd { display: none; }

  /* --- drawer: bottom sheet instead of side panel --- */
  .drawer {
    top: auto;
    right: 0;
    left: 0;
    width: 100%;
    max-width: none;
    bottom: var(--footer-h);
    max-height: 65vh;
    border-left: 0;
    border-top: 1px solid var(--line-strong);
    box-shadow: 0 -6px 24px rgba(20, 20, 20, 0.08);
    transform: translateY(100%);
    padding: 22px 20px 48px;
  }
  .drawer.open { transform: translateY(0); }

  /* --- help overlay: smaller card --- */
  .help-card { padding: 26px 28px; max-width: 320px; }
  .help-card h2 { font-size: 22px; }
  .help-card dl { font-size: 13px; gap: 6px 16px; }

  /* ================================================================ */
  /*  Index page, phone                                                 */
  /* ================================================================ */

  body.index { overflow-x: hidden; }
  .index-wrap { padding: 28px 18px 72px; }
  .index-masthead {
    grid-template-columns: 1fr;
    gap: 14px;
    padding-bottom: 22px;
    margin-bottom: 30px;
  }
  .wordmark { font-size: 40px; letter-spacing: -0.02em; }
  .masthead-meta { text-align: left; font-size: 10px; line-height: 1.8; }
  .masthead-meta .chip { margin-left: 0; margin-right: 4px; }

  section.group { margin-bottom: 38px; }
  .group-head {
    flex-wrap: wrap;
    gap: 8px;
    padding-top: 14px;
    margin-bottom: 12px;
  }
  .group-head h2 { font-size: 22px; letter-spacing: -0.01em; }
  .group-head .group-count {
    flex: 1 1 100%;
    margin-left: 0;
    font-size: 11px;
  }
  .group-head .rule { display: none; }

  /* three-line card rows */
  .pair-row {
    grid-template-columns: 1fr auto;
    grid-template-rows: auto auto auto;
    gap: 3px 12px;
    padding: 14px 0 16px;
  }
  .pair-row .pair-n {
    grid-column: 1;
    grid-row: 1;
    font-size: 10px;
  }
  .pair-row .pair-score {
    grid-column: 2;
    grid-row: 1;
    font-size: 11px;
  }
  .pair-row .pair-query {
    grid-column: 1 / -1;
    grid-row: 2;
    display: block;
  }
  .pair-row .pair-query .title,
  .pair-row .pair-candidate .title {
    font-size: 14px;
    line-height: 1.35;
  }
  .pair-row .pair-query .kind,
  .pair-row .pair-candidate .kind {
    font-size: 9px;
  }
  .pair-row .pair-candidate {
    grid-column: 1 / -1;
    grid-row: 3;
    display: block;
    position: relative;
    padding-left: 18px;
    margin-top: 2px;
  }
  .pair-row .pair-candidate::before {
    content: "↳";
    position: absolute;
    left: 3px;
    top: 0;
    color: var(--ink-soft);
    font-family: var(--serif);
  }
}

@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; animation: none !important; }
}
"""

STYLE_CSS = STYLE_CSS_TEMPLATE.replace(
    "__FONT_IMPORT__", _FONT_IMPORT
).replace("__GRAIN_SVG__", _GRAIN_SVG)


# --- templates -------------------------------------------------------------

def _build_head(
    title: str,
    inline_css: bool = False,
    css_href: str = "/static/style.css",
) -> str:
    """Build the <head> block.

    inline_css=True embeds the full stylesheet in a <style> tag.
    Otherwise links to css_href.
    """
    escaped_title = _h(title)
    if inline_css:
        return (
            "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
            "<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{escaped_title}</title>\n"
            f"<style>{STYLE_CSS}</style>\n"
            "</head>\n"
        )
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{escaped_title}</title>\n"
        f"<link rel=\"stylesheet\" href=\"{_h(css_href)}\">\n"
        "</head>\n"
    )


def _group_titles() -> dict:
    return {
        "DUPLICATE": "Duplicates",
        "LIKELY_DUPLICATE": "Likely Duplicates",
        "RELATED": "Related",
        "OFF_TOPIC": "Off-topic",
        "UNRELATED": "Unrelated",
        "": "Pending",
    }


def _group_subtitles() -> dict:
    return {
        "DUPLICATE": "Close the issue side.",
        "LIKELY_DUPLICATE": "Confirm before closing.",
        "RELATED": "Linked but not resolved.",
        "OFF_TOPIC": "Spam, wrong repo, or not actionable.",
        "UNRELATED": "Search false positives.",
    }


def render_index_html(
    pairs: list[dict],
    inline_css: bool = False,
    css_href: str = "/static/style.css",
    pair_href_fmt: str = "/pair/{n}",
) -> str:
    """Render the index page.

    pair_href_fmt: format string for pair links; {n} is replaced with the
    1-indexed pair number.  Use "/pair/{n}" for the live server and
    "pair/{n}.html" for a static directory export.
    """
    sonnet_n = sum(1 for r in pairs if r.get("assessment_source") == "sonnet")
    qwen_n = sum(1 for r in pairs if r.get("assessment_source") == "qwen")
    unique_issues = len({r["query_number"] for r in pairs})

    # Group by classification
    groups: dict[str, list[tuple[int, dict]]] = {}
    for idx, r in enumerate(pairs):
        groups.setdefault(r.get("classification", ""), []).append((idx, r))

    titles = _group_titles()
    subtitles = _group_subtitles()

    # Masthead totals
    group_counts = {cls: len(items) for cls, items in groups.items()}

    sections: list[str] = []
    order = ["DUPLICATE", "LIKELY_DUPLICATE", "RELATED", "OFF_TOPIC", ""]
    group_num = 0
    for cls in order:
        items = groups.get(cls, [])
        if not items:
            continue
        group_num += 1
        rows: list[str] = []
        for idx, r in items:
            n = idx + 1  # 1-indexed
            q_num = r["query_number"]
            q_title = _h(r.get("query_title", ""))
            c_num = r["candidate_number"]
            c_title = _h(r.get("candidate_title", ""))
            c_kind = "PR" if r.get("candidate_type") == "pull_request" else "Issue"
            score = r.get("value_score", 0)
            src = r.get("assessment_source", "")
            src_badge = ""
            if src == "sonnet":
                src_badge = '<span class="src-badge src-sonnet" title="Sonnet">S</span>'
            elif src == "qwen":
                src_badge = '<span class="src-badge src-qwen" title="Qwen">Q</span>'
            rows.append(
                f'<a class="pair-row" href="{pair_href_fmt.format(n=n)}">'
                f'<span class="pair-n">{n:04d}</span>'
                f'<span class="pair-query">'
                f'<span class="kind">Issue</span>'
                f'<span class="num">#{q_num}</span>'
                f'<span class="title">{q_title}</span></span>'
                f'<span class="pair-candidate">'
                f'<span class="kind">{c_kind}</span>'
                f'<span class="num">#{c_num}</span>'
                f'<span class="title">{c_title}</span></span>'
                f'<span class="pair-score">{score:.3f}{src_badge}</span>'
                f"</a>"
            )

        sections.append(
            f'<section class="group" data-cls="{cls}">'
            f'<header class="group-head">'
            f'<span class="group-num">§ {group_num:02d}</span>'
            f'<h2>{titles.get(cls, cls)}</h2>'
            f'<span class="group-count">'
            f'<em>{subtitles.get(cls, "")}</em> · {len(items)} pairs'
            f'</span>'
            f'</header>'
            f'<div class="pair-list">'
            + "\n".join(rows)
            + "</div></section>"
        )

    body = f"""
<body class="index">
<div class="index-wrap">
  <header class="index-masthead">
    <div class="wordmark">mpy<em>·</em>triage</div>
    <div class="masthead-meta">
      <div><b>{len(pairs)}</b> pairs · <b>{unique_issues}</b> open issues</div>
      <div>
        sources:
        <span class="chip sonnet">S Sonnet <b>{sonnet_n}</b></span>
        <span class="chip qwen">Q Qwen <b>{qwen_n}</b></span>
      </div>
      <div>
        dup <b>{group_counts.get("DUPLICATE", 0)}</b> ·
        likely <b>{group_counts.get("LIKELY_DUPLICATE", 0)}</b> ·
        rel <b>{group_counts.get("RELATED", 0)}</b> ·
        off <b>{group_counts.get("OFF_TOPIC", 0)}</b>
      </div>
    </div>
  </header>
  {"".join(sections)}
</div>
</body>
</html>
"""
    head = _build_head("mpy-triage · index", inline_css, css_href=css_href)
    return head + body


def render_detail_html(
    conn: sqlite3.Connection,
    pairs: list[dict],
    index: int,
    css_href: str = "/static/style.css",
    index_href: str = "/",
    pair_href_fmt: str = "/pair/{n}",
) -> str:
    """Render a single pair detail page. `index` is 0-based."""
    if not 0 <= index < len(pairs):
        raise IndexError(index)

    pair = pairs[index]
    detail = fetch_pair_detail(conn, pair)
    q = detail["query"]
    c = detail["candidate"]
    q_comments = detail["query_comments"]
    c_comments = detail["candidate_comments"]

    cls = pair.get("classification", "")
    cls_label = _CLASSIFICATION_LABEL.get(cls, cls or "Pending")
    confidence = pair.get("confidence") or ""
    score = pair.get("value_score", 0)

    q_num = pair["query_number"]
    c_num = pair["candidate_number"]
    q_type = pair.get("query_type", "issue")
    c_type = pair.get("candidate_type", "issue")
    q_kind_short = "PR" if q_type == "pull_request" else "Issue"
    c_kind_short = "PR" if c_type == "pull_request" else "Issue"

    position = f"{index + 1:04d}"
    total = f"{len(pairs):04d}"

    prev_index = index - 1 if index > 0 else None
    next_index = index + 1 if index < len(pairs) - 1 else None

    suggested = suggested_comment(pair)
    suggested_js = json.dumps(suggested) if suggested else "null"

    reasoning = pair.get("reasoning") or ""
    suggested_action = pair.get("suggested_action") or ""
    source = pair.get("assessment_source") or ""
    src_chip = ""
    if source == "sonnet":
        src_chip = '<span class="src-badge src-sonnet">S</span> Sonnet'
    elif source == "qwen":
        src_chip = '<span class="src-badge src-qwen">Q</span> Qwen'

    def _pane_html(
        side: str,
        item: dict,
        comments: list[dict],
        label_kicker: str,
        item_url: str,
    ) -> str:
        if not item:
            return (
                f'<section class="pane pane-{side}">'
                f'<div class="kicker">{label_kicker}</div>'
                f'<h1 class="item-title">(item not found in database)</h1>'
                f"</section>"
            )
        title = _h(item.get("title", "") or "(no title)")
        state = item.get("display_state", "unknown")
        author = _h(item.get("author") or "")
        created = _fmt_date(item.get("created_at"))
        updated = _fmt_date(item.get("updated_at"))

        labels_html = ""
        if item.get("labels"):
            chips = "".join(
                f'<span class="label">{_h(lb)}</span>' for lb in item["labels"]
            )
            labels_html = f'<div class="item-labels">{chips}</div>'

        body_html = render_markdown(item.get("body"))

        comments_html = ""
        if comments:
            cblocks = []
            for cm in comments:
                cblocks.append(
                    f'<article class="comment">'
                    f'<div class="comment-meta">'
                    f'<span class="author">{_h(cm.get("author", ""))}</span>'
                    f' · {_fmt_date(cm.get("created_at"))}'
                    f"</div>"
                    f'<div class="comment-body">{render_markdown(cm.get("body"))}</div>'
                    f"</article>"
                )
            comments_html = (
                f'<section class="comments">'
                f'<div class="comments-head">{len(comments)} comment'
                f'{"s" if len(comments) != 1 else ""}</div>'
                + "".join(cblocks)
                + "</section>"
            )

        title_link = (
            f'<a href="{item_url}" target="_blank" rel="noopener">{title}</a>'
        )
        return (
            f'<section class="pane pane-{side}">'
            f'<div class="kicker">{label_kicker}</div>'
            f'<h1 class="item-title">{title_link}</h1>'
            f'<div class="item-meta">'
            f'<span class="state {state}">{state}</span>'
            + (f"<span>by {author}</span>" if author else "")
            + (f"<span>opened {created}</span>" if created else "")
            + (f"<span>updated {updated}</span>" if updated else "")
            + f'</div>{labels_html}<div class="body">{body_html}</div>{comments_html}'
            f"</section>"
        )

    q_kicker_kind = "ISSUE" if pair.get("query_type") == "issue" else "PULL REQUEST"
    c_kicker_kind = "ISSUE" if c_type == "issue" else "PULL REQUEST"

    q_url = pair.get("query_url") or github_url(
        pair["query_repo"], pair["query_type"], q_num
    )
    c_url = pair.get("candidate_url") or github_url(
        pair["candidate_repo"], c_type, c_num
    )

    q_pane = _pane_html(
        "query", q, q_comments, f"QUERY · {q_kicker_kind}", q_url
    )
    c_pane = _pane_html(
        "candidate", c, c_comments, f"CANDIDATE · {c_kicker_kind}", c_url
    )

    prev_href = (
        pair_href_fmt.format(n=prev_index + 1) if prev_index is not None else None
    )
    next_href = (
        pair_href_fmt.format(n=next_index + 1) if next_index is not None else None
    )
    prev_attr = f'href="{prev_href}"' if prev_href else 'aria-disabled="true"'
    next_attr = f'href="{next_href}"' if next_href else 'aria-disabled="true"'

    copy_btn = ""
    if suggested:
        copy_btn = (
            '<button class="primary" id="copy-btn" type="button">'
            'Copy comment <span class="kbd">c</span></button>'
        )

    reasoning_html = (
        render_markdown(reasoning)
        if reasoning
        else '<p class="empty">(no reasoning recorded)</p>'
    )
    suggested_panel = ""
    if suggested_action:
        suggested_panel = (
            '<div class="suggested">'
            '<div class="suggested-head">Suggested action</div>'
            f"{_h(suggested_action)}"
            "</div>"
        )

    body = f"""
<body class="detail cls-{cls}">
<div class="detail-layout">

  <header class="chrome">
    <div class="chrome-left">
      <a class="back" href="{index_href}">← index</a>
      <span class="pair-id">#{q_num}<span class="arrow">↔</span>{c_kind_short} #{c_num}</span>
    </div>
    <div class="chrome-center">
      <span class="classification">{cls_label}</span>
      {f'<span class="confidence">· {confidence}</span>' if confidence else ""}
      <span class="score">· value {score:.3f}</span>
    </div>
    <div class="chrome-right">
      <button id="reasoning-btn" type="button">Reasoning <span class="kbd">r</span></button>
      {copy_btn}
    </div>
  </header>

  <main class="split" data-active="query">
    <nav class="pane-tabs" role="tablist" aria-label="Pane switcher">
      <button class="pane-tab active" type="button" data-pane="query"
              role="tab" aria-selected="true">
        <span class="mark">◆</span>
        <span>Query</span>
        <span class="ref">{q_kind_short} #{q_num}</span>
      </button>
      <button class="pane-tab" type="button" data-pane="candidate"
              role="tab" aria-selected="false">
        <span class="mark">◇</span>
        <span>Candidate</span>
        <span class="ref">{c_kind_short} #{c_num}</span>
      </button>
    </nav>
    {q_pane}
    {c_pane}
  </main>

  <nav class="footer">
    <a class="nav-prev" {prev_attr}>
      <span class="kbd">k</span>
      <span class="arrow">←</span>
      <span>prev</span>
    </a>
    <span class="position">
      <span class="cur">{position}</span>
      <span class="sep">/</span>
      <span class="tot">{total}</span>
    </span>
    <a class="nav-next" {next_attr}>
      <span>next</span>
      <span class="arrow">→</span>
      <span class="kbd">j</span>
    </a>
  </nav>

</div>

<aside class="drawer" id="drawer">
  <h2>Assessment</h2>
  <div class="drawer-meta">{src_chip or "(no source)"} · {_h(confidence or "")}</div>
  <div class="drawer-body">{reasoning_html}</div>
  {suggested_panel}
</aside>

<div class="help-overlay" id="help-overlay">
  <div class="help-card">
    <h2>Keyboard</h2>
    <dl>
      <dt><kbd>j</kbd> / <kbd>↓</kbd> / <kbd>n</kbd></dt><dd>next pair</dd>
      <dt><kbd>k</kbd> / <kbd>↑</kbd> / <kbd>p</kbd></dt><dd>previous pair</dd>
      <dt><kbd>1</kbd> / <kbd>←</kbd> / <kbd>h</kbd></dt><dd>show query pane</dd>
      <dt><kbd>2</kbd> / <kbd>→</kbd> / <kbd>l</kbd></dt><dd>show candidate pane</dd>
      <dt><kbd>c</kbd></dt><dd>copy suggested comment</dd>
      <dt><kbd>r</kbd></dt><dd>toggle reasoning</dd>
      <dt><kbd>g</kbd> <kbd>i</kbd></dt><dd>go to index</dd>
      <dt><kbd>?</kbd></dt><dd>show this help</dd>
      <dt><kbd>esc</kbd></dt><dd>close overlays</dd>
    </dl>
    <p class="hint">press <kbd>?</kbd> or <kbd>esc</kbd> to close</p>
  </div>
</div>

<div class="toast" id="toast">copied</div>

<script>
(function() {{
  const prevUrl = {json.dumps(prev_href)};
  const nextUrl = {json.dumps(next_href)};
  const suggested = {suggested_js};
  const copyBtn = document.getElementById("copy-btn");
  const reasoningBtn = document.getElementById("reasoning-btn");
  const drawer = document.getElementById("drawer");
  const help = document.getElementById("help-overlay");
  const toast = document.getElementById("toast");
  const split = document.querySelector(".split");
  const tabs = document.querySelectorAll(".pane-tab");
  let gHeld = false;

  function setPane(name) {{
    if (!split) return;
    split.dataset.active = name;
    tabs.forEach(t => {{
      const on = t.dataset.pane === name;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    }});
    // Scroll the visible pane to top so we land at the start of the new content
    const active = split.querySelector(".pane-" + name);
    if (active) active.scrollTop = 0;
  }}

  tabs.forEach(t => {{
    t.addEventListener("click", () => setPane(t.dataset.pane));
  }});

  function showToast(msg) {{
    toast.textContent = msg;
    toast.classList.add("show");
    setTimeout(() => toast.classList.remove("show"), 1200);
  }}

  function copyComment() {{
    if (!suggested) return;
    navigator.clipboard.writeText(suggested).then(() => {{
      showToast("copied: " + suggested);
      if (copyBtn) {{
        copyBtn.classList.add("copied");
        setTimeout(() => copyBtn.classList.remove("copied"), 800);
      }}
    }}).catch(() => showToast("copy failed"));
  }}

  function toggleDrawer() {{
    drawer.classList.toggle("open");
  }}

  function toggleHelp() {{
    help.classList.toggle("open");
  }}

  if (copyBtn) copyBtn.addEventListener("click", copyComment);
  if (reasoningBtn) reasoningBtn.addEventListener("click", toggleDrawer);
  help.addEventListener("click", (e) => {{
    if (e.target === help) toggleHelp();
  }});

  document.addEventListener("keydown", (e) => {{
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;

    if (e.key === "Escape") {{
      drawer.classList.remove("open");
      help.classList.remove("open");
      return;
    }}

    if (e.key === "?") {{ toggleHelp(); e.preventDefault(); return; }}

    if (gHeld) {{
      if (e.key === "i") {{ window.location.href = {json.dumps(index_href)}; return; }}
      gHeld = false;
      return;
    }}

    switch (e.key) {{
      case "j":
      case "ArrowDown":
      case "n":
        if (nextUrl) window.location.href = nextUrl;
        break;
      case "k":
      case "ArrowUp":
      case "p":
        if (prevUrl) window.location.href = prevUrl;
        break;
      case "c":
        copyComment();
        break;
      case "r":
        toggleDrawer();
        break;
      case "g":
        gHeld = true;
        setTimeout(() => {{ gHeld = false; }}, 800);
        break;
      case "1":
        setPane("query");
        break;
      case "2":
        setPane("candidate");
        break;
      case "ArrowLeft":
      case "h":
        setPane("query");
        break;
      case "ArrowRight":
      case "l":
        setPane("candidate");
        break;
    }}
  }});
}})();
</script>
</body>
</html>
"""
    head = _build_head(f"mpy-triage · pair {index + 1}", css_href=css_href)
    return head + body
