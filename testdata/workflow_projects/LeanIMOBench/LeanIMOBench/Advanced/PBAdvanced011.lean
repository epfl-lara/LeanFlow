import Mathlib

/-
Find all functions $f: \mathbb{R}^+ \to \mathbb{R}^+$ such that $$yf(yf(x)+1) = f(\frac{1}{x} + f(y))$$ for all $x, y \in \mathbb{R}^+$
Solution: ${fun x => 1/x}$
-/
open Classical

theorem PBAdvanced011 :
    {f : {x : ℝ // 0 < x} → {x : ℝ // 0 < x} | ∀ x y, y * f (y * f x + 1) = f (1 / x + f y)}
      = {fun x ↦ 1 / x} := by sorry
