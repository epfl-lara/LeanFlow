import Mathlib

open Topology Filter Set Polynomial Function Matrix Nat Real Complex
open MeasureTheory Metric MvPolynomial
open scoped InnerProductSpace


/--
Let $a,b,c$ be positive real numbers that are the side lengths of a triangle. Prove
\[
a^2(b+c-a)+b^2(c+a-b)+c^2(a+b-c)\le 3abc.
\]
-/
theorem imo_1964_p2
    (a b c : ℝ)
    (h_pos : 0 < a ∧ 0 < b ∧ 0 < c)
    (h₁ : c < a + b)
    (h₂ : b < a + c)
    (h₃ : a < b + c) :
    a ^ 2 * (b + c - a) + b ^ 2 * (c + a - b) + c ^ 2 * (a + b - c) ≤ 3 * a * b * c := by
  rcases h_pos with ⟨ha, hb, hc⟩
  nlinarith [sq_nonneg (a - b), sq_nonneg (b - c), sq_nonneg (c - a),
    sq_nonneg (a + b - c), sq_nonneg (b + c - a), sq_nonneg (c + a - b),
    mul_pos ha hb, mul_pos hb hc, mul_pos ha hc,
    mul_pos (sub_pos.mpr h₁) (sub_pos.mpr h₂),
    mul_pos (sub_pos.mpr h₂) (sub_pos.mpr h₃),
    mul_pos (sub_pos.mpr h₃) (sub_pos.mpr h₁)]

/--
Suppose real $x$ and rational $m$ satisfy
\[
\sec x+\tan x=\frac{22}{7},\qquad \csc x+\cot x=m.
\]
Prove that the numerator plus denominator of $m$ is $44$.
-/
theorem aime_1991_p9
    (x : ℝ)
    (m : ℚ)
    (h₁ : 1 / Real.cos x + Real.tan x = 22 / 7)
    (h₂ : 1 / Real.sin x + 1 / Real.tan x = m) :
    ↑m.den + m.num = 44 := by
  have hcos : Real.cos x ≠ 0 := by
    by_contra h
    rw [h] at h₁
    have htan0 : Real.tan x = 0 := by
      rw [Real.tan_eq_sin_div_cos, h]
      norm_num
    rw [htan0] at h₁
    norm_num at h₁
  have hsin : Real.sin x ≠ 0 := by
    by_contra h
    have h3 : Real.sin x ^ 2 + Real.cos x ^ 2 = 1 := Real.sin_sq_add_cos_sq x
    rw [h] at h3
    have h4 : Real.cos x ^ 2 = 1 := by nlinarith
    have h5 : Real.cos x = 1 ∨ Real.cos x = -1 := by
      rw [sq_eq_one_iff] at h4
      exact h4
    cases h5 with
    | inl h6 =>
      rw [h6] at h₁
      have htan0 : Real.tan x = 0 := by
        rw [Real.tan_eq_sin_div_cos, h, h6]
        norm_num
      rw [htan0] at h₁
      norm_num at h₁
    | inr h7 =>
      rw [h7] at h₁
      have htan0 : Real.tan x = 0 := by
        rw [Real.tan_eq_sin_div_cos, h, h7]
        norm_num
      rw [htan0] at h₁
      norm_num at h₁
  have htan : Real.tan x ≠ 0 := by
    by_contra h
    have h3 : Real.tan x = Real.sin x / Real.cos x := Real.tan_eq_sin_div_cos x
    rw [h3] at h
    have h4 : Real.sin x = 0 := by
      field_simp [hcos] at h
      linarith
    contradiction
  have h3 : Real.sin x ^ 2 + Real.cos x ^ 2 = 1 := Real.sin_sq_add_cos_sq x
  have h4 : Real.tan x = Real.sin x / Real.cos x := Real.tan_eq_sin_div_cos x
  have h5 : (1 + Real.sin x) / Real.cos x = 22 / 7 := by
    have h6 : 1 / Real.cos x + Real.tan x = (1 + Real.sin x) / Real.cos x := by
      rw [h4]
      field_simp [hcos]
    rw [h6] at h₁
    exact h₁
  have h7 : (1 + Real.cos x) / Real.sin x = (m : ℝ) := by
    have h8 : 1 / Real.sin x + 1 / Real.tan x = (1 + Real.cos x) / Real.sin x := by
      rw [h4]
      field_simp [hsin, hcos]
    rw [h8] at h₂
    exact h₂
  have h8 : (m : ℝ) = 29 / 15 := by
    field_simp [hcos] at h5
    field_simp [hsin] at h7
    have h9 : Real.cos x = 7 * (1 + Real.sin x) / 22 := by
      linarith
    rw [h9] at h3
    have h10 : 533 * Real.sin x ^ 2 + 98 * Real.sin x - 435 = 0 := by
      nlinarith
    have h11 : (Real.sin x - 435 / 533) * (Real.sin x + 1) = 0 := by
      nlinarith
    cases mul_eq_zero.mp h11 with
    | inl h12 =>
      have h14 : Real.sin x = 435 / 533 := by linarith
      have h15 : Real.cos x = 308 / 533 := by
        rw [h14] at h9
        nlinarith
      rw [h14, h15] at h7
      norm_num at h7
      linarith
    | inr h13 =>
      have h14 : Real.sin x = -1 := by linarith
      have h15 : Real.cos x = 0 := by
        nlinarith [h3, h14]
      contradiction
  have h9 : m = (29 / 15 : ℚ) := by
    have h10 : (m : ℝ) = (29 / 15 : ℝ) := h8
    have h11 : m = (29 / 15 : ℚ) := by
      rw [← Rat.cast_inj (α := ℝ)]
      simpa using h10
    exact h11
  rw [h9]
  norm_num
