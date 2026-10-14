/** Render frozen PDF bytes locally and navigate physical pages back to inventory candidates. */
import { useEffect, useRef, useState } from "react";
import type { PDFDocumentProxy, RenderTask, TextLayer } from "pdfjs-dist";
import { pdfItemCandidates, type FormalizationItem, type FormalizationPdf, type FormalizationSourceRef } from "../../src/core/formalization";

interface PdfResources { worker: string; fonts: string; cmaps: string; iccs: string }

export function FormalizationPdfView({ pdf, items, selectedId, selectItem, onSelection, companion = false }: {
  pdf: FormalizationPdf; items: FormalizationItem[]; selectedId: string; selectItem: (id: string) => void; onSelection?: (ref: FormalizationSourceRef) => void; companion?: boolean;
}) {
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
  const [pageIndex, setPageIndex] = useState(pdf.pageIndex);
  const [pageLabel, setPageLabel] = useState("");
  const [selection, setSelection] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [pageInfo, setPageInfo] = useState("");
  const canvas = useRef<HTMLCanvasElement>(null);
  const layer = useRef<HTMLDivElement>(null);
  const pageContainer = useRef<HTMLDivElement>(null);
  const selected = items.find(item => item.id === selectedId)?.source_refs.find(ref => ref.artifact_id === pdf.artifactId);

  useEffect(() => { setPageIndex(pdf.pageIndex); }, [pdf.pageIndex]);
  useEffect(() => {
    let disposed = false;
    let cleanup: (() => void) | undefined;
    setError(""); setDocument(null); setLoading(true);
    void (async () => {
      const resources = (window as unknown as { __LEANFLOW_PDF__?: PdfResources }).__LEANFLOW_PDF__;
      if (!resources) throw new Error("The packaged PDF viewer is unavailable. Reload the extension.");
      const pdfjs = await import("pdfjs-dist/legacy/build/pdf.mjs");
      const response = await fetch(resources.worker);
      if (!response.ok) throw new Error("The packaged PDF worker could not be loaded.");
      const workerUrl = URL.createObjectURL(new Blob([await response.text()], { type: "text/javascript" }));
      const port = new Worker(workerUrl, { type: "module" });
      const worker = pdfjs.PDFWorker.create({ port });
      const bytes = Uint8Array.from(atob(pdf.base64), character => character.charCodeAt(0));
      const task = pdfjs.getDocument({
        data: bytes, worker, cMapUrl: resources.cmaps, cMapPacked: true,
        standardFontDataUrl: resources.fonts, iccUrl: resources.iccs,
        useWorkerFetch: false, useWasm: false, enableXfa: false, verbosity: 0,
      });
      cleanup = () => { void task.destroy(); worker.destroy(); port.terminate(); URL.revokeObjectURL(workerUrl); };
      if (disposed) { cleanup(); return; }
      const loaded = await task.promise;
      if (disposed) return;
      setDocument(loaded);
      setPageIndex(index => Math.max(0, Math.min(index, loaded.numPages - 1)));
    })().catch(reason => { if (!disposed) { setError(String(reason)); setLoading(false); } });
    return () => { disposed = true; cleanup?.(); };
  }, [pdf.base64]);

  useEffect(() => {
    if (!document || !canvas.current || !layer.current || !pageContainer.current) return;
    let disposed = false;
    let render: RenderTask | undefined;
    let textLayer: TextLayer | undefined;
    setLoading(true); setSelection(""); setError("");
    void (async () => {
      const page = await document.getPage(pageIndex + 1);
      const pdfjs = await import("pdfjs-dist/legacy/build/pdf.mjs");
      if (disposed || !canvas.current || !layer.current || !pageContainer.current) return;
      const unscaled = page.getViewport({ scale: 1 });
      const width = Math.min(1000, Math.max(320, pageContainer.current.parentElement?.clientWidth ?? 700));
      const viewport = page.getViewport({ scale: width / unscaled.width });
      const context = canvas.current.getContext("2d");
      if (!context) throw new Error("The PDF canvas could not be created.");
      canvas.current.width = Math.ceil(viewport.width);
      canvas.current.height = Math.ceil(viewport.height);
      pageContainer.current.style.width = `${viewport.width}px`;
      pageContainer.current.style.height = `${viewport.height}px`;
      pageContainer.current.style.setProperty("--total-scale-factor", String(viewport.scale));
      layer.current.replaceChildren();
      render = page.render({ canvas: canvas.current, canvasContext: context, viewport });
      const text = await page.getTextContent();
      if (disposed) return;
      textLayer = new pdfjs.TextLayer({ textContentSource: text, container: layer.current, viewport });
      await Promise.all([render.promise, textLayer.render()]);
      if (disposed) return;
      const labels = await document.getPageLabels();
      setPageLabel(labels?.[pageIndex] ?? "");
      setPageInfo(`Rotation ${page.rotate}° · crop ${page.view.join(", ")}`);
      setLoading(false);
    })().catch(reason => { if (!disposed) { setError(String(reason)); setLoading(false); } });
    return () => { disposed = true; render?.cancel(); textLayer?.cancel(); };
  }, [document, pageIndex]);

  useEffect(() => {
    if (selected?.page_index !== undefined && document) setPageIndex(Math.max(0, Math.min(selected.page_index, document.numPages - 1)));
  }, [selectedId, document, selected?.page_index]);

  const candidates = pdfItemCandidates(items, pdf.artifactId, pageIndex, selection);
  return <section className="formalization-pdf" aria-label="Frozen PDF source">
    <div className="toolbar">
      <button className="btn" disabled={!document || pageIndex === 0} onClick={() => setPageIndex(index => index - 1)}>Previous page</button>
      <label>Physical page <input aria-label="Physical PDF page" type="number" min={1} max={document?.numPages ?? 1} value={pageIndex + 1}
        onChange={event => setPageIndex(Math.min(Math.max(0, Number(event.target.value) - 1), (document?.numPages ?? 1) - 1))} /></label>
      <span>of {document?.numPages ?? "…"}{pageLabel ? ` · printed ${pageLabel}` : ""}</span>
      <button className="btn" disabled={!document || pageIndex + 1 >= document.numPages} onClick={() => setPageIndex(index => index + 1)}>Next page</button>
    </div>
    <p className="muted">Select text in the PDF to narrow candidate source items. Page links remain available when text extraction is missing.</p>
    {companion && <p className="notice">This PDF is a visual companion. Check approximate page links against the rendered page; repair statement boundaries in the frozen TeX source.</p>}
    {selected && <p className="notice">Mapping: {selected.precision ?? "missing"}{selected.method ? ` · ${selected.method}` : ""}. {selected.text || "No passage alignment recorded."}</p>}
    {companion && selected?.page_index === undefined && <p className="notice warn">No page match is available for this source item. Browse the PDF to check it; the displayed page is not a claimed match.</p>}
    {loading && <p role="status">Rendering frozen page…</p>}
    {error && <p className="notice error" role="alert">{error}</p>}
    <div className="formalization-pdf-scroll">
      <div className="formalization-pdf-page" ref={pageContainer} onMouseUp={() => {
        const selectedText = window.getSelection()?.toString().trim() ?? "";
        setSelection(selectedText.slice(0, 16000));
        onSelection?.({ artifact_id: pdf.artifactId, page_index: pageIndex, text: selectedText.slice(0, 16000) });
      }}>
        <canvas ref={canvas} aria-label={`PDF physical page ${pageIndex + 1}`} />
        <div className="textLayer" ref={layer} />
      </div>
    </div>
    <p className="muted">{pageInfo}</p>
    <div aria-live="polite">
      <strong>{selection ? "Selected passage candidates" : "Items on this page"}</strong>
      {selection && <><blockquote>{selection}</blockquote><button className="btn" onClick={() => setSelection("")}>Show all page items</button></>}
      {candidates.length ? <ul>{candidates.map(item => <li key={item.id}><button className="link" onClick={() => selectItem(item.id)}>{item.title}</button></li>)}</ul>
        : <p>No linked items match this {selection ? "passage" : "page"}. {companion ? "Check the frozen TeX source to add a missing item or repair its boundary." : "Add a missing item or repair a boundary below."} This does not establish that the page contains no mathematics.</p>}
    </div>
  </section>;
}
