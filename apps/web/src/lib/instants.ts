/** Compare ISO instants without losing PostgreSQL microseconds to JavaScript Date rounding. */
export function compareInstants(left: string, right: string): number {
  const milliseconds = (Date.parse(left) || 0) - (Date.parse(right) || 0);
  if (milliseconds !== 0) return milliseconds;
  const fraction = (value: string) => /\.(\d+)(?:Z|[+-]\d{2}:?\d{2})$/i.exec(value)?.[1]?.slice(3) ?? "";
  const a = fraction(left);
  const b = fraction(right);
  const digits = Math.max(a.length, b.length);
  return a.padEnd(digits, "0").localeCompare(b.padEnd(digits, "0"));
}
