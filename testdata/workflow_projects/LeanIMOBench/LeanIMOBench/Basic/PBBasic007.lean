import Mathlib

/-
Determine all positive integers $n$ and integer sequences $a_0, a_1,\ldots, a_n$ such that $a_n = 3$ and
\[f(a_{i-1}) = a_i\]
for all $i = 1,2,\ldots,n$, where $f(x) = a_n x^n + a_{n-1} x^{n-1} + \cdots + a_1 x + a_0$.

Solution: $n=2$ with $\left(a_{0}, a_{1}, a_{2}\right)=(-1,1,3)$
-/
open Polynomial

theorem PBBasic007 :
    { f : ℤ[X] | f.leadingCoeff = 3 ∧ 1 ≤ f.degree ∧
      ∀ (i : ℕ), i < f.degree → f.coeff (i + 1) = f.eval (f.coeff i) } =
    {3 * X ^ 2 + 1 * X - 1} := by sorry
