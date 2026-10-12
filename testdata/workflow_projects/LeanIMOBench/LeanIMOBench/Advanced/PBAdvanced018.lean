import Mathlib

/-
For given integers $n \ge 5$ and $k \ge 1$, we color each of the $n^2$ cells of an $n \times n$ grid  using one of $k$ colors. If $q$ is the largest integer which is not larger than $\frac{n^2}{k}$, then, each of the $k$ colors must be used to color exactly $q$ or $q+1$ cells. A sequence of $n$ different cells $c_1, c_2, \ldots, c_n$ in the grid is called a \textit{snake} if it satisfies the following conditions simultaneously:

\begin{enumerate}
\item[(a)] For each $1 \le i \le n-1$, two cells $c_i$ and $c_{i+1}$ are adjacent to their sides,
\item[(b)] For each $1 \le i \le n-1$, cell $c_i$ and cell $c_{i+1}$ are colored with different colors.
\end{enumerate}
Let $a(n)$ be the minimum $k$ such that a snake exists regardless of the method of coloring. Find a constant $L$ that satisfies the following inequality and prove it:

\[
|La(n)- n^2 | \le n +2 \sqrt n + 3 \;.
\]

Answer: 3
-/

-- The set of valid coloring: each of the $k$ colors must be used to color exactly $q$ or $q+1$ cells
def validColorings (n k : ℕ) : Set (Matrix (Fin n) (Fin n) (Fin k)) :=
  let q : ℕ := ⌊ n^2 / k ⌋₊
  { M |
    ∀ c : Fin k, let cn := Finset.card {coor : Fin n × Fin n | M.uncurry coor = c}
    cn = q ∨ cn = q+1
  }

-- Definition of a snake on a fixed grid
structure Snake {n k : ℕ} (M : Matrix (Fin n) (Fin n) (Fin k)) where
  c : List (Fin n × Fin n)
  c_nodup : c.Nodup -- the cells must be different
  c_len : c.length = n
  c_adj : c.Chain' (fun (y₁,x₁) (y₂,x₂) ↦
    abs (x₁ - x₂ : ℤ) + abs (y₁ - y₂ : ℤ) = 1 ∧ -- the cells are adjacent
    M y₁ x₁ ≠ M y₂ x₂ -- and they have a different color
  )

-- Let $a(n)$ be the minimum $k$ such that a snake exists regardless of the method of coloring.
-- We are given k ≥ 1 among the main assumptions, so we will force k ≥ 1 here.
noncomputable
def a (n : ℕ) := sInf { k : ℕ | k ≥ 1 ∧ ∀ M ∈ validColorings n k, Nonempty (Snake M) }

theorem PBAdvanced018 (n : ℕ) (hn : n ≥ 5) : |(3 * a n - n^2 : ℤ)| ≤ n + 2 * √ n + 3 := by sorry
