---
name: provider-fallback
description: Handle provider outages and fallback routing for EPFLemma workflows.
---

# Provider Fallback

Use this skill when provider availability is unstable.

## Procedure

1. Identify the active provider, base URL, and model.
2. If the active route is failing, try the configured fallback or a known-good alternative.
3. For RCP-style OpenAI-compatible endpoints, prefer the exact tested model spelling.
4. Record which route succeeded so the next workflow step is reproducible.

## Defaults

- Prefer the direct requested provider when healthy.
- For the known RCP fallback path, use `google/gemma-4-31B-it` with exact casing.
