import Mathlib

/-
Given an acute triangle $ABC$, let $D$ and $E$ be the feet of
the altitudes from $B$ to $AC$ and $C$ to $AB$, respectively.
Let $E_{1}$ and $E_{2}$ be the reflections of $E$ with respect
to $AC$ and $BC$, respectively. If $X$ (not equal to $C$) is an
intersection point of the circumcircle of $\triangle CE_{1}E_{2}$
and $AC$, and $O$ is the circumcenter of $\triangle CE_{1}E_{2}$,
prove that $XO$ is perpendicular to $DE$.
-/
open Real Affine Simplex EuclideanGeometry
local notation "ℝ²" => EuclideanSpace ℝ (Fin 2)

theorem PBBasic027
    (A B C : ℝ²) (tri : AffineIndependent ℝ ![A, B, C])
    (acute : AcuteAngled ⟨![A, B, C], tri⟩) :
    let D := altitudeFoot ⟨![A, B, C], tri⟩ 1
    let E := altitudeFoot ⟨![A, B, C], tri⟩ 2
    let E₁ := reflection (affineSpan ℝ {A, C}) E
    let E₂ := reflection (affineSpan ℝ {B, C}) E
    ∀ X : ℝ², X ≠ C → Cospherical {X, C, E₁, E₂} → Collinear ℝ {X, A, C} →
    ∀ (tri2 : AffineIndependent ℝ ![C, E₁, E₂]),
    let O := circumcenter ⟨![C, E₁, E₂], tri2⟩
    (affineSpan ℝ {X, O}).direction ⟂ (affineSpan ℝ {D, E}).direction := by sorry
