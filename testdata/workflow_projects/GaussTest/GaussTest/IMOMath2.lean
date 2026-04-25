import Mathlib

open Topology Filter Set Polynomial Function Matrix Nat Real Complex
open MeasureTheory Metric MvPolynomial
open scoped InnerProductSpace

/--
Let $S$ be the set of real numbers $x$ in $[0,\pi]$ satisfying
\[
\sin\left(\frac{\pi}{2}\cos x\right)=\cos\left(\frac{\pi}{2}\sin x\right).
\]
Prove that $S$ has exactly two elements.
-/
theorem amc12a_2021_p19
    (S : Finset ℝ)
    (hS :
      ∀ x : ℝ,
        x ∈ S ↔
          0 ≤ x ∧
          x ≤ Real.pi ∧
          Real.sin (Real.pi / 2 * Real.cos x) = Real.cos (Real.pi / 2 * Real.sin x)) :
    S.card = 2 := by
  sorry


/--
For every positive natural number $n$, prove
\[
(3^{2^n}-1)\bmod 2^{n+3}=2^{n+2}.
\]
-/
theorem numbertheory_3pow2pownm1mod2pownp3eq2pownp2
    (n : ℕ)
    (h_pos : 0 < n) :
    (3 ^ 2 ^ n - 1) % 2 ^ (n + 3) = 2 ^ (n + 2) := by
  sorry

/--
Let $n$ be a positive integer such that $n+1$ is divisible by $24$. Prove that the sum of all the divisors of $n$ is divisible by $24$.
-/
theorem putnam_1969_b1
    (n : ℕ)
    (hnpos : n > 0)
    (hn : 24 ∣ n + 1) :
    24 ∣ ∑ d ∈ divisors n, d :=
  sorry

/--
Let $a_n$ denote the sequence $0, 1, 1, 2, 2, 3, \dots$, where $a_n = \frac{n}{2}$ if $n$ is even and $\frac{n - 1}{2}$ if n is odd. Furthermore, let $f(n)$ denote the sum of the first $n$ terms of $a_n$. Prove that all positive integers $x$ and $y$ with $x > y$ satisfy $xy = f(x + y) - f(x - y)$.
-/
theorem putnam_1966_a1
    (f : ℤ → ℤ)
    (hf : f = fun n : ℤ => ∑ m ∈ Finset.Icc 0 n, if Even m then m / 2 else (m - 1) / 2) :
    ∀ x y : ℤ, x > 0 ∧ y > 0 ∧ x > y → x * y = f (x + y) - f (x - y) :=
  sorry

/--
Let $\{f(n)\}$ be a strictly increasing sequence of positive integers such that $f(2)=2$ and $f(mn)=f(m)f(n)$ for every relatively prime pair of positive integers $m$ and $n$ (the greatest common divisor of $m$ and $n$ is equal to $1$). Prove that $f(n)=n$ for every positive integer $n$.
-/
theorem putnam_1963_a2
    (f : ℕ → ℕ)
    (hfpos : ∀ n, f n > 0)
    (hfinc : StrictMonoOn f (Ici 1))
    (hf2 : f 2 = 2)
    (hfmn : ∀ m n, m > 0 → n > 0 → IsRelPrime m n → f (m * n) = f m * f n) :
    ∀ n > 0, f n = n :=
  sorry
