import Mathlib

/-
Find all functions $f\colon \mathbb{R} \rightarrow \mathbb{R}$ such that for all $a,b \in \mathbb{R}$,
\[ (b - a)f(f(a)) = a f(a + f(b)). \]

Solution: $f(x)=0, f(x)=-x+k$ where $k$ is a constant
-/
theorem PBBasic003 :
    {f : ℝ → ℝ | ∀ a b, (b - a) * f (f a) = a * f (a + f b)}
      = {0} ∪ {(fun x ↦ - x + k) | (k : ℝ)} := by sorry
