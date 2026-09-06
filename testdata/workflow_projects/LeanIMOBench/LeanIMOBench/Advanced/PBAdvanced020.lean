import Mathlib

open scoped Topology

/-
For given positive integers $x$ and $y$, we define a sequence $(a_n)_{n \ge 1}$ as
$$
a_n = \gcd \left( x^n +y , \, (y-x)\left(\sum_{i=0}^{n-1} y^i x^{n-i-1} - 1\right) \right)
$$
for all $n\in \mathbb{N}$. Find all pairs $(x,y)$ of positive integers such that the limit of the sequence $(a_n)$ exists.
-/

/-- Recursive definition of `a x y n` -/
def a (x y : ℤ) (n : ℕ) : ℤ :=
  (x^n + y).gcd ((y - x) * (∑ i ∈ Finset.range n, y^i * x^(n - i - 1 : ℕ) - 1))

/-- All pairs of positive integers x, y where a limit exists -/
def satisfyingPairs : Set (ℤ × ℤ) := {
  (x, y) | (x : ℤ) (_ : 0 < x) (y : ℤ) (_ : 0 < y) (l : ℝ)
  (_ : Filter.atTop.Tendsto (fun n ↦ (a x y n : ℝ)) (𝓝 l))
}

theorem PBAdvanced020 : satisfyingPairs = {(1, 1)} := by sorry
