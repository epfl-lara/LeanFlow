import Mathlib

/-
Given a triangle $XYZ$ with circumcenter $O$, the incircle of triangle
$XYZ$ has center $I$. Let $M,N$ on the sides $XY,XZ$
respectively such that $YM=ZN=YZ$. If $\gamma$ is the angle created
by two lines $MN,OI$, what is $\frac{\gamma}{2}$ in terms of degree?
-/
open Real Affine Simplex EuclideanGeometry
local notation "ℝ²" => EuclideanSpace ℝ (Fin 2)
local notation degrees "ᵒ" => (degrees * π / 180)

theorem PBBasic025
    -- Given a triangle $XYZ$
    (X Y Z : ℝ²) (tri : AffineIndependent ℝ ![X, Y, Z]) :
    -- with circumcenter $O$,
    let O := circumcenter ⟨![X, Y, Z], tri⟩
    -- the incircle of triangle $XYZ$ has center $I$.
    let I := incenter ⟨![X, Y, Z], tri⟩
    -- Let $M,N$ on the sides $XY,XZ$ respectively such that $YM=ZN=YZ$.
    ∀ M N : ℝ², Sbtw ℝ X M Y → Sbtw ℝ X N Z →
    dist Y M = dist Y Z → dist Z N = dist Y Z →
    -- take an auxiliary point `p` at the intersection of `MN` and `OI`
    -- and two helper points `p₁ p₂` on the lines to define the angle `gamma`
    ∀ (p p₁ p₂ : ℝ²), p ∈ affineSpan ℝ {M, N} → p ∈ affineSpan ℝ {O, I} →
    p₁ ∈ affineSpan ℝ {M, N} → p₂ ∈ affineSpan ℝ {O, I} →
    let γ := ∠ p₁ p p₂
    -- it is true that once we know `γ / 2` is supposed to equal `45ᵒ`, we don't
    -- need the condition γ ≤ 90ᵒ but in general, we probably mean to take
    -- the non-obtuse angle when measuring an angle between lines
    γ ≤ 90ᵒ → γ / 2 = 45ᵒ
      := by sorry
