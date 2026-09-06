import Mathlib

/-
Find all functions$f:\mathbb{Z}\rightarrow\mathbb{Z}$
such that the equation
\[
f(x-f(xy))=f(x)f(1-y)
\]
holds for all $x,y\in\mathbb{Z}$.

Solution: $f_{1}(x)\equiv0$, $f_{2}(x)\equiv1$,
$f_{3}(x)\equiv x$, $f_{4}(x)=\begin{cases}
0, & x=2n\\
1, & x=2n+1
\end{cases}$, where $n\in\mathbb{Z}$, $f_{5}(x)=\begin{cases}
0, & x=3n\\
1, & x=3n+1\\
-1, & x=3n+2
\end{cases}$, where $n\in\mathbb{Z}$
-/
theorem PBAdvanced006 :
    {f : ℤ → ℤ | ∀ x y, f (x - f (x * y)) = f x * f (1 - y)}
      = {(fun _ => 0), (fun _ => 1), id, (fun x => x % 2),
          (fun x => if 3 ∣ x then 0 else if 3 ∣ x - 1 then 1
            else -1)} := by sorry
