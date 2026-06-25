---
id: doctor
kind: workflow
title: Doctor
summary: Native Lean environment diagnostic covering project validity, MCP/LSP tools, search providers, worker availability, legacy migrations, and cleanup hints.
skills: [lean-diagnostics]
tools: [lean_capabilities]
stop_conditions: [report-generated]
route_actions: [diagnostics]
---

# Native Doctor Spec

Doctor is non-throwing and should report degraded states instead of crashing.
