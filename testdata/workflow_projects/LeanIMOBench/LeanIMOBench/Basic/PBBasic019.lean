import Mathlib

/-
For any positive integer $n$, let $\delta(n)$ be the largest odd divisor of $n$. Prove for any $N>0$ that we have
\[
\left| \sum_{n=1}^N \frac{\delta(n)}{n}- \frac{2}{3}N \right| <1.
\]
-/
theorem PBBasic019
    (δ : ℕ → ℕ) (hδ : ∀ n, δ n = (n.divisors.filter Odd).sup id)
    (N : ℕ) (hN : 0 < N) :
    |∑ n ∈ Finset.Icc 1 N, (δ n : ℚ) / n - 2 / 3 * N| < 1 := by sorry
