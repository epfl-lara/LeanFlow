import Mathlib

/-
Consider a positive integer $n$. We define $f(n)$ as the number of pairs of paths on an $n \times n$ grid that:
 (1) Both paths start at $(0, 0)$ (bottom left corner) and end at $(n, n)$ (top right corner).
 (2) Both paths allow only right or up movements (one unit each).
 (3) The $y$ coordinate of the first path never exceeds the y coordinate of the second path at any timestep.

 For example, when $n = 2$, consider the following pair of paths:

 The first path: $(0,0) \rightarrow (1,0) \rightarrow (1,1) \rightarrow (2,1) \rightarrow (2,2)$
 The second path: $(0,0) \rightarrow (1,0) \rightarrow (2,0) \rightarrow (2,1) \rightarrow(2,2)$
 The example is invalid because after 2 steps, the y coordinate of the first path (1) is larger than the y coordinate of the second path (0).

 However, the following example is valid,

 The first path: $(0,0) \rightarrow (1,0) \rightarrow (2,0) \rightarrow (2,1) \rightarrow (2,2)$
 The second path: $(0,0) \rightarrow (1,0) \rightarrow (1,1) \rightarrow (2,1) \rightarrow (2,2)$

 since the y coordinate of the first path is never larger than the second path. Find $f(10)$."

Answer: $\binom{20}{10}^2 - \binom{20}{9}^2$
-/

-- a predicate asserting that a path only goes up or right by a single step
structure ProblemPath (n : ℕ) where
  -- points of the path, (0,0), (0,1), (1,1), ...
  points : List (ℕ × ℕ)
  nonempty : points ≠ [] -- auxuliary condition to easier state the rest
  -- starts at (0,0)
  start : points.head nonempty = (0,0)
  -- ends at (n,n)
  finish : points.getLast nonempty = (n,n)
  -- every step goes up or right
  up_right : points.Chain' (fun (x₁,y₁) (x₂,y₂) ↦ (x₂,y₂) = (x₁+1,y₁) ∨ (x₂,y₂) = (x₁,y₁+1))

-- a pair of paths satisfying the condition from the problem statement:
-- The $y$ coordinate of the first path never exceeds the y coordinate of the second path at any timestep.
structure ProblemPath.pair n where
  (path1 path2 : ProblemPath n)
  (cond : ∀ (i : ℕ) (_ : i < path1.points.length) (_ : i < path2.points.length),
    path1.points[i].2 ≤ path2.points[i].2)

noncomputable
def f (n : ℕ) : ℕ := Nat.card (ProblemPath.pair n)

theorem PBBasic012 : f 10 = (Nat.choose 20 10)^2 - (Nat.choose 20 9)^2 := by sorry
