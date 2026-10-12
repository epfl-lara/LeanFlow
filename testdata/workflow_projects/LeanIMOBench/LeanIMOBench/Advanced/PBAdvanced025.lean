import Mathlib

/-
Let $k$ and $d$ be positive integers. Prove that there exists a positive integer $N$ such that for every odd integer $n > N$, the digits in the base-$2n$ representation of $n^k$ are all greater than $d$.
-/
theorem PBAdvanced025
    (k d : ℕ) (hk : 0 < k) (hd : 0 < d) :
    ∃ N > 0, ∀ n > N, Odd n → ∀ u ∈ Nat.digits (2*n) (n^k), d < u := by sorry
