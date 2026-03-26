---
name: mpy-triage
description: Triage a MicroPython issue or PR for duplicates and related items
argument-hint: "<issue|pr> <number> [--skip-summarize] [--skip-assess] [--json]"
allowed-tools: ["Bash", "Read"]
---

Run the mpy-triage CLI to find duplicate and related issues/PRs.

Parse the user's input to determine:
- Whether they want to triage an issue or PR
- The item number
- Any optional flags (`--skip-summarize`, `--skip-assess`, `--json`, `--repo REPO`)

Execute:

```bash
uv run --project ${CLAUDE_PLUGIN_ROOT} mpy-triage <issue|pr> <NUMBER> [OPTIONS]
```

Present the output to the user. If they want more detail on a candidate, use `gh issue view <NUMBER> --repo micropython/micropython` or `gh pr view <NUMBER> --repo micropython/micropython` to fetch it.
