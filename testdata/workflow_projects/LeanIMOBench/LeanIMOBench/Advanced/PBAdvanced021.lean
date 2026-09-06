import Mathlib

/-- The property that a sequence `c` of positive integers satisfies the recursive rule
starting from index `N + 1`. Indices start at 1. -/
def SatisfiesRule (c : ℕ → ℕ) (N : ℕ) : Prop :=
  (N > 0) ∧
  -- (1) All chosen numbers are positive integers (for i ≥ 1).
  (∀ i : ℕ, i > 0 → c i > 0) ∧
  -- (2) For m > N, the rule applies.
  (∀ m : ℕ, m > N →
    -- The set of indices {k | 1 ≤ k ≤ m - 2} is Finset.Icc 1 (m - 2)
    c m = 1 + Finset.card (
      (Finset.Icc 1 (m - 2)).filter (fun k => c k = c (m - 1)))
    )

variable (c : ℕ → ℕ)

/-- The sequence of numbers chosen by the boys (at odd positions 1, 3, 5, ...). -/
def boys_sequence : ℕ → ℕ :=
  fun i => c (2 * i - 1)

/-- The sequence of numbers chosen by the girls (at even positions 2, 4, 6, ...). -/
def girls_sequence : ℕ → ℕ :=
  fun i => c (2 * i)

/-- A sequence `a : ℕ → ℕ` is eventually periodic. We require the starting index M and period P to be positive. -/
def IsEventuallyPeriodic (a : ℕ → ℕ) : Prop :=
  -- M is the index after which periodicity starts, P is the period.
  ∃ M P : ℕ, M > 0 ∧ P > 0 ∧ ∀ n : ℕ, n ≥ M → a n = a (n + P)

-- The main claim of the problem: At least one of the gender subsequences is eventually periodic.
theorem PBAdvanced021 {N : ℕ} (h_valid : SatisfiesRule c N) :
  IsEventuallyPeriodic (boys_sequence c) ∨ IsEventuallyPeriodic (girls_sequence c) := by sorry
