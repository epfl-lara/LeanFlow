import Mathlib

open Topology Filter Set Polynomial Function Matrix Nat Real Complex MeasureTheory Metric MvPolynomial
open scoped InnerProductSpace

/--
Let $\triangle ABC$ be a triangle in the Euclidean plane, with points $P$, $Q$, and $R$ lying on segments $\overline{BC}$, $\overline{CA}$, $\overline{AB}$ respectively such that $$\frac{AQ}{QC} = \frac{BR}{RA} = \frac{CP}{PB} = k$$ for some positive constant $k$. If $\triangle UVW$ is the triangle formed by parts of segments $\overline{AP}$, $\overline{BQ}$, and $\overline{CR}$, prove that $$\frac{[\triangle UVW]}{[\triangle ABC]} = \frac{(k - 1)^2}{k^2 + k + 1},$$ where $[\triangle]$ denotes the area of the triangle $\triangle$.
-/
theorem putnam_1962_a3
    (A B C A' B' C' P Q R : EuclideanSpace ℝ (Fin 2))
    (k : ℝ)
    (hk : k > 0)
    (hABC : ¬Collinear ℝ {A, B, C})
    (hA' : A' ∈ segment ℝ B C ∧ dist C A' / dist A' B = k)
    (hB' : B' ∈ segment ℝ C A ∧ dist A B' / dist B' C = k)
    (hC' : C' ∈ segment ℝ A B ∧ dist B C' / dist C' A = k)
    (hP : P ∈ segment ℝ B B' ∧ P ∈ segment ℝ C C')
    (hQ : Q ∈ segment ℝ C C' ∧ Q ∈ segment ℝ A A')
    (hR : R ∈ segment ℝ A A' ∧ R ∈ segment ℝ B B') :
    (volume (convexHull ℝ {P, Q, R})).toReal / (volume (convexHull ℝ {A, B, C})).toReal =
      (k - 1)^2 / (k^2 + k + 1) :=
  sorry

/--
Let $S$ be a finite set of collinear points. Let $k$ be the maximum distance between any two points of $S$. Given a pair of points of $S$ a distance $d < k$ apart, we can find another pair of points of $S$ also a distance $d$ apart. Prove that if two pairs of points of $S$ are distances $a$ and $b$ apart, then $\frac{a}{b}$ is rational.
-/
theorem putnam_1964_a6
    (S : Finset ℝ)
    (pairs : Set (ℝ × ℝ))
    (hpairs : pairs = {(a, b) | (a ∈ S) ∧ (b ∈ S) ∧ (a < b)})
    (distance : ℝ × ℝ → ℝ)
    (hdistance : distance = fun (a, b) => b - a)
    (hrepdist : ∀ p ∈ pairs, (∃ m ∈ pairs, distance m > distance p) → ∃ q ∈ pairs, q ≠ p ∧ distance p = distance q) :
    ∀ p q : pairs, q ≠ p → ∃ r : ℚ, distance p / distance q = r :=
  sorry

/--
Three distinct points with integer coordinates lie in the plane on a circle of radius $r>0$. Show that two of these points are separated by a distance of at least $r^{1/3}$.
-/
theorem putnam_2000_a5
    (r : ℝ)
    (z : EuclideanSpace ℝ (Fin 2))
    (p : Fin 3 → EuclideanSpace ℝ (Fin 2))
    (rpos : r > 0)
    (pdiff : ∀ n m, n ≠ m → p n ≠ p m)
    (pint : ∀ n i, p n i = round (p n i))
    (pcirc : ∀ n, p n ∈ Metric.sphere z r) :
    ∃ n m, n ≠ m ∧ dist (p n) (p m) ≥ r ^ ((1 : ℝ) / 3) :=
  sorry

/--
Given any five points on a sphere, show that some four of them must lie on a closed hemisphere.
-/
theorem putnam_2002_a2
    (unit_sphere : Set (EuclideanSpace ℝ (Fin 3)))
    (hsphere : unit_sphere = sphere 0 1)
    (hemi : EuclideanSpace ℝ (Fin 3) → Set (EuclideanSpace ℝ (Fin 3)))
    (hhemi : hemi = fun V => {P : EuclideanSpace ℝ (Fin 3) | ⟪P, V⟫_ℝ ≥ 0}) :
    ∀ S : Set (EuclideanSpace ℝ (Fin 3)),
      S ⊆ unit_sphere ∧ S.encard = 5 →
        ∃ V : EuclideanSpace ℝ (Fin 3), V ≠ 0 ∧ (S ∩ hemi V).encard ≥ 4 :=
  sorry

/--
Let $f(z)=az^4+bz^3+cz^2+dz+e=a(z-r_1)(z-r_2)(z-r_3)(z-r_4)$ where $a,b,c,d,e$ are integers, $a \neq 0$. Show that if $r_1+r_2$ is a rational number and $r_1+r_2 \neq r_3+r_4$, then $r_1r_2$ is a rational number.
-/
theorem putnam_2003_b4
    (f : ℝ → ℝ)
    (a b c d e : ℤ)
    (r1 r2 r3 r4 : ℝ)
    (ane0 : a ≠ 0)
    (hf1 : ∀ z, f z = a * z ^ 4 + b * z ^ 3 + c * z ^ 2 + d * z + e)
    (hf2 : ∀ z, f z = a * (z - r1) * (z - r2) * (z - r3) * (z - r4)) :
    (¬Irrational (r1 + r2) ∧ r1 + r2 ≠ r3 + r4) → ¬Irrational (r1 * r2) :=
  sorry

/--
Let $f$ be a nonconstant polynomial with positive integer coefficients. Prove that if $n$ is a positive integer, then $f(n)$ divides $f(f(n) + 1)$ if and only if $n = 1$
-/
theorem putnam_2007_b1
    (f : Polynomial ℤ)
    (hf : ∀ n : ℕ, f.coeff n ≥ 0)
    (hfnconst : ∃ n : ℕ, n > 0 ∧ f.coeff n > 0)
    (n : ℤ)
    (hn : n > 0) :
    f.eval n ∣ f.eval (f.eval n + 1) ↔ n = 1 :=
  sorry

/--
Suppose that the real numbers \( a_0, a_1, \ldots, a_n \) and \( x \), with \( 0 < x < 1 \), satisfy $ \frac{a_0}{1-x} + \frac{a_1}{(1-x)^2} + \cdots + \frac{a_n}{(1-x)^{n+1}} = 0. $ Prove that there exists a real number \( y \) with \( 0 < y < 1 \) such that $ a_0 + a_1y + \cdots + a_ny^n = 0. $.
-/
theorem putnam_2013_a3
    (n : ℕ)
    (a : Set.Icc 0 n → ℝ)
    (x : ℝ)
    (hx : 0 < x ∧ x < 1)
    (hsum : (∑ i : Set.Icc 0 n, a i / (1 - x ^ (i.1 + 1))) = 0) :
    ∃ y : ℝ, 0 < y ∧ y < 1 ∧ (∑ i : Set.Icc 0 n, a i * y ^ i.1) = 0 :=
  sorry

/--
Suppose that $P(x)=a_1x+a_2x^2+\cdots+a_nx^n$ is a polynomial with integer coefficients, with $a_1$ odd. Suppose that $e^{P(x)}=b_0+b_1x+b_2x^2+\dots$ for all $x$. Prove that $b_k$ is nonzero for all $k \geq 0$.
-/
theorem putnam_2022_b1
    (P : Polynomial ℤ)
    (b : ℕ → ℝ)
    (Pconst : P.coeff 0 = 0)
    (Podd : Odd (P.coeff 1))
    (hB : ∀ x : ℝ, HasSum (fun i => b i * x ^ i) (Real.exp (aeval x P))) :
    ∀ k : ℕ, b k ≠ 0 :=
  sorry
