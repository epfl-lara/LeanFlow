import Mathlib.Data.Rat.Defs
import Mathlib.Data.Rat.Lemmas
import Mathlib.Data.Nat.GCD.Basic
import Mathlib.Algebra.Order.Ring.Unbundled.Rat

/-! # Hyperbinary Representations and the Calkin-Wilf Enumeration

Formalization of the paper "Recounting the Rationals" by Calkin and Wilf.

A hyperbinary representation of a nonnegative integer `n` is a finite sequence
of digits in `{0,1,2}` such that `n = Σ εᵢ·2ⁱ`. Trailing zeroes are ignored.

The counting function `h(n)` satisfies:
- `h(0) = 1`
- `h(2n+1) = h(n)` for all `n ≥ 0`
- `h(2n) = h(n) + h(n-1)` for all `n ≥ 1`

The Calkin-Wilf sequence is `qₙ = h(n)/h(n+1)`, which enumerates every
positive rational exactly once in lowest terms.
-/

/-- A hyperbinary representation is a finite sequence of digits in {0,1,2}. -/
def HyperbinaryRep := List (Fin 3)

/-- Compute the value of a hyperbinary representation.
    `value([]) = 0`, `value(d::ds) = d + 2 * value(ds)` -/
def hyperbinaryValue (rep : HyperbinaryRep) : Nat :=
  rep.foldr (fun d acc => d.val + 2 * acc) 0

/-- The hyperbinary counting function `h(n)`.
    Defined by the recurrence relations that uniquely determine it. -/
def h : Nat → Nat
| 0 => 1
| 1 => 1
| n + 2 =>
    if (n + 2) % 2 = 1 then
      h ((n + 2) / 2)
    else
      h ((n + 2) / 2) + h (((n + 2) / 2) - 1)

/-- The Calkin-Wilf fraction `qₙ = h(n)/h(n+1)`. -/
def q (n : Nat) : Rat :=
  (h n : Rat) / (h (n + 1))

/-- `h(0) = 1`. The empty sum is the only hyperbinary representation of 0. -/
lemma h_zero : h 0 = 1 := by
  unfold h
  rfl

/-- `h(1) = 1`. -/
lemma h_one : h 1 = 1 := by
  unfold h
  rfl

/-- The defining equation of `h` for `m + 2`. -/
lemma h_eq (m : Nat) : h (m + 2) = if (m + 2) % 2 = 1 then h ((m + 2) / 2) else h ((m + 2) / 2) + h (((m + 2) / 2) - 1) := by
  conv => lhs; unfold h

/-- Odd recurrence: `h(2n+1) = h(n)` for all `n ≥ 0`. -/
lemma h_odd (n : Nat) : h (2 * n + 1) = h n := by
  cases n with
  | zero =>
    simp [h_zero, h_one]
  | succ n =>
    have h1 : 2 * (n + 1) + 1 = (2 * n + 1) + 2 := by omega
    rw [h1]
    rw [h_eq (2 * n + 1)]
    have h2 : ((2 * n + 1) + 2) % 2 = 1 := by omega
    have h3 : ((2 * n + 1) + 2) / 2 = n + 1 := by omega
    simp [h2, h3]

/-- Even recurrence: `h(2n) = h(n) + h(n-1)` for all `n ≥ 1`. -/
lemma h_even (n : Nat) (hn : n ≥ 1) : h (2 * n) = h n + h (n - 1) := by
  have h1 : 2 * n = (2 * n - 2) + 2 := by omega
  rw [h1]
  rw [h_eq (2 * n - 2)]
  have h2 : ((2 * n - 2) + 2) % 2 = 0 := by omega
  have h3 : ((2 * n - 2) + 2) / 2 = n := by omega
  have h4 : (((2 * n - 2) + 2) / 2) - 1 = n - 1 := by
    rw [h3]
  simp [h2, h3, h4]

/-- Consecutive values of `h` are coprime: `gcd(h(n), h(n+1)) = 1`. -/
lemma consecutive_coprime (n : Nat) : Nat.Coprime (h n) (h (n + 1)) := by
  induction n using Nat.strongRecOn with
  | ind n ih =>
    cases n with
    | zero =>
      simp [h_zero, h_one]
    | succ n =>
      cases n with
      | zero =>
        simp [h_zero, h_one, h_eq]
      | succ n =>
        by_cases hmod : (n + 2) % 2 = 1
        · -- n + 2 is odd, so n + 2 = 2m + 1 for some m
          have hm : n + 2 = 2 * ((n + 2) / 2) + 1 := by omega
          let m := (n + 2) / 2
          have h1 : h (n + 2) = h m := by rw [hm]; rw [h_odd]
          have h2 : h (n + 3) = h (m + 1) + h m := by
            have : n + 3 = 2 * (m + 1) := by omega
            rw [this]
            rw [h_even (m + 1) (by omega)]
            simp
          rw [h1, h2]
          have ih' : Nat.Coprime (h m) (h (m + 1)) := ih m (by omega)
          rw [Nat.coprime_add_self_right]
          exact ih'
        · -- n + 2 is even, so n + 2 = 2m for some m
          have hm : n + 2 = 2 * ((n + 2) / 2) := by omega
          let m := (n + 2) / 2
          have h1 : h (n + 2) = h m + h (m - 1) := by
            rw [hm]
            rw [h_even m (by omega)]
          have h2 : h (n + 3) = h m := by
            have : n + 3 = 2 * m + 1 := by omega
            rw [this]
            rw [h_odd]
          rw [h1, h2]
          have h3 : m - 1 + 1 = m := by omega
          have ih' : Nat.Coprime (h (m - 1)) (h m) := by
            rw [← h3]
            exact ih (m - 1) (by omega)
          rw [add_comm]
          rw [Nat.coprime_add_self_left]
          exact ih'

/-- Left child of a reduced positive fraction `a/b`. -/
def leftChild (a b : Nat) : Rat :=
  (a : Rat) / (a + b)

/-- Right child of a reduced positive fraction `a/b`. -/
def rightChild (a b : Nat) : Rat :=
  (a + b : Rat) / b

/-- `h(n) > 0` for all `n`. -/
lemma h_pos (n : Nat) : h n > 0 := by
  induction n using Nat.strongRecOn with
  | ind n ih =>
    cases n with
    | zero =>
      simp [h_zero]
    | succ n =>
      cases n with
      | zero =>
        simp [h_one]
      | succ n =>
        by_cases hmod : (n + 2) % 2 = 1
        · -- n + 2 is odd
          have hm : n + 2 = 2 * ((n + 2) / 2) + 1 := by omega
          rw [hm]
          rw [h_odd]
          exact ih ((n + 2) / 2) (by omega)
        · -- n + 2 is even
          have hm : n + 2 = 2 * ((n + 2) / 2) := by omega
          rw [hm]
          rw [h_even ((n + 2) / 2) (by omega)]
          have h1 : h ((n + 2) / 2) > 0 := ih ((n + 2) / 2) (by omega)
          have h2 : h (((n + 2) / 2) - 1) > 0 := ih (((n + 2) / 2) - 1) (by omega)
          omega

/-- Left child recurrence: if `qₙ = a/b` in reduced form, then `q_{2n+1} = a/(a+b)`. -/
lemma left_child_recurrence (n a b : Nat)
    (ha : a > 0) (hb : b > 0) (hcop : Nat.Coprime a b)
    (hq : q n = (a : Rat) / b) :
    q (2 * n + 1) = leftChild a b := by
  have h1 : q n = (h n : Rat) / (h (n + 1)) := by rfl
  rw [h1] at hq
  have h2 : Nat.Coprime (h n) (h (n + 1)) := consecutive_coprime n
  have h3 : (h n : Int) = (a : Int) ∧ (h (n + 1) : Int) = (b : Int) := by
    apply Rat.div_int_inj
    · exact_mod_cast show h (n + 1) > 0 by apply h_pos
    · exact_mod_cast hb
    · simp; exact h2
    · simp; exact hcop
    · exact_mod_cast hq
  have hn : h n = a := by exact_mod_cast h3.1
  have hnp1 : h (n + 1) = b := by exact_mod_cast h3.2
  have h4 : q (2 * n + 1) = (h (2 * n + 1) : Rat) / (h (2 * n + 2)) := by rfl
  rw [h4]
  have h5 : h (2 * n + 1) = h n := h_odd n
  have h6 : h (2 * n + 2) = h (n + 1) + h n := by
    have : 2 * n + 2 = 2 * (n + 1) := by omega
    rw [this]
    rw [h_even (n + 1) (by omega)]
    simp
  rw [h5, h6, hn, hnp1]
  simp [leftChild]
  rw [add_comm (b : Rat)]

/-- Right child recurrence: if `qₙ = a/b` in reduced form, then `q_{2n+2} = (a+b)/b`. -/
lemma right_child_recurrence (n a b : Nat)
    (ha : a > 0) (hb : b > 0) (hcop : Nat.Coprime a b)
    (hq : q n = (a : Rat) / b) :
    q (2 * n + 2) = rightChild a b := by
  have h1 : q n = (h n : Rat) / (h (n + 1)) := by rfl
  rw [h1] at hq
  have h2 : Nat.Coprime (h n) (h (n + 1)) := consecutive_coprime n
  have h3 : (h n : Int) = (a : Int) ∧ (h (n + 1) : Int) = (b : Int) := by
    apply Rat.div_int_inj
    · exact_mod_cast show h (n + 1) > 0 by apply h_pos
    · exact_mod_cast hb
    · simp; exact h2
    · simp; exact hcop
    · exact_mod_cast hq
  have hn : h n = a := by exact_mod_cast h3.1
  have hnp1 : h (n + 1) = b := by exact_mod_cast h3.2
  have h4 : q (2 * n + 2) = (h (2 * n + 2) : Rat) / (h (2 * n + 3)) := by rfl
  rw [h4]
  have h5 : h (2 * n + 2) = h (n + 1) + h n := by
    have : 2 * n + 2 = 2 * (n + 1) := by omega
    rw [this]
    rw [h_even (n + 1) (by omega)]
    simp
  have h6 : h (2 * n + 3) = h (n + 1) := by
    have : 2 * n + 3 = 2 * (n + 1) + 1 := by omega
    rw [this]
    rw [h_odd]
  rw [h5, h6, hn, hnp1]
  simp [rightChild]
  rw [add_comm (b : Rat)]

/-- The parent step decreases the sum.
    If `a,b` are coprime positive integers with `(a,b) ≠ (1,1)`:
    - If `a < b`, then `(a, b-a)` is coprime and `a + (b-a) < a + b`.
    - If `b < a`, then `(a-b, b)` is coprime and `(a-b) + b < a + b`. -/
lemma parent_step_decreases (a b : Nat)
    (ha : a > 0) (hb : b > 0) (hcop : Nat.Coprime a b)
    (hne : (a, b) ≠ (1, 1)) :
    (a < b → Nat.Coprime a (b - a) ∧ a + (b - a) < a + b) ∧
    (b < a → Nat.Coprime (a - b) b ∧ (a - b) + b < a + b) := by
  constructor
  · -- Case a < b
    intro hlt
    constructor
    · -- Show a and b - a are coprime
      have hle : a ≤ b := by omega
      rw [Nat.coprime_sub_self_right hle]
      exact hcop
    · -- Show a + (b - a) < a + b
      have : a + (b - a) = b := by omega
      rw [this]
      omega
  · -- Case b < a
    intro hlt
    constructor
    · -- Show a - b and b are coprime
      have hle : b ≤ a := by omega
      rw [Nat.coprime_sub_self_left hle]
      exact hcop
    · -- Show (a - b) + b < a + b
      have : (a - b) + b = a := by omega
      rw [this]
      omega

/-- If `h(m) = 1` and `h(m+1) = 1`, then `m = 0`. -/
lemma h_unique_one_one (m : Nat) :
    h m = 1 → h (m + 1) = 1 → m = 0 := by
  intro hm1 hm2
  induction m using Nat.strongRecOn with
  | ind m ih =>
    cases m with
    | zero => rfl
    | succ m =>
      have h01 : (m + 1) % 2 = 0 ∨ (m + 1) % 2 = 1 := by omega
      cases h01 with
      | inl heven =>
        have h_eq : m + 1 = 2 * ((m + 1) / 2) := by omega
        rw [h_eq] at hm1
        rw [h_even ((m + 1) / 2) (by omega)] at hm1
        have h1 : h ((m + 1) / 2) > 0 := h_pos ((m + 1) / 2)
        have h2 : h (((m + 1) / 2) - 1) > 0 := h_pos (((m + 1) / 2) - 1)
        omega
      | inr hodd =>
        have h_eq : m + 1 = 2 * ((m + 1) / 2) + 1 := by omega
        rw [h_eq] at hm1
        rw [h_odd] at hm1
        have hk1 : h ((m + 1) / 2) = 1 := by omega
        have h_eq2 : m + 2 = 2 * ((m + 1) / 2 + 1) := by omega
        rw [h_eq2] at hm2
        rw [h_even ((m + 1) / 2 + 1) (by omega)] at hm2
        simp at hm2
        have hk2 : h ((m + 1) / 2 + 1) = 0 := by omega
        have : h ((m + 1) / 2 + 1) > 0 := h_pos ((m + 1) / 2 + 1)
        omega

/-- Comparison of consecutive h values based on parity.
    - n = 0: h(0) = h(1)
    - n > 0 even: h(n) > h(n+1)
    - n odd: h(n) < h(n+1) -/
lemma h_compare (n : Nat) :
    (n = 0 → h n = h (n + 1)) ∧
    (n > 0 → n % 2 = 0 → h n > h (n + 1)) ∧
    (n % 2 = 1 → h n < h (n + 1)) := by
  constructor
  · -- n = 0
    intro hn
    rw [hn]
    simp [h_zero, h_one]
  constructor
  · -- n > 0 even
    intro hn heven
    have h_eq : n = 2 * (n / 2) := by omega
    rw [h_eq]
    have h1 : h (2 * (n / 2)) = h (n / 2) + h (n / 2 - 1) := by
      apply h_even (n / 2)
      omega
    have h2 : h (2 * (n / 2) + 1) = h (n / 2) := by
      apply h_odd
    rw [h1, h2]
    have h3 : h (n / 2 - 1) > 0 := h_pos (n / 2 - 1)
    omega
  · -- n odd
    intro hodd
    have h_eq : n = 2 * (n / 2) + 1 := by omega
    rw [h_eq]
    have h1 : h (2 * (n / 2) + 1) = h (n / 2) := by
      apply h_odd
    have h2 : h (2 * (n / 2) + 2) = h (n / 2 + 1) + h (n / 2) := by
      have h3 : 2 * (n / 2) + 2 = 2 * (n / 2 + 1) := by omega
      rw [h3]
      rw [h_even (n / 2 + 1) (by omega)]
      simp
    rw [h1, h2]
    have h3 : h (n / 2 + 1) > 0 := h_pos (n / 2 + 1)
    omega

/-- Calkin-Wilf enumeration theorem.
    For every pair of coprime positive integers `a` and `b`,
    there exists a unique `n ≥ 0` such that `h(n) = a` and `h(n+1) = b`. -/
theorem calkin_wilf_enumeration (a b : Nat)
    (ha : a > 0) (hb : b > 0) (hcop : Nat.Coprime a b) :
    ∃! n : Nat, h n = a ∧ h (n + 1) = b := by
  have hexists : ∃ n, h n = a ∧ h (n + 1) = b := by
    have hex_ind : ∀ s, ∀ a b, a > 0 → b > 0 → Nat.Coprime a b → a + b = s → ∃ n, h n = a ∧ h (n + 1) = b := by
      intro s
      induction s using Nat.strongRecOn with
      | ind s ih =>
        intro a b ha hb hcop hs
        by_cases h11 : a = 1 ∧ b = 1
        · -- Base case: (1, 1)
          rcases h11 with ⟨rfl, rfl⟩
          use 0
          simp [h_zero, h_one]
        · -- Inductive step
          have hne : (a, b) ≠ (1, 1) := by
            intro h
            simp at h
            tauto
          by_cases hlt : a < b
          · -- Case a < b
            have hparent := (parent_step_decreases a b ha hb hcop hne).1 hlt
            rcases hparent with ⟨hcop', hsum⟩
            have hex := ih (a + (b - a)) (by omega) a (b - a) ha (by omega) hcop' rfl
            rcases hex with ⟨m, hm1, hm2⟩
            use 2 * m + 1
            constructor
            · rw [h_odd]
              exact hm1
            · have h_eq : h (2 * m + 2) = h (m + 1) + h m := by
                have h1 : 2 * m + 2 = 2 * (m + 1) := by omega
                rw [h1]
                rw [h_even (m + 1) (by omega)]
                simp
              rw [h_eq, hm1, hm2]
              omega
          · -- Case b ≤ a, and since (a,b) ≠ (1,1), we have b < a
            have hlt' : b < a := by
              by_contra h
              have hab : a = b := by omega
              have ha1 : a = 1 := by
                rw [← hab] at hcop
                rw [Nat.coprime_self] at hcop
                exact hcop
              have hb1 : b = 1 := by omega
              tauto
            have hparent := (parent_step_decreases a b ha hb hcop hne).2 hlt'
            rcases hparent with ⟨hcop', hsum⟩
            have hex := ih ((a - b) + b) (by omega) (a - b) b (by omega) hb hcop' rfl
            rcases hex with ⟨m, hm1, hm2⟩
            use 2 * m + 2
            constructor
            · have h_eq : h (2 * m + 2) = h (m + 1) + h m := by
                have h1 : 2 * m + 2 = 2 * (m + 1) := by omega
                rw [h1]
                rw [h_even (m + 1) (by omega)]
                simp
              rw [h_eq, hm1, hm2]
              omega
            · have h_eq : h (2 * m + 3) = h (m + 1) := by
                have h1 : 2 * m + 3 = 2 * (m + 1) + 1 := by omega
                rw [h1]
                rw [h_odd]
              rw [h_eq]
              exact hm2
    exact hex_ind (a + b) a b ha hb hcop rfl
  have hunique : ∀ n m, h n = a ∧ h (n + 1) = b → h m = a ∧ h (m + 1) = b → n = m := by
    intro n m hn hm
    have h_ind : ∀ n, ∀ a' b', h n = a' → h (n + 1) = b' → Nat.Coprime a' b' → ∀ m, h m = a' → h (m + 1) = b' → n = m := by
      intro n
      induction n using Nat.strongRecOn with
      | ind n ih =>
        intro a' b' hn_a hn_b hcop' m hm_a hm_b
        cases n with
        | zero =>
          have ha1 : a' = 1 := by
            rw [← hn_a, h_zero]
          have hb1 : b' = 1 := by
            rw [← hn_b, h_one]
          rw [ha1] at hm_a
          rw [hb1] at hm_b
          have hm0 : m = 0 := by
            apply h_unique_one_one m hm_a hm_b
          exact hm0.symm
        | succ n =>
          by_cases hlt : a' < b'
          · -- a' < b': n+1 and m must be odd
            have hn_odd : (n + 1) % 2 = 1 := by
              have h01 : (n + 1) % 2 = 0 ∨ (n + 1) % 2 = 1 := by omega
              cases h01 with
              | inl heven =>
                have h1 : h (n + 1) > h (n + 2) := (h_compare (n + 1)).2.1 (by omega) heven
                have h2 : h (n + 1) = a' := hn_a
                have h3 : h (n + 2) = b' := hn_b
                rw [h2] at h1
                rw [h3] at h1
                omega
              | inr hodd => exact hodd
            have hm_odd : m % 2 = 1 := by
              have h01 : m % 2 = 0 ∨ m % 2 = 1 := by omega
              cases h01 with
              | inl heven =>
                by_cases hm0 : m = 0
                · -- m = 0: h 0 = 1 and h 1 = 1, so a' = b' = 1, contradicting a' < b'
                  rw [hm0] at hm_a hm_b
                  simp [h_zero, h_one] at hm_a hm_b
                  omega
                · -- m > 0
                  have h1 : h m > h (m + 1) := (h_compare m).2.1 (by omega) heven
                  have h2 : h m = a' := hm_a
                  have h3 : h (m + 1) = b' := hm_b
                  rw [h2] at h1
                  rw [h3] at h1
                  omega
              | inr hodd => exact hodd
            have h_eq1 : n + 1 = 2 * ((n + 1) / 2) + 1 := by omega
            rw [h_eq1] at hn_a hn_b
            have hn1 : h ((n + 1) / 2) = a' := by
              have h2 : h (2 * ((n + 1) / 2) + 1) = a' := hn_a
              rw [h_odd] at h2
              exact h2
            have hn2 : h ((n + 1) / 2 + 1) = b' - a' := by
              have h2 : h (2 * ((n + 1) / 2) + 2) = b' := hn_b
              have h3 : 2 * ((n + 1) / 2) + 2 = 2 * ((n + 1) / 2 + 1) := by omega
              rw [h3] at h2
              rw [h_even ((n + 1) / 2 + 1) (by omega)] at h2
              simp at h2
              omega
            have h_eq2 : m = 2 * (m / 2) + 1 := by omega
            rw [h_eq2] at hm_a hm_b
            have hm1 : h (m / 2) = a' := by
              have h2 : h (2 * (m / 2) + 1) = a' := hm_a
              rw [h_odd] at h2
              exact h2
            have hm2 : h (m / 2 + 1) = b' - a' := by
              have h2 : h (2 * (m / 2) + 2) = b' := hm_b
              have h3 : 2 * (m / 2) + 2 = 2 * (m / 2 + 1) := by omega
              rw [h3] at h2
              rw [h_even (m / 2 + 1) (by omega)] at h2
              simp at h2
              omega
            have hlt' : b' - a' > 0 := Nat.sub_pos_of_lt hlt
            have hcop'' : Nat.Coprime a' (b' - a') := by
              have hle : a' ≤ b' := Nat.le_of_lt hlt
              rw [Nat.coprime_sub_self_right hle]
              exact hcop'
            have hlt_n : (n + 1) / 2 < n + 1 := by omega
            have heq := ih ((n + 1) / 2) hlt_n a' (b' - a') hn1 hn2 hcop'' (m / 2) hm1 hm2
            have h_eq3 : (n + 1) / 2 = m / 2 := heq
            have h_eq4 : n + 1 = m := by
              have h1 : n + 1 = 2 * ((n + 1) / 2) + 1 := by omega
              have h2 : m = 2 * (m / 2) + 1 := by omega
              rw [h1, h2, h_eq3]
            exact h_eq4
          · -- a' ≥ b'
            by_cases hgt : a' > b'
            · -- a' > b': n+1 and m must be even
              have hn_even : (n + 1) % 2 = 0 := by
                have h01 : (n + 1) % 2 = 0 ∨ (n + 1) % 2 = 1 := by omega
                cases h01 with
                | inl heven => exact heven
                | inr hodd =>
                  have h_eq : n + 1 = 2 * ((n + 1) / 2) + 1 := by omega
                  rw [h_eq] at hn_a hn_b
                  have h1 : h (2 * ((n + 1) / 2) + 1) = h ((n + 1) / 2) := by
                    apply h_odd
                  have h2 : h (2 * ((n + 1) / 2) + 2) = h ((n + 1) / 2 + 1) + h ((n + 1) / 2) := by
                    have h3 : 2 * ((n + 1) / 2) + 2 = 2 * ((n + 1) / 2 + 1) := by omega
                    rw [h3]
                    rw [h_even ((n + 1) / 2 + 1) (by omega)]
                    simp
                  rw [h1] at hn_a
                  rw [h2] at hn_b
                  have h3 : h ((n + 1) / 2 + 1) > 0 := h_pos ((n + 1) / 2 + 1)
                  omega
              have hm_even : m % 2 = 0 := by
                have h01 : m % 2 = 0 ∨ m % 2 = 1 := by omega
                cases h01 with
                | inl heven => exact heven
                | inr hodd =>
                  have h_eq : m = 2 * (m / 2) + 1 := by omega
                  rw [h_eq] at hm_a hm_b
                  have h1 : h (2 * (m / 2) + 1) = h (m / 2) := by
                    apply h_odd
                  have h2 : h (2 * (m / 2) + 2) = h (m / 2 + 1) + h (m / 2) := by
                    have h3 : 2 * (m / 2) + 2 = 2 * (m / 2 + 1) := by omega
                    rw [h3]
                    rw [h_even (m / 2 + 1) (by omega)]
                    simp
                  rw [h1] at hm_a
                  rw [h2] at hm_b
                  have h3 : h (m / 2 + 1) > 0 := h_pos (m / 2 + 1)
                  have h4 : b' > a' := by
                    have h5 : b' = h (m / 2 + 1) + h (m / 2) := by omega
                    have h6 : a' = h (m / 2) := by omega
                    rw [h5, h6]
                    omega
                  have h5 : a' > a' := by
                    apply Nat.lt_trans
                    · exact h4
                    · exact hgt
                  exfalso
                  exact Nat.lt_irrefl a' h5
              have h_eq1 : n + 1 = 2 * ((n + 1) / 2) := by omega
              rw [h_eq1] at hn_a hn_b
              have hk : h ((n + 1) / 2) = b' := by
                have h2 : h (2 * ((n + 1) / 2) + 1) = b' := hn_b
                rw [h_odd] at h2
                exact h2
              have hk_prev : h ((n + 1) / 2 - 1) = a' - b' := by
                have h2 : h (2 * ((n + 1) / 2)) = a' := hn_a
                rw [h_even ((n + 1) / 2) (by omega)] at h2
                omega
              have h_eq2 : m = 2 * (m / 2) := by omega
              have hm2 : m ≥ 2 := by
                by_contra h
                have : m = 0 := by omega
                rw [this] at hm_a hm_b
                simp [h_zero, h_one] at hm_a hm_b
                omega
              have hm2' : m / 2 ≥ 1 := by omega
              rw [h_eq2] at hm_a hm_b
              have hj : h (m / 2) = b' := by
                have h2 : h (2 * (m / 2) + 1) = b' := hm_b
                rw [h_odd] at h2
                exact h2
              have hj_prev : h (m / 2 - 1) = a' - b' := by
                have h2 : h (2 * (m / 2)) = a' := hm_a
                rw [h_even (m / 2) hm2'] at h2
                omega
              have hgt' : a' - b' > 0 := by omega
              have hcop'' : Nat.Coprime (a' - b') b' := by
                have hle : b' ≤ a' := by omega
                rw [Nat.coprime_sub_self_left hle]
                exact hcop'
              have hlt_n : (n + 1) / 2 - 1 < n + 1 := by omega
              have hk' : h ((n + 1) / 2 - 1 + 1) = b' := by
                have : (n + 1) / 2 - 1 + 1 = (n + 1) / 2 := by omega
                rw [this]
                exact hk
              have hj' : h (m / 2 - 1 + 1) = b' := by
                have : m / 2 - 1 + 1 = m / 2 := by omega
                rw [this]
                exact hj
              have heq := ih ((n + 1) / 2 - 1) hlt_n (a' - b') b' hk_prev hk' hcop'' (m / 2 - 1) hj_prev hj'
              have h_eq3 : (n + 1) / 2 - 1 = m / 2 - 1 := heq
              have h_eq4 : (n + 1) / 2 = m / 2 := by omega
              have h_eq5 : n + 1 = m := by
                have h1 : n + 1 = 2 * ((n + 1) / 2) := by omega
                have h2 : m = 2 * (m / 2) := by omega
                rw [h1, h2, h_eq4]
              exact h_eq5
            · -- a' = b'
              have heq : a' = b' := by omega
              have ha1 : a' = 1 := by
                rw [← heq] at hcop'
                rw [Nat.coprime_self] at hcop'
                exact hcop'
              have hb1 : b' = 1 := by omega
              have hn1 : h (n + 1) = 1 := by
                rw [ha1] at hn_a
                exact hn_a
              have hn2 : h (n + 2) = 1 := by
                rw [hb1] at hn_b
                exact hn_b
              have hn0 : n + 1 = 0 := by
                apply h_unique_one_one (n + 1) hn1 hn2
              omega
    exact h_ind n a b hn.1 hn.2 hcop m hm.1 hm.2
  rcases hexists with ⟨n, hn⟩
  exact ⟨n, hn, fun m hm => (hunique n m hn hm).symm⟩

/-- The function `n ↦ qₙ` is a bijection from the nonnegative integers
    to the positive rational numbers. -/
theorem explicit_positive_rational_listing :
    Function.Injective (fun n : Nat => q n) ∧
    ∀ r : Rat, r > 0 → ∃ n : Nat, q n = r := by
  constructor
  · -- Injectivity: if q n = q m, then n = m
    intro n m h_eq
    have h_eq' : q n = q m := by
      simpa using h_eq
    have h1 : q n = (h n : Rat) / (h (n + 1)) := by rfl
    have h2 : q m = (h m : Rat) / (h (m + 1)) := by rfl
    rw [h1, h2] at h_eq'
    have h3 : Nat.Coprime (h n) (h (n + 1)) := consecutive_coprime n
    have h4 : Nat.Coprime (h m) (h (m + 1)) := consecutive_coprime m
    have h5 : (h n : Int) = (h m : Int) ∧ (h (n + 1) : Int) = (h (m + 1) : Int) := by
      apply Rat.div_int_inj
      · exact_mod_cast show h (n + 1) > 0 by apply h_pos
      · exact_mod_cast show h (m + 1) > 0 by apply h_pos
      · simp; exact h3
      · simp; exact h4
      · exact_mod_cast h_eq'
    have hn1 : h n = h m := by exact_mod_cast h5.1
    have hn2 : h (n + 1) = h (m + 1) := by exact_mod_cast h5.2
    have hcop : Nat.Coprime (h n) (h (n + 1)) := consecutive_coprime n
    have h_unique := calkin_wilf_enumeration (h n) (h (n + 1))
      (by apply h_pos) (by apply h_pos) hcop
    rcases h_unique with ⟨k, hk1, hk2⟩
    have hk_n : n = k := hk2 n ⟨rfl, rfl⟩
    have hk_m : m = k := hk2 m ⟨hn1.symm, hn2.symm⟩
    exact Eq.trans hk_n hk_m.symm
  · -- Surjectivity: for every positive rational r, there exists n such that q n = r
    intro r hr
    have hr_denom_pos : r.den > 0 := by
      exact r.den_pos
    have hcop : Nat.Coprime r.num.natAbs r.den := by
      exact r.reduced
    have h_num_pos : r.num.natAbs > 0 := by
      have h1 : r.num > 0 := Rat.num_pos.mpr hr
      have h2 : r.num.natAbs ≠ 0 := by
        rw [Int.natAbs_ne_zero]
        exact ne_of_gt h1
      exact Nat.zero_lt_of_ne_zero h2
    have hexists := calkin_wilf_enumeration r.num.natAbs r.den h_num_pos hr_denom_pos hcop
    rcases hexists with ⟨n, ⟨hn1, hn2⟩, hunique⟩
    use n
    have h1 : q n = (h n : Rat) / (h (n + 1)) := by rfl
    rw [h1]
    have h2 : (h n : Int) = (r.num.natAbs : Int) := by
      exact_mod_cast hn1
    have h3 : (h (n + 1) : Int) = (r.den : Int) := by
      exact_mod_cast hn2
    have h4 : (h n : Rat) = (r.num.natAbs : Rat) := by
      exact_mod_cast h2
    have h5 : (h (n + 1) : Rat) = (r.den : Rat) := by
      exact_mod_cast h3
    rw [h4, h5]
    have h6 : (r.num.natAbs : Rat) = (r.num : Rat) := by
      have h7 : r.num > 0 := Rat.num_pos.mpr hr
      have h8 : (r.num.natAbs : Int) = r.num := by
        rw [Int.natAbs_of_nonneg]
        exact Int.le_of_lt h7
      have h9 : (r.num.natAbs : Rat) = ((r.num.natAbs : Int) : Rat) := by rfl
      rw [h9, h8]
    rw [h6]
    have h7 : (r : Rat) = (r.num : Rat) / (r.den : Rat) := by
      exact Eq.symm (Rat.num_div_den r)
    exact h7.symm
