import Mathlib

-- Let $a_1,a_2,...,a_{18}$ be 18 real numbers, not necessarily distinct, with average $m$. Let $A$ denote the number of triples $1 \le i < j < k \le 18$ for which $a_i + a_j + a_k \ge 3m$. What is the minimum possible value of $A$?
-- Answer: $136$

-- Let $a_1,a_2,...,a_{18}$ be 18 real numbers, not necessarily distinct
-- the indexing doesn't matter, so we use Lean's default indexing from zero
variable (a : Fin 18 → ℝ)

-- $m$ is the average of $a$
noncomputable
def m : ℝ := (∑ i : Fin 18, a i) / 18

-- Let $A$ denote the number of triples $1 \le i < j < k \le 18$ for which $a_i + a_j + a_k \ge 3m$.
noncomputable
def A : ℕ :=
    { (i, j, k) : Fin 18 × Fin 18 × Fin 18 |
    i < j ∧ j < k ∧ a i + a j + a k ≥ 3 * (m a) }.ncard

-- prove that the minimum is 136
theorem PBBasic009 : iInf A = 136 := by sorry
