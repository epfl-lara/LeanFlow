import Mathlib

/-
Let $p$ be a prime number and $a, b$ be positive integers such that

\[
p^{n}=a^{4}+b^{4}
\]

for some integer $n \geq 2$. Prove that $n \geq 5$.
-/
theorem PBAdvanced012
    (p a b n : ℕ) (hp : p.Prime) (ha : a ≠ 0) (hb : b ≠ 0)
    (hp' : p ^ n = a ^ 4 + b ^ 4) (hn : 2 ≤ n) : 5 ≤ n := by sorry
