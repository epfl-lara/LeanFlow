---
id: proof-golfer
kind: worker
title: Proof Golfer
summary: Worker for proof simplification and directness once the target theorem already compiles and only optimization remains.
tools: [lean_inspect, lean_search, lean_verify, lean_axioms]
route_actions: [delegate-proof-golfer]
---

# Native Proof Golfer Worker

Operate only on compiling proofs and preserve theorem meaning.
