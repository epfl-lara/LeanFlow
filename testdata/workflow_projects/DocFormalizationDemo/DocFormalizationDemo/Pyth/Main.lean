import Mathlib.NumberTheory.PythagoreanTriples
import Mathlib.Algebra.MvPolynomial.Basic
import Mathlib.Algebra.MvPolynomial.Eval
import Mathlib.Algebra.GCDMonoid.Basic

open MvPolynomial

def IntegerValued4 (F : ℤ → ℤ → ℤ → ℤ → ℚ) : Prop :=
  ∀ x y z w : ℤ, ∃ n : ℤ, (n : ℚ) = F x y z w

def IntegerValued4OnPositiveNonnegative (F : ℤ → ℤ → ℤ → ℤ → ℚ) : Prop :=
  ∀ x y z w : ℤ, x > 0 → y > 0 → z > 0 → w ≥ 0 → ∃ n : ℤ, (n : ℚ) = F x y z w

def IntegerValued16 (F : (Fin 16 → ℤ) → ℚ) : Prop :=
  ∀ a : Fin 16 → ℤ, ∃ n : ℤ, (n : ℚ) = F a

/-! ## Rational parametrization T(a,b,c)

The source defines `T(a,b,c) = (c(a²-b²)/2, cab, c(a²+b²)/2)`.
These are rational-valued functions that yield Pythagorean triples.
-/

def T_x (a b c : ℤ) : ℚ := (c * (a^2 - b^2)) / 2
def T_y (a b c : ℤ) : ℚ := c * a * b
def T_z (a b c : ℤ) : ℚ := (c * (a^2 + b^2)) / 2

/-- `T(a,b,c)` always satisfies the Pythagorean equation.

Source proof: direct verification by expanding
`T_x² + T_y² = T_z²`.
-/
theorem T_is_pythagorean (a b c : ℤ) :
    (T_x a b c)^2 + (T_y a b c)^2 = (T_z a b c)^2 := by
  simp [T_x, T_y, T_z]
  ring

/-- `T(a,b,c)` yields integers iff `c` is even or `a ≡ b (mod 2)`.

Source proof: `T_y` is always integral. `T_x` and `T_z` involve division by 2,
so they are integral exactly when `c(a²-b²)` and `c(a²+b²)` are even.
Since `a²-b²` and `a²+b²` have the same parity, this happens iff `c` is even
or `a²-b²` is even, i.e. `a ≡ b (mod 2)`.
-/
theorem T_integer_iff (a b c : ℤ) :
    (∃ x y z : ℤ, (x : ℚ) = T_x a b c ∧ (y : ℚ) = T_y a b c ∧ (z : ℚ) = T_z a b c) ↔
      c % 2 = 0 ∨ a % 2 = b % 2 := by
  constructor
  · -- Forward: if T(a,b,c) gives integers, then c even or a ≡ b (mod 2)
    rintro ⟨x, y, z, hx, hy, hz⟩
    have h1 : 2 * x = c * (a^2 - b^2) := by
      have h : (2 * x : ℚ) = c * (a^2 - b^2) := by
        rw [hx]
        simp [T_x]
        ring
      exact_mod_cast h
    by_cases hc : c % 2 = 0
    · left; exact hc
    · right
      have hc' : c % 2 = 1 := by omega
      have h3 : (a^2 - b^2) % 2 = 0 := by
        have h4 : 2 ∣ c * (a^2 - b^2) := by
          use x
          rw [h1]
        have h5 : c % 2 = 1 := hc'
        have h6 : (c * (a^2 - b^2)) % 2 = 0 := by
          rw [Int.dvd_iff_emod_eq_zero] at h4
          exact h4
        simp [Int.mul_emod, h5] at h6
        omega
      have ha0 : a % 2 = 0 ∨ a % 2 = 1 := by omega
      have hb0 : b % 2 = 0 ∨ b % 2 = 1 := by omega
      rcases ha0 with (ha | ha)
      · rcases hb0 with (hb | hb)
        · have : a % 2 = b % 2 := by rw [ha, hb]
          exact this
        · have h6 : a^2 % 2 = 0 := by
            rw [pow_two, Int.mul_emod, ha]
            norm_num
          have h7 : b^2 % 2 = 1 := by
            rw [pow_two, Int.mul_emod, hb]
            norm_num
          have h8 : (a^2 - b^2) % 2 = 1 := by
            rw [Int.sub_emod, h6, h7]
            norm_num
          rw [h8] at h3
          exfalso
          linarith
      · rcases hb0 with (hb | hb)
        · have h6 : a^2 % 2 = 1 := by
            rw [pow_two, Int.mul_emod, ha]
            norm_num
          have h7 : b^2 % 2 = 0 := by
            rw [pow_two, Int.mul_emod, hb]
            norm_num
          have h8 : (a^2 - b^2) % 2 = 1 := by
            rw [Int.sub_emod, h6, h7]
            norm_num
          rw [h8] at h3
          exfalso
          linarith
        · have : a % 2 = b % 2 := by rw [ha, hb]
          exact this
  · -- Backward: if c even or a ≡ b (mod 2), then T(a,b,c) gives integers
    rintro (hc | hab)
    · -- c % 2 = 0
      have ⟨k, hk⟩ : ∃ k : ℤ, c = 2 * k := ⟨c / 2, by omega⟩
      use k * (a^2 - b^2), c * a * b, k * (a^2 + b^2)
      constructor
      · simp [T_x, hk]
        ring
      constructor
      · simp [T_y]
      · simp [T_z, hk]
        ring
    · -- a % 2 = b % 2
      have h1 : (a^2 - b^2) % 2 = 0 := by
        have h2 : a % 2 = 0 ∨ a % 2 = 1 := by omega
        have h3 : b % 2 = 0 ∨ b % 2 = 1 := by omega
        rcases h2 with (ha | ha)
        · rcases h3 with (hb | hb)
          · have h4 : a^2 % 2 = 0 := by
              rw [pow_two, Int.mul_emod, ha]
              norm_num
            have h5 : b^2 % 2 = 0 := by
              rw [pow_two, Int.mul_emod, hb]
              norm_num
            rw [Int.sub_emod, h4, h5]
            norm_num
          · exfalso
            have h4 : (0 : ℤ) = 1 := by
              rw [← ha, hab, hb]
            norm_num at h4
            all_goals tauto
        · rcases h3 with (hb | hb)
          · exfalso
            have h4 : (1 : ℤ) = 0 := by
              rw [← ha, hab, hb]
            norm_num at h4
            all_goals tauto
          · have h4 : a^2 % 2 = 1 := by
              rw [pow_two, Int.mul_emod, ha]
              norm_num
            have h5 : b^2 % 2 = 1 := by
              rw [pow_two, Int.mul_emod, hb]
              norm_num
            rw [Int.sub_emod, h4, h5]
            norm_num
      have h2 : (a^2 + b^2) % 2 = 0 := by
        have h3 : a % 2 = 0 ∨ a % 2 = 1 := by omega
        have h4 : b % 2 = 0 ∨ b % 2 = 1 := by omega
        rcases h3 with (ha | ha)
        · rcases h4 with (hb | hb)
          · have h5 : a^2 % 2 = 0 := by
              rw [pow_two, Int.mul_emod, ha]
              norm_num
            have h6 : b^2 % 2 = 0 := by
              rw [pow_two, Int.mul_emod, hb]
              norm_num
            rw [Int.add_emod, h5, h6]
            norm_num
          · exfalso
            have h5 : (0 : ℤ) = 1 := by
              rw [← ha, hab, hb]
            norm_num at h5
            all_goals tauto
        · rcases h4 with (hb | hb)
          · exfalso
            have h5 : (1 : ℤ) = 0 := by
              rw [← ha, hab, hb]
            norm_num at h5
            all_goals tauto
          · have h5 : a^2 % 2 = 1 := by
              rw [pow_two, Int.mul_emod, ha]
              norm_num
            have h6 : b^2 % 2 = 1 := by
              rw [pow_two, Int.mul_emod, hb]
              norm_num
            rw [Int.add_emod, h5, h6]
            norm_num
      have ⟨k1, hk1⟩ : ∃ k1 : ℤ, c * (a^2 - b^2) = 2 * k1 := by
        have h3 : 2 ∣ (a^2 - b^2) := by
          rw [Int.dvd_iff_emod_eq_zero]
          exact h1
        have h4 : 2 ∣ c * (a^2 - b^2) := by
          apply dvd_mul_of_dvd_right
          exact h3
        rcases h4 with ⟨k1, hk1⟩
        exact ⟨k1, hk1⟩
      have ⟨k2, hk2⟩ : ∃ k2 : ℤ, c * (a^2 + b^2) = 2 * k2 := by
        have h3 : 2 ∣ (a^2 + b^2) := by
          rw [Int.dvd_iff_emod_eq_zero]
          exact h2
        have h4 : 2 ∣ c * (a^2 + b^2) := by
          apply dvd_mul_of_dvd_right
          exact h3
        rcases h4 with ⟨k2, hk2⟩
        exact ⟨k2, hk2⟩
      use k1, c * a * b, k2
      constructor
      · simp [T_x]
        have h : (c * (a^2 - b^2) : ℚ) = 2 * (k1 : ℚ) := by exact_mod_cast hk1
        linarith
      constructor
      · simp [T_y]
      · simp [T_z]
        have h : (c * (a^2 + b^2) : ℚ) = 2 * (k2 : ℚ) := by exact_mod_cast hk2
        linarith

/-- Every Pythagorean triple is of the form `T(a,b,c)` for some integers `a,b,c`.

Source proof: Every primitive PT with `z>0` is `T₁(a,b)` or `T₂(a,b)`.
Since `2·T₂(a,b) = T₁(a+b,a-b)`, every primitive PT is `c·T₁(a,b)/2` with
`c ∈ {1,2}`. Scaling gives the general case.
-/
theorem pythagoreanTriple_eq_T (x y z : ℤ) (h : PythagoreanTriple x y z) :
    ∃ a b c : ℤ, (x : ℚ) = T_x a b c ∧ (y : ℚ) = T_y a b c ∧ (z : ℚ) = T_z a b c := by
  sorry

/-! ## Integer-valued polynomial parametrization (Theorem, line-194)

The explicit triple from the source:
```
f = (2x-xw)((y+zw)²-(z-yw)²)/2
g = (2x-xw)(y+zw)(z-yw)
h = (2x-xw)((y+zw)²+(z-yw)²)/2
```
-/

def f_param (x y z w : ℤ) : ℚ := ((2 * x - x * w) * ((y + z * w)^2 - (z - y * w)^2)) / 2
def g_param (x y z w : ℤ) : ℚ := (2 * x - x * w) * (y + z * w) * (z - y * w)
def h_param (x y z w : ℤ) : ℚ := ((2 * x - x * w) * ((y + z * w)^2 + (z - y * w)^2)) / 2

/-- The source's displayed rational formulas are integer-valued on all integer inputs.

Source proof: Substitute `a = y+zw`, `b = z-yw`, `c = 2x-xw` into the
integrality condition for `T(a,b,c)`.
-/
theorem param_integer_valued :
    IntegerValued4 f_param ∧ IntegerValued4 g_param ∧ IntegerValued4 h_param := by
  sorry

/-- The integer-valued parametrization `(f_param, g_param, h_param)` covers all Pythagorean triples.

Source proof: Substitute `a = y+zw`, `b = z-yw`, `c = 2x-xw` into `T(a,b,c)`.
If `w` is even then `c` is even; if `w` is odd then `a ≡ b (mod 2)`.
Conversely, any `(a,b,c)` with `c` even or `a ≡ b (mod 2)` arises
by setting `w=0` or `w=1` appropriately.
-/
theorem integer_valued_parametrization :
    IntegerValued4 f_param ∧ IntegerValued4 g_param ∧ IntegerValued4 h_param ∧
    {(x, y, z) : ℤ × ℤ × ℤ | PythagoreanTriple x y z} =
    {(x, y, z) : ℤ × ℤ × ℤ | ∃ a b c d : ℤ,
      (x : ℚ) = f_param a b c d ∧ (y : ℚ) = g_param a b c d ∧ (z : ℚ) = h_param a b c d} := by
  sorry

/-! ## Positive Pythagorean triples (Remark, line-240)

The explicit triple from the source:
```
f = (x+(1-w)²x)((y+(1+w)z)²-y²)/2
g = (x+(1-w)²x)(y+(1+w)z)y
h = (x+(1-w)²x)((y+(1+w)z)²+y²)/2
```
where `x,y,z > 0` and `w ≥ 0`.
-/

def f_pos (x y z w : ℤ) : ℚ := ((x + (1 - w)^2 * x) * ((y + (1 + w) * z)^2 - y^2)) / 2
def g_pos (x y z w : ℤ) : ℚ := (x + (1 - w)^2 * x) * (y + (1 + w) * z) * y
def h_pos (x y z w : ℤ) : ℚ := ((x + (1 - w)^2 * x) * ((y + (1 + w) * z)^2 + y^2)) / 2

def fourSquares (a b c d : ℤ) : ℤ := a^2 + b^2 + c^2 + d^2
def fourSquaresPos (a b c d : ℤ) : ℤ := fourSquares a b c d + 1

def f_pos16 (a : Fin 16 → ℤ) : ℚ :=
  f_pos
    (fourSquaresPos (a 0) (a 1) (a 2) (a 3))
    (fourSquaresPos (a 4) (a 5) (a 6) (a 7))
    (fourSquaresPos (a 8) (a 9) (a 10) (a 11))
    (fourSquares (a 12) (a 13) (a 14) (a 15))

def g_pos16 (a : Fin 16 → ℤ) : ℚ :=
  g_pos
    (fourSquaresPos (a 0) (a 1) (a 2) (a 3))
    (fourSquaresPos (a 4) (a 5) (a 6) (a 7))
    (fourSquaresPos (a 8) (a 9) (a 10) (a 11))
    (fourSquares (a 12) (a 13) (a 14) (a 15))

def h_pos16 (a : Fin 16 → ℤ) : ℚ :=
  h_pos
    (fourSquaresPos (a 0) (a 1) (a 2) (a 3))
    (fourSquaresPos (a 4) (a 5) (a 6) (a 7))
    (fourSquaresPos (a 8) (a 9) (a 10) (a 11))
    (fourSquares (a 12) (a 13) (a 14) (a 15))

/-- The positive-parametrization formulas are integer-valued on their stated domain.

Source proof: Substitute `a = y+(1+w)z`, `b = y`,
`c = x+(1-w)²x` into the integrality condition for `T(a,b,c)`.
-/
theorem positive_param_integer_valued :
    IntegerValued4OnPositiveNonnegative f_pos ∧
    IntegerValued4OnPositiveNonnegative g_pos ∧
    IntegerValued4OnPositiveNonnegative h_pos := by
  sorry

/-- The parametrization `(f_pos, g_pos, h_pos)` covers all positive Pythagorean triples.

Source proof: Positive PTs are `T(a,b,c)` with `a,b,c > 0`, `a > b`, and
`c` even or `a ≡ b (mod 2)`. Such triples are parametrized by
`(a,b,c) = (y+(1+w)z, y, x+(1-w)²x)` with `x,y,z > 0` and `w ≥ 0`.
Substituting into `T` gives `f_pos, g_pos, h_pos`.
-/
theorem positive_pythagorean_parametrization :
    IntegerValued4OnPositiveNonnegative f_pos ∧
    IntegerValued4OnPositiveNonnegative g_pos ∧
    IntegerValued4OnPositiveNonnegative h_pos ∧
    {(x, y, z) : ℤ × ℤ × ℤ | x > 0 ∧ y > 0 ∧ z > 0 ∧ PythagoreanTriple x y z} =
    {(x, y, z) : ℤ × ℤ × ℤ | ∃ a b c d : ℤ, a > 0 ∧ b > 0 ∧ c > 0 ∧ d ≥ 0 ∧
      (x : ℚ) = f_pos a b c d ∧ (y : ℚ) = g_pos a b c d ∧ (z : ℚ) = h_pos a b c d} := by
  sorry

/-- The four-square substitution gives a parametrization of positive Pythagorean triples
with unrestricted integer parameters.

Source proof: use the four-square theorem to replace each positive parameter by
a sum of four squares plus one, and the nonnegative parameter by a sum of four squares.
-/
theorem positive_pythagorean_parametrization_integer_parameters :
    IntegerValued16 f_pos16 ∧ IntegerValued16 g_pos16 ∧ IntegerValued16 h_pos16 ∧
    {(x, y, z) : ℤ × ℤ × ℤ | x > 0 ∧ y > 0 ∧ z > 0 ∧ PythagoreanTriple x y z} =
    {(x, y, z) : ℤ × ℤ × ℤ | ∃ a : Fin 16 → ℤ,
      (x : ℚ) = f_pos16 a ∧ (y : ℚ) = g_pos16 a ∧ (z : ℚ) = h_pos16 a} := by
  sorry

/-! ## No integer-coefficient polynomial parametrization (Remark, line-143)

The source proves that no single triple of polynomials with integer coefficients
in any number of variables can parametrize all Pythagorean triples.
-/

/-- There do not exist `f,g,h ∈ ℤ[x₁,…,xₙ]` (for any `n`) that parametrize all PTs.

Source proof: Suppose `(f,g,h)` parametrizes PTs. In the UFD `ℤ[x]`, let `d = gcd(g,h)`.
Then `d | f` and setting `φ=f/d`, `ψ=g/d`, `θ=h/d` gives `φ² = (θ+ψ)(θ-ψ)`.
The gcd of `θ+ψ` and `θ-ψ` is 1 or 2; it cannot be 2 because of `(3,4,5)`.
So `θ+ψ` and `θ-ψ` are coprime squares, yielding `θ=(s²+t²)/2`, `ψ=(s²-t²)/2`.
Then `ψ` is divisible by 2, contradicting `(4,3,5)`.
-/
theorem no_integer_polynomial_parametrization (n : ℕ) :
    ¬∃ (f g h : MvPolynomial (Fin n) ℤ),
      {(x, y, z) : ℤ × ℤ × ℤ | PythagoreanTriple x y z} =
      Set.range (fun (a : Fin n → ℤ) => (eval a f, eval a g, eval a h)) := by
  sorry
