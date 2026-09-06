import Mathlib

/-
101 stones are arranged in a circle, numbered 1 through 101 in order (so that stone 1 is next to stone 101). Each stone is painted either red, white, or blue. Initially, stone 101 is the only blue stone. Among the remaining stones, even-numbered stones are red, and odd-numbered stones are white.

We perform a sequence of modifications to the coloring, where in each step, we may choose a stone and repaint it a different color, as long as we ensure that no two adjacent stones are ever the same color. Prove that it is not possible to eventually reach a state where again stone 101 is the only blue stone, but among the remaining stones, all even-numbered stones are white and all odd-numbered stones are red."
-/
open Relation

inductive Color
| red
| white
| blue
deriving DecidableEq

def Coloring := Fin 101 → Color

-- a coloring is valid if no adjacent stones have the same color
def Coloring.Valid (coloring : Coloring) :=
  ∀ i : Fin 101, coloring i ≠ coloring (i+1)

-- the initial coloring, we swap even / odd from the problem statement because we use 0-based indexing
def Coloring.initial : Coloring := fun i ↦
  if i = 100 then .blue else if i % 2 = 1 then .red else .white
-- the final coloring is the same but with white & red swapped
def Coloring.final : Coloring := fun i ↦
  if i = 100 then .blue else if i % 2 = 1 then .white else .red

-- `c2` can be obtained with a single step from `c1`
def Coloring.Step (c1 c2 : Coloring) : Prop :=
  ∃ i : Fin 101, c2.Valid ∧ ∀ j, j ≠ i ↔ c1 j = c2 j

-- The main problem: the final coloring is not reachable, that is, the edge (initial, final)
-- is not in the transitive closure.
theorem PBBasic016 : ¬ ReflTransGen Coloring.Step .initial .final := by sorry
