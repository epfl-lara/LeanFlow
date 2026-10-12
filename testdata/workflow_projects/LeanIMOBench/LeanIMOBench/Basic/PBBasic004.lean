import Mathlib

/-
Find all strictly increasing functions $g:\mathbb{R} \to \mathbb{R}$ such that:
(a) $g$ is surjective
(b) $g(g(x))=g(x)+20x.$

Solution: $g(x) = 5x$ for all x
-/
theorem PBBasic004 :
    {g : ℝ → ℝ | StrictMono g ∧ g.Surjective ∧ ∀ x, g (g x) = g x + 20 * x}
      = {(fun x ↦ 5 * x)} := by sorry
