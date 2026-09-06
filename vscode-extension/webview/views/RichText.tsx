/**
 * Readable rendering for model output and tool payloads.
 *
 * Everything here renders React text nodes: a model's markdown, a tool's JSON,
 * or a Lean file can never inject markup or links into the webview.
 */
import { Fragment, type ReactNode } from "react";

import { classifyText, fieldPresentation } from "../../src/core/eventDetail";

const MAX_LIST_ITEMS = 40;
const MAX_FIELDS = 80;
const MAX_DEPTH = 6;

/** Inline markdown: `code` and **bold** only. */
function inline(text: string): ReactNode {
  const parts: ReactNode[] = [];
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g;
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) {
      parts.push(text.slice(last, match.index));
    }
    const token = match[0];
    parts.push(
      token.startsWith("`") ? (
        <code key={match.index}>{token.slice(1, -1)}</code>
      ) : (
        <strong key={match.index}>{token.slice(2, -2)}</strong>
      ),
    );
    last = match.index + token.length;
  }
  if (last < text.length) {
    parts.push(text.slice(last));
  }
  return <>{parts}</>;
}

/** A short line in capitals is how models label the parts of a report. */
function isShoutedHeading(line: string): boolean {
  const trimmed = line.trim();
  return (
    trimmed.length >= 4 &&
    trimmed.length <= 72 &&
    /^[A-Z0-9][A-Z0-9 ,:;/&()'’\-–—]*$/.test(trimmed) &&
    /[A-Z]{3,}/.test(trimmed) &&
    !/^\d+$/.test(trimmed)
  );
}

export function CodeBlock(props: { text: string; language?: string }) {
  return (
    <div className="code-block">
      {props.language && <span className="code-lang">{props.language}</span>}
      <pre>{props.text}</pre>
    </div>
  );
}

/**
 * Markdown-lite for model prose: headings, bullet and numbered lists, block
 * quotes, rules, and fenced code. Single line breaks are kept, because a
 * model's hard-wrapped report loses its structure when lines are re-flowed.
 */
export function RichText(props: { text: string }) {
  const lines = props.text.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let paragraph: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let code: { language: string; lines: string[] } | null = null;

  const flushParagraph = (): void => {
    if (paragraph.length > 0) {
      const body = paragraph;
      blocks.push(
        <p key={blocks.length}>
          {body.map((line, index) => (
            <Fragment key={index}>
              {index > 0 && <br />}
              {inline(line)}
            </Fragment>
          ))}
        </p>,
      );
      paragraph = [];
    }
  };
  const flushList = (): void => {
    if (list) {
      const items = list.items.map((item, index) => <li key={index}>{inline(item)}</li>);
      blocks.push(list.ordered ? <ol key={blocks.length}>{items}</ol> : <ul key={blocks.length}>{items}</ul>);
      list = null;
    }
  };
  const flushCode = (): void => {
    if (code) {
      blocks.push(
        <CodeBlock key={blocks.length} text={code.lines.join("\n")} language={code.language || undefined} />,
      );
      code = null;
    }
  };

  for (const line of lines) {
    if (code) {
      if (/^\s*```/.test(line)) {
        flushCode();
      } else {
        code.lines.push(line);
      }
      continue;
    }
    const fence = /^\s*```\s*([\w+-]+)?/.exec(line);
    if (fence) {
      flushParagraph();
      flushList();
      code = { language: (fence[1] ?? "").toLowerCase(), lines: [] };
      continue;
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      flushParagraph();
      flushList();
      blocks.push(
        heading[1].length <= 2 ? (
          <h3 key={blocks.length}>{inline(heading[2])}</h3>
        ) : (
          <h4 key={blocks.length}>{inline(heading[2])}</h4>
        ),
      );
      continue;
    }
    const bullet = /^\s*[-*•]\s+(.*)$/.exec(line);
    const numbered = /^\s*(\d+)[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      flushParagraph();
      const ordered = Boolean(numbered);
      if (!list || list.ordered !== ordered) {
        flushList();
        list = { ordered, items: [] };
      }
      list.items.push(bullet ? bullet[1] : numbered![2]);
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      flushParagraph();
      flushList();
      blocks.push(<blockquote key={blocks.length}>{inline(line.replace(/^\s*>\s?/, ""))}</blockquote>);
      continue;
    }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flushParagraph();
      flushList();
      blocks.push(<hr key={blocks.length} />);
      continue;
    }
    if (!line.trim()) {
      flushParagraph();
      flushList();
      continue;
    }
    if (list && /^\s{2,}\S/.test(line)) {
      // An indented continuation belongs to the previous list item.
      list.items[list.items.length - 1] += ` ${line.trim()}`;
      continue;
    }
    flushList();
    if (paragraph.length === 0 && isShoutedHeading(line)) {
      blocks.push(<h4 key={blocks.length}>{line.trim()}</h4>);
      continue;
    }
    paragraph.push(line.trimEnd());
  }
  flushCode();
  flushParagraph();
  flushList();
  return <div className="rich">{blocks}</div>;
}

function isScalar(value: unknown): boolean {
  return value === null || typeof value !== "object";
}

function shortScalarList(value: unknown[]): boolean {
  return (
    value.length <= 12 &&
    value.every((item) => isScalar(item) && String(item).length <= 48)
  );
}

/**
 * Render any JSON value readably: objects as labelled fields, long strings as
 * prose or code, arrays as lists. Field names and a sibling `path` decide how
 * a string reads, so a file written to `Scratch.lean` shows as Lean.
 */
export function ValueView(props: {
  value: unknown;
  name?: string;
  context?: { path?: string };
  depth?: number;
}) {
  const { value, name = "", context = {}, depth = 0 } = props;
  if (typeof value === "string") {
    const presentation = fieldPresentation(name, value, context);
    if (presentation.kind === "inline") {
      return <span className="field-inline">{value}</span>;
    }
    if (presentation.kind === "code") {
      return <CodeBlock text={value} language={presentation.language} />;
    }
    return <RichText text={value} />;
  }
  if (value === null || value === undefined) {
    return <span className="field-inline muted">—</span>;
  }
  if (typeof value === "number") {
    // Token and call counts are read as magnitudes; grouping is what makes
    // 152,261 legible at a glance. Small numbers and ids are left alone.
    const shown =
      Number.isInteger(value) && Math.abs(value) >= 10_000 ? value.toLocaleString() : String(value);
    return <code className="field-scalar">{shown}</code>;
  }
  if (typeof value !== "object") {
    return <code className="field-scalar">{String(value)}</code>;
  }
  if (depth > MAX_DEPTH) {
    return <CodeBlock text={JSON.stringify(value, null, 2)} language="json" />;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return <span className="field-inline muted">none</span>;
    }
    if (shortScalarList(value)) {
      return <span className="field-inline">{value.map(String).join(", ")}</span>;
    }
    const shown = value.slice(0, MAX_LIST_ITEMS);
    return (
      <ol className="field-list">
        {shown.map((item, index) => (
          <li key={index}>
            <ValueView value={item} name={name} context={context} depth={depth + 1} />
          </li>
        ))}
        {value.length > shown.length && (
          <li className="muted">… {value.length - shown.length} more</li>
        )}
      </ol>
    );
  }
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length === 0) {
    return <span className="field-inline muted">empty</span>;
  }
  const own = (value as Record<string, unknown>).path;
  const path = typeof own === "string" ? own : context.path;
  const shown = entries.slice(0, MAX_FIELDS);
  return (
    <dl className="fields">
      {shown.map(([key, item]) => {
        const block =
          typeof item === "string"
            ? fieldPresentation(key, item, { path }).kind !== "inline"
            : !isScalar(item) && !(Array.isArray(item) && shortScalarList(item));
        return (
          <div className={`field${block ? " block" : ""}`} key={key}>
            <dt>{key}</dt>
            <dd>
              <ValueView value={item} name={key} context={{ path }} depth={depth + 1} />
            </dd>
          </div>
        );
      })}
      {entries.length > shown.length && (
        <div className="field muted">… {entries.length - shown.length} more fields</div>
      )}
    </dl>
  );
}

/** Ensure a language label exists for a whole-section code value. */
export function sectionLanguage(text: string, language?: string): string | undefined {
  return language ?? classifyText(text).language;
}
