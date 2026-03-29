---
sidebar_position: 1
title: "CLI Interface"
description: "Use the Gauss terminal interface and launch the managed Lean workflows."
---

# CLI Interface

Gauss is a terminal-first product. The public surface is intentionally small.

## Start the CLI

```bash
gauss
gauss --resume <session_id>
gauss chat -q "hello"
```

## Primary Workflow

Run this inside the interactive CLI:

```text
/prove
/review Main.lean
/checkpoint Main.lean
/refactor Main.lean
/golf Main.lean
/draft "Every continuous function on a compact set is bounded"
/autoprove
/formalize --source ./paper.pdf "Theorem 3.2"
/autoformalize --source ./paper.pdf --claim-select=first --out=Paper.lean
```

Gauss stages the managed Lean runtime, yields the terminal to the configured backend, and restores the same Gauss session when the managed workflow exits.

## Default Slash Commands

- `/prove`
- `/draft`
- `/review`
- `/checkpoint`
- `/refactor`
- `/golf`
- `/autoprove`
- `/formalize`
- `/autoformalize`
- `/new`, `/reset`, `/clear`
- `/history`, `/save`, `/retry`, `/undo`, `/title`
- `/config`, `/model`, `/provider`, `/verbose`, `/reasoning`
- `/compress`, `/rollback`, `/stop`
- `/usage`, `/paste`, `/help`, `/quit`

## Compatibility Notes

- `/handoff` is no longer a public generic workflow and only survives as a compatibility alias to `/autoformalize`
- only a curated subset of bundled skill slash commands are exposed directly by Gauss
- inside the interactive CLI, common missing-slash forms like `prove ...`, `review ...`, and `auto-proof ...` are rewritten automatically
- user-managed MCP commands are not part of the default Gauss surface
