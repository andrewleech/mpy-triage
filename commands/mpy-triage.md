---
name: mpy-triage
description: Triage a MicroPython issue or PR for duplicates and related items
argument-hint: "<issue|pr> <number> [--repo REPO] [--skip-summarize] [--skip-assess] [--json]"
allowed-tools: ["Bash", "Read"]
---

Run the mpy-triage CLI to find duplicate and related issues/PRs.

Parse the user's input to determine:
- Whether they want to triage an issue or PR
- The item number
- Any optional flags

Execute:

```bash
uv run --project ${CLAUDE_PLUGIN_ROOT} mpy-triage <issue|pr> <NUMBER> [OPTIONS]
```

Present the output to the user. If they want more detail on a candidate, use `gh issue view` or `gh pr view` to fetch it.
