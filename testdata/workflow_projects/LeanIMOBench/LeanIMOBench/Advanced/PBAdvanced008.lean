import Mathlib

/-
Let $\left\{F_{n}\right\}_{n \geq 1}$ be a sequence of integers satisfying $F_{1}=1$ and for $n \geq 2$,
\[
F_{n}=n^{F_{n-1}}.
\]
For example, $F_3 = 3^2= 9$ and $F_4 = 4^9$.

Prove that for any positive integers $a, c$ and integer $b$, there exists a positive integer $n$ such that the following expression is an integer:

\[
\frac{a^{F_{n}}+n-b}{c}.
\]
-/
theorem PBAdvanced008
    (F : ℕ → ℕ) (hF1 : F 1 = 1) (hF_rec : ∀ n ≥ 1, F (n+1) = (n+1) ^ F n)
    (a b c : ℤ) (ha : 0 < a) (hc : 0 < c) :
    ∃ᵉ (n ≥ 1) (m : ℤ), (a ^ F n + n - b : ℚ) / c = m := by sorry
