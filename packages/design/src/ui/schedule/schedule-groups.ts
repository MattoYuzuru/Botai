/** A visual group preserves every event. Touching [start, end) intervals do not overlap. */
export function groupScheduleItems<T extends { id: string; start: number; end: number }>(
  items: readonly T[],
): T[][] {
  const sorted = [...items].sort((a, b) => a.start - b.start || a.end - b.end || a.id.localeCompare(b.id));
  const groups: T[][] = [];
  let until = Number.NEGATIVE_INFINITY;
  for (const item of sorted) {
    const previous = groups.at(-1);
    if (previous && item.start < until && item.end > item.start) {
      previous.push(item);
      until = Math.max(until, item.end);
    } else {
      groups.push([item]);
      until = Math.max(item.start, item.end);
    }
  }
  return groups;
}
