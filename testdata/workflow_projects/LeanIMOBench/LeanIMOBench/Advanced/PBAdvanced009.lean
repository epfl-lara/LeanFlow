import Mathlib

/-
Let $H$ be the orthocenter of an acute-angled triangle $A B C$, and let $D, E, F$ be the feet of the altitudes from vertices $A, B, C$ to the opposite sides, respectively. Let $G$ be the midpoint of $B C$. Let $I, J$ be the feet of the perpendiculars from $B, C$ to $AG$, respectively. Let $K (\neq D)$ be the second intersection of the circumcircles of triangle $D I F$ and triangle $D J E$. Let $M$ be the midpoint of segment $A H$. Let $L$ be the foot of the perpendicular from $M$ to $A G$. Let $R (\neq G)$ be the second intersection of the circumcircle of triangle $A H G$ with $B C$. Let $S$ be the intersection of line $A H$ and $E F$. Let $N$ be the foot of the perpendicular from point $D$ to $R S$. Let $O$ be the midpoint of segment $D N$. Let line $D N$ intersect the circumcircle of triangle $D K L$ again at point $P (\neq D)$. Let $Q (\neq C)$ be the second intersection of the circumcircle of triangle $O C P$ and line $B C$. Prove that $A B=A Q$.
-/

open Affine Simplex EuclideanGeometry
local notation "ℝ²" => EuclideanSpace ℝ (Fin 2)

theorem PBAdvanced009
    -- Acute-angled triangle $A, B, C$
    (A B C : ℝ²) (tri : AffineIndependent ℝ ![A, B, C])
    (acute : AcuteAngled ⟨![A, B, C], tri⟩)
    (D : ℝ²) (hD : D = altitudeFoot ⟨![A, B, C], tri⟩ 0) -- D is the foot from A
    (E : ℝ²) (hE : E = altitudeFoot ⟨![A, B, C], tri⟩ 1) -- E is the foot from B
    (F : ℝ²) (hF : F = altitudeFoot ⟨![A, B, C], tri⟩ 2) -- F is the foot from C
    (H : ℝ²) (hH : H = Triangle.orthocenter ⟨![A, B, C], tri⟩) -- H is the orthocenter
    (G : ℝ²) (hG : G = midpoint ℝ B C) -- $G$ is the midpoint of $B C$
    -- $I, J$ are the feet of the perpendiculars from $B, C$ to $AG$
    (I : ℝ²) (hI : I = orthogonalProjection (affineSpan ℝ {A, G}) B)
    (J : ℝ²) (hJ : J = orthogonalProjection (affineSpan ℝ {A, G}) C)
    -- K ≠ D is the second intersection of the circumcircles of triangles $D I F$ and $D J E$
    (K : ℝ²) (hK : K ≠ D ∧ Cospherical {K, D, I, F} ∧ Cospherical {K, D, J, E})
    (M : ℝ²) (hM : M = midpoint ℝ A H) -- M is the midpoint of segment $A H$
    -- Let $L$ be the foot of the perpendicular from $M$ to $A G$.
    (L : ℝ²) (hL : L = orthogonalProjection (affineSpan ℝ {A, G}) M)
    -- Let R ≠ G be the second intersection of the circumcircle of triangle $A H G$ with $B C$.
    (R : ℝ²) (hR : (R ≠ G ∧ Cospherical {R, A, H, G} ∧ Collinear ℝ {R, B, C}))
    -- Let $S$ be the intersection of line $A H$ and $E F$.
    (S : ℝ²) (hS : Collinear ℝ {S, A, H} ∧ Collinear ℝ {S, E, F})
    -- Let $N$ be the foot of the perpendicular from point $D$ to $R S$.
    (N : ℝ²) (hN : N = orthogonalProjection (affineSpan ℝ {R, S}) D)
    -- Let $O$ be the midpoint of segment $D N$.
    (O : ℝ²) (hO : O = midpoint ℝ D N)
    -- Let line $D N$ intersect the circumcircle of triangle $D K L$ again at point $P (\neq D)$.
    (P : ℝ²) (hP : P ≠ D ∧ Collinear ℝ {P, D, N} ∧ Cospherical {P, D, K, L})
    -- Let $Q (\neq C)$ be the second intersection of the circumcircle of triangle $O C P$ and line $B C$.
    (Q : ℝ²) (hQ : Q ≠ C ∧ Cospherical {Q, O, C, P} ∧ Collinear ℝ {Q, B, C}) :
    -- Prove that $A B=A Q$.
    dist A B = dist A Q := by sorry
