import Mathlib

/-
Let $O$ and $G$ be the circumcenter and centroid of a non-isosceles triangle $ABC$, respectively. Let $H$ be the foot of the perpendicular from $A$ to $BC$, and let $M$ be the midpoint of $BC$. For a point $X$ on the line $OG$, not lying on any of the side lines $AB$, $BC$, $CA$, let the line $BX$ intersect $AC$ at $P$, and let the line $CX$ intersect $AB$ at $Q$. Let $H_1$ be the foot of the perpendicular from $P$ to the line $AB$, and let $K$ be the reflection of $A$ about $H_1$, with $K \neq Q$. Let $T$ be the second intersection of the circumcircle of triangle $KPQ$ and the circumcircle of triangle $PHM$, assuming the two circumcircles are distinct. Prove that as $X$ moves along the line $OG$, $T$ moves along a fixed circle.
-/
open Affine.Simplex EuclideanGeometry
local notation "ℝ²" => EuclideanSpace ℝ (Fin 2)

theorem PBAdvanced010
    -- a triangle ABC
    (A B C : ℝ²) (tri : AffineIndependent ℝ ![A, B, C])
    -- ABC is non-isosceles
    (n_isosceles : [dist A B, dist A C, dist B C].Nodup )
    (O : ℝ²) (hO : O = circumcenter ⟨![A, B, C], tri⟩) -- O is the circumcenter
    (G : ℝ²) (hG : G = Finset.centroid ℝ .univ ![A, B, C]) -- G is the centroid
    (H : ℝ²) (hH : H = altitudeFoot ⟨![A, B, C], tri⟩ 0) -- H is the foot from A
    (M : ℝ²) (hM : M = midpoint ℝ B C) : -- $M$ is the midpoint of $B C$
    -- There is a sphere ω independent of the point `X`
    ∃ ω : Sphere ℝ²,
    -- For a point $X$ on the line $OG$, not on any side line,
    ∀ (X : ℝ²) (hX : Collinear ℝ {X, O, G} ∧ X ∉ (affineSpan ℝ {A, B} : Set ℝ²) ∪ (affineSpan ℝ {A, C} : Set ℝ²) ∪ (affineSpan ℝ {B, C} : Set ℝ²))
    -- let the line $BX$ intersect $AC$ at $P$,
    (P : ℝ²) (hP : Collinear ℝ {P, B, X} ∧ Collinear ℝ {P, A, C})
    -- let the line $CX$ intersect $AB$ at $Q$.
    (Q : ℝ²) (hQ : Collinear ℝ {Q, C, X} ∧ Collinear ℝ {Q, A, B})
    -- Let $H_1$ be the foot of the perpendicular from $P$ to the line $AB$,
    (H₁ : ℝ²) (hH₁ : H₁ = orthogonalProjection (affineSpan ℝ {A, B}) P)
    -- let $K$ be the reflection of $A$ about $H_1$.
    (K : ℝ²) (hK : K = reflection (affineSpan ℝ {H₁}) A)
    -- K ≠ Q (non-degeneracy: prevents 3-point trivial cosphericality)
    (hK_ne_Q : K ≠ Q)
    -- The two circumcircles are distinct (non-degeneracy: prevents coincidence at X = G)
    (hCircles_ne : ¬ Cospherical ({K, P, Q, H, M} : Set ℝ²))
    -- Let $T$ be the second intersection of the circumcircle of triangle $KPQ$ and the circumcircle of triangle $PHM$.
    (T : ℝ²) (hT : T ≠ P ∧ Cospherical {T, K, P, Q} ∧ Cospherical {T, P, H, M}),
    -- Prove that as $X$ moves along the line $OG$, $T$ moves along a fixed circle.
    T ∈ ω := by sorry
