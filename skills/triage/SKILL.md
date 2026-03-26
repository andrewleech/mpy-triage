---
name: MPy Issue Triage
description: This skill should be used when the user wants to find duplicate or related MicroPython issues/PRs, triage an issue for similarity to existing items, detect spam or off-topic issues, or check if a new issue has already been reported. Invoke when user mentions finding duplicates, triaging issues, checking for related PRs, or detecting spam.
---

# MPy Issue Triage

Detect duplicate, related, and off-topic issues/PRs across MicroPython repositories using semantic search and LLM assessment.

## When to Use

- Finding duplicates of a specific issue or PR
- Triaging a new issue to see if it's already been reported
- Checking if a PR overlaps with existing work
- Detecting spam or off-topic issues
- Looking for related issues that might inform a fix

## Workflow

### Triage an Issue

```bash
uv run --project ${CLAUDE_PLUGIN_ROOT} mpy-triage issue <NUMBER> --repo micropython/micropython
```

### Triage a PR

```bash
uv run --project ${CLAUDE_PLUGIN_ROOT} mpy-triage pr <NUMBER> --repo micropython/micropython
```

### Options

- `--skip-summarize` - Skip Haiku summarization, use only static fields for embedding
- `--skip-assess` - Skip Sonnet assessment, return ranked candidates with scores only
- `--json` - Output results as JSON for machine processing
- `--repo REPO` - Target repository (default: micropython/micropython)

### Data Management

Before first use, the database must be populated:

```bash
uv run --project ${CLAUDE_PLUGIN_ROOT} mpy-triage collect
uv run --project ${CLAUDE_PLUGIN_ROOT} mpy-triage summarize
uv run --project ${CLAUDE_PLUGIN_ROOT} mpy-triage assemble
uv run --project ${CLAUDE_PLUGIN_ROOT} mpy-triage embed
```

Check database status:

```bash
uv run --project ${CLAUDE_PLUGIN_ROOT} mpy-triage stats
```

### Interpreting Results

Each candidate shows:
- **Classification**: DUPLICATE, LIKELY_DUPLICATE, RELATED, OFF_TOPIC, or UNRELATED
- **Confidence**: high, medium, or low
- **Reasoning**: Why this classification was chosen
- **Suggested action**: What the maintainer should do

Results include full GitHub URLs for easy navigation.
