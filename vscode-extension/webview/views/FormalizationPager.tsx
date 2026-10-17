/** Bound long Formalization lists to one page so large papers mount a fixed number of rows and forms. */
export const FORMALIZATION_PAGE_SIZE = 25;

/** Clamp a stored page to the current list, which can shrink after a repair, search or filter change. */
export function formalizationPage<T>(rows: T[], page: number): { rows: T[]; page: number } {
  const pages = Math.max(1, Math.ceil(rows.length / FORMALIZATION_PAGE_SIZE));
  const current = Math.min(Math.max(0, page), pages - 1);
  return { rows: rows.slice(current * FORMALIZATION_PAGE_SIZE, (current + 1) * FORMALIZATION_PAGE_SIZE), page: current };
}

/** Show the visible range; a short unfiltered list needs no pager because its section summary counts it. */
export function FormalizationPager({ noun, total, page, onPage, filtered = false }: {
  noun: string; total: number; page: number; onPage: (page: number) => void; filtered?: boolean;
}) {
  const pages = Math.max(1, Math.ceil(total / FORMALIZATION_PAGE_SIZE));
  if (pages === 1 && !filtered) return null;
  const current = Math.min(Math.max(0, page), pages - 1);
  const first = current * FORMALIZATION_PAGE_SIZE + 1;
  const last = Math.min(total, first + FORMALIZATION_PAGE_SIZE - 1);
  const range = first === last ? `${first}` : `${first}–${last}`;
  return <nav className="toolbar" aria-label={`${noun} pages`}>
    <span aria-live="polite">{total ? `Showing ${range} of ${total} ${noun}${pages > 1 ? ` · page ${current + 1} of ${pages}` : ""}` : `No matching ${noun}`}</span>
    {pages > 1 && <>
      <button className="btn secondary" aria-label={`Previous ${noun} page`} disabled={current === 0} onClick={() => onPage(current - 1)}>Previous</button>
      <button className="btn secondary" aria-label={`Next ${noun} page`} disabled={current + 1 >= pages} onClick={() => onPage(current + 1)}>Next</button>
    </>}
  </nav>;
}
