import Mathlib

/-- A non-empty finset of cupcakes is consecutive if it consists of indices `start, start+1, ..., start+len-1`
in the cyclic group structure of `Fin m`. -/
def IsConsecutive {m : ℕ} (A : Finset (Fin m)) : Prop :=
  A.Nonempty ∧ ∃ (start : Fin m) (len : ℕ),
    A.card = len ∧ len > 0 ∧
    -- k = start + i (mod m). Fin m addition is cyclic.
    ∀ k : Fin m, k ∈ A ↔ ∃ i < len, k = start + i

/-- The partitioning hypothesis: For each person `p`, the circle can be partitioned into `n`
consecutive groups, each giving `p` a total score of at least 1. -/
def satisfies_partition_condition {m n : ℕ} (scores : Fin n → Fin m → ℝ) : Prop :=
  ∀ (p : Fin n),
    ∃ (groups : Fin n → Finset (Fin m)),
      -- 1. Groups are consecutive
      (∀ i : Fin n, IsConsecutive (groups i)) ∧
      -- 2. Groups form a partition (unique containment)
      (∀ c : Fin m, ∃! i : Fin n, c ∈ groups i) ∧
      -- 3. Score condition
      (∀ i : Fin n, Finset.sum (groups i) (fun c => scores p c) ≥ 1)

/-- The conclusion: It is possible to distribute the cupcakes such that each person `p` receives a total score of at least 1. -/
def exists_good_distribution {m n : ℕ} (scores : Fin n → Fin m → ℝ) : Prop :=
  ∃ (D : Fin m → Fin n),
    ∀ (p : Fin n),
      let received_cupcakes : Finset (Fin m) := Finset.univ.filter (fun c => D c = p)
      Finset.sum received_cupcakes (fun c => scores p c) ≥ 1

/-- The main theorem, stating that the existence of a good score-partition for every person
implies the existence of a distribution of total score >= 1 for every person. -/
theorem PBAdvanced030 {m n : ℕ} (scores : Fin n → Fin m → ℝ) (hm : 1 ≤ m) (hn : 1 ≤ n)
    (hmn : n ≤ m) (h_nonneg : ∀ (p : Fin n) (c : Fin m), 0 ≤ scores p c) :
  satisfies_partition_condition scores →
  exists_good_distribution scores := by sorry
