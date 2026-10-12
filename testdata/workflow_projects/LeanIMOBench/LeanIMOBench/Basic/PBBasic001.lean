import Mathlib

/-
Determine all functions $f: \mathbb{Z} \rightarrow \mathbb{Z}$ such that, for all $x, y \in \mathbb{Z}$, we have \[ f(2x)+2f(y)=f(f(x+y)).\]

Solution: $f(x) = 0$ and $f(x) = 2x + c$ for all integer $x$ and some constant $c$.
-/
theorem PBBasic001 :
    {f : ℤ → ℤ | ∀ x y, f (2 * x) + 2 * f y = f (f (x + y))}
      = {0} ∪ {(fun x ↦ 2 * x + c)| (c : ℤ)} := by sorry
