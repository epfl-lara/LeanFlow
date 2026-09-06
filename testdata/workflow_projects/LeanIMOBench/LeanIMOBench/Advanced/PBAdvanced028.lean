import Mathlib

/-
Let $H$ be the orthocenter of acute triangle $ABC$, let $F$ be
the foot of the altitude from $C$ to $AB$, and let $P$ be the reflection
of $H$ across $BC$. Suppose that the circumcircle of triangle $AFP$
intersects line $BC$ at two distinct points $X$ and $Y$. Prove
that $C$ is the midpoint of $XY$.
-/
open Affine Simplex EuclideanGeometry

local notation "ℝ²" => EuclideanSpace ℝ (Fin 2)

theorem PBAdvanced028
    -- an acute-angled triangle ABC
    (A B C : ℝ²) (tri : AffineIndependent ℝ ![A, B, C])
    (h_acute : AcuteAngled ⟨![A, B, C], tri⟩)
    (H : ℝ²) (hH : H = Triangle.orthocenter ⟨![A, B, C], tri⟩) -- H is the orthocenter
    (F : ℝ²) (hF : F = altitudeFoot ⟨![A, B, C], tri⟩ 2) -- F is the foot from C
    -- P be the reflection of H across BC
    (P : ℝ²) (hP : P = reflection (affineSpan ℝ {B, C}) H)
    -- the circumcircle of triangle AFP intersects line BC at two distinct points X and Y
    (X Y : ℝ²) (hXY : X ≠ Y ∧ Collinear ℝ {X, Y, B, C} ∧ Cospherical {X, Y, A, F, P}) :
    C = midpoint ℝ X Y := by sorry
