import Mathlib

/-
Let $ABC$ be a non-isosceles triangle with incenter $I$. Let line $BI$ intersect $AC$ at $E$, and line $CI$ intersect $AB$ at $F$. Two Points $U$ and $V$ are on segments $AB$ and $AC$ respectively, such that $AU = AE$ and $AV = AF$. Let the line passing through $I$ and perpendicular to $AI$ intersect line $BC$ at $L$. The circumcircle of $\triangle ILC$ intersects line $LU$ at $X$ (other than $L$), and the circumcircle of triangle $\triangle ILB$ intersects line $LV$ at $Y$ (other than $L$). Prove that if $P$ is the intersection of lines $YB$ and $XC$, then line $IP$ is parallel to line $XY$.
-/
open Affine.Simplex EuclideanGeometry Real
local notation "ℝ²" => EuclideanSpace ℝ (Fin 2)

theorem PBAdvanced016
    -- a triangle ABC
    (A B C : ℝ²) (tri : AffineIndependent ℝ ![A, B, C])
    -- ABC is non-isosceles
    (n_isosceles : [dist A B, dist A C, dist B C].Nodup )
    (I : ℝ²) (hI : I = incenter ⟨![A, B, C], tri⟩) -- I is the incenter
    (E : ℝ²) (hE : Collinear ℝ {E, B, I} ∧ Collinear ℝ {E, A, C}) -- BI intersect AC at E
    (F : ℝ²) (hF : Collinear ℝ {F, C, I} ∧ Collinear ℝ {F, A, B}) -- CI intersect AB at E
    -- Two Points $U$ and $V$ are on segments $AB$ and $AC$ respectively, such that $AU = AE$ and $AV = AF$.
    (U : ℝ²) (hU : Sbtw ℝ A U B ∧ dist A U = dist A E)
    (V : ℝ²) (hV : Sbtw ℝ A V C ∧ dist A V = dist A F)
    -- the line passing through $I$ and perpendicular to $AI$ intersect line $BC$ at $L$.
    (L : ℝ²) (hL : ∠ A I L = π / 2 ∧ Collinear ℝ {L, B, C})
    -- The circumcircle of ILC intersects line LU at X (other than L),
    (X : ℝ²) (hX : X ≠ L ∧ Cospherical {X, I, L, C} ∧ Collinear ℝ {X, L, U})
    -- the circumcircle of ILB intersects line LV at Y (other than L).
    (Y : ℝ²) (hY : Y ≠ L ∧ Cospherical {Y, I, L, B} ∧ Collinear ℝ {Y, L, V})
    -- P is the intersection of lines YB and XC
    (P : ℝ²) (hP : Collinear ℝ {P, Y, B} ∧ Collinear ℝ {P, X, C}) :
    -- Prove that line $IP$ is parallel to line $XY$.
    (affineSpan ℝ {I, P}).Parallel (affineSpan ℝ {X, Y}) := by sorry
