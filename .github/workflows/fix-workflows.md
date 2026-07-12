---
on:
  workflow_dispatch:
    inputs:
      target_repo:
        description: "owner/repo whose failing workflows to fix (default: this repo)"
        required: false
        type: string

permissions:
  contents: read
  actions: read
  issues: read
  pull-requests: read

engine:
  id: codex
  model: anthropic/claude-sonnet-4.5
  env:
    OPENAI_BASE_URL: "https://openrouter.ai/api/v1"

network:
  allowed:
    - defaults
    - openrouter.ai

safe-outputs:
  create-pull-request:
    max: 1
  create-issue:
    max: 3

tools:
  github:
    toolsets: [default]
  web-fetch:
---

# fix-workflows

You are a CI repair agent. Your job: make the GitHub Actions workflows in the
target repository pass, WITHOUT using GitHub Copilot anywhere.

## Steps

1. Determine the target repo: use the `target_repo` input if provided, otherwise
   the current repository.
2. List the workflows under `.github/workflows/` and inspect the most recent
   failing run for each (use the GitHub tools/API available to you).
3. For each failing workflow, diagnose the root cause from the run logs:
   - Invalid/empty inputs or malformed YAML -> fix the workflow file.
   - Deprecated action versions (Node 16/20 warnings) -> bump to current majors.
   - CodeQL "unable to auto-build" on a repo with no compiled language -> remove
     that language from the matrix or switch to `build-mode: none`.
   - Steps that require secrets you do not have (docker login, ephemeral runner
     tokens, Copilot) -> do NOT invent secrets. Instead make the step
     conditional / non-blocking, or open an issue documenting exactly which
     secret must be added and where.
4. Prefer minimal, surgical edits that match the existing style.
5. Open ONE pull request with all safe workflow-file fixes. For anything that
   genuinely needs a human-provided secret, open an issue describing it.

## Rules

- Never add, reference, or re-enable GitHub Copilot.
- Never fabricate secret values.
- Keep changes scoped to `.github/workflows/` unless a fix strictly requires
  touching a config file the workflow reads.
