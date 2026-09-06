/-
Lean-IMO-Bench root module.

Deliberately imports nothing. Each of the 60 problems is a standalone module
under `LeanIMOBench/Basic/` or `LeanIMOBench/Advanced/`, so that a single
problem can be built, proved, and verified without dragging in the other 59.

Build one problem:   lake build LeanIMOBench.Basic.PBBasic001
Build all statements: lake build            (the statement-integrity gate)
-/
