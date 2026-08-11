/** Merge run activity without duplicating events after cursor rotation. */
import type { ActivityEvent } from "./types";

export interface EventBufferMerge {
  buffer: ActivityEvent[];
  appended: ActivityEvent[];
  /** True when consumers must replace their tail because older rows were evicted. */
  trimmed: boolean;
}

/**
 * Append events not already present and retain only the newest bounded tail.
 *
 * The CLI intentionally returns a tail when a cursor has fallen out of a
 * rotated stream. Event ids are durable within a run, so filtering that tail
 * prevents both the host cache and webview from displaying it twice.
 */
export function mergeActivityEvents(
  existing: readonly ActivityEvent[],
  incoming: readonly ActivityEvent[],
  limit: number,
): EventBufferMerge {
  const seen = new Set(
    existing.map((event) => event.event_id).filter((eventId) => Boolean(eventId)),
  );
  const appended: ActivityEvent[] = [];
  for (const event of incoming) {
    if (event.event_id && seen.has(event.event_id)) {
      continue;
    }
    if (event.event_id) {
      seen.add(event.event_id);
    }
    appended.push(event);
  }
  const merged = [...existing, ...appended];
  const boundedLimit = Math.max(1, Math.trunc(limit) || 1);
  const trimmed = merged.length > boundedLimit;
  return {
    buffer: trimmed ? merged.slice(merged.length - boundedLimit) : merged,
    appended,
    trimmed,
  };
}
