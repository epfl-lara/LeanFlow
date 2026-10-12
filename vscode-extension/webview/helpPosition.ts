/** Place help within the viewport, independently of surrounding scroll containers. */
export function helpPosition(anchor: { left: number; top: number; bottom: number }, height: number, viewportWidth: number, viewportHeight: number) {
  const margin = 12;
  const width = Math.max(0, Math.min(320, viewportWidth - margin * 2));
  const maxHeight = Math.max(0, viewportHeight - margin * 2);
  const actualHeight = Math.min(height, maxHeight);
  const below = anchor.bottom + 8;
  const top = below + actualHeight <= viewportHeight - margin ? below : anchor.top - actualHeight - 8;
  return {
    width, maxHeight,
    left: Math.max(margin, Math.min(anchor.left, viewportWidth - width - margin)),
    top: Math.max(margin, Math.min(top, viewportHeight - actualHeight - margin)),
  };
}
