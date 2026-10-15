/** Account for every frozen source region in bounded pages, listing regions that need repair first by default. */
import { useMemo, useState } from "react";
import type { FormalizationAction, FormalizationRegion } from "../../src/core/formalization";
import { FORMALIZATION_PAGE_SIZE, FormalizationPager, formalizationPage } from "./FormalizationPager";
import { FormalizationRegionRepair } from "./FormalizationRepair";
import { FormalizationSection, type SectionStatus } from "./FormalizationSection";
import { plural, regionState, type RegionState } from "./formalizationProgress";

type RegionFilter = RegionState | "all";

function searchText(region: FormalizationRegion): string {
  return [region.id, region.title, region.classification, region.disposition, region.reason,
    ...(region.source_refs ?? []).flatMap(ref => [ref.file, ref.page_index !== undefined ? `page ${ref.page_index + 1}` : "", ref.line_start !== undefined ? `line ${ref.line_start}` : ""]),
  ].join(" ").toLowerCase();
}

export function FormalizationRegions({ regions, disabled, submit }: {
  regions: FormalizationRegion[]; disabled: boolean; submit: (action: FormalizationAction) => void;
}) {
  const counts = useMemo(() => {
    const result: Record<RegionState, number> = { repair: 0, open: 0, accounted: 0 };
    for (const region of regions) result[regionState(region)] += 1;
    return result;
  }, [regions]);
  const [show, setShow] = useState<RegionFilter>(counts.repair ? "repair" : "all");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  // Short lists fit on one page, so they skip the filter and search controls entirely.
  const long = regions.length > FORMALIZATION_PAGE_SIZE;
  const filter = long ? show : "all";
  const needle = long ? query.trim().toLowerCase() : "";
  const filtered = useMemo(() => regions.filter(region => (filter === "all" || regionState(region) === filter) && (!needle || searchText(region).includes(needle))), [regions, filter, needle]);
  const visible = formalizationPage(filtered, page);
  const status: SectionStatus[] = [
    ...(counts.repair ? [{ label: `${counts.repair} need${counts.repair === 1 ? "s" : ""} repair`, tone: "warn" as const }] : []),
    ...(counts.open ? [{ label: `${counts.open} not yet accounted` }] : []),
    { label: plural(regions.length, "region") },
  ];
  return <FormalizationSection id="formalization-regions" title="Source region accounting" status={status} attention={counts.repair > 0}>
    <p className="muted">A complete formalization proposal must account for every region before the controller accepts its coverage. Regions that need repair require a verified page transcription or an explicit exclusion first.</p>
    {long && <div className="toolbar">
      <label>Show<select aria-label="Source region filter" value={show} onChange={event => { setShow(event.target.value as RegionFilter); setPage(0); }}>
        <option value="repair">Need repair ({counts.repair})</option>
        <option value="open">Not yet accounted ({counts.open})</option>
        <option value="accounted">Accounted ({counts.accounted})</option>
        <option value="all">All regions ({regions.length})</option>
      </select></label>
      <label>Search regions<input aria-label="Search source regions" placeholder="Title, file, page or line" value={query} onChange={event => { setQuery(event.target.value); setPage(0); }} /></label>
    </div>}
    <FormalizationPager noun="source regions" total={filtered.length} page={visible.page} onPage={setPage} filtered={filter !== "all" || !!needle} />
    {visible.rows.map(region => <FormalizationRegionRepair key={region.id} region={region} disabled={disabled} submit={submit} />)}
    {!regions.length && <p>No source regions are recorded for this revision.</p>}
  </FormalizationSection>;
}
