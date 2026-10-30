/**
 * Спецификация простого графика из fenced-блока ```chart (docs/plan/agent-hub-v3.md §3.3):
 * `{ "type": "bar" | "line", "title"?: string, "labels": string[], "series": [{ "name": string, "values": number[] }] }`.
 * Это данные, а не код: разбор только проверяет форму и пределы, ничего не исполняется.
 */
export type ChartKind = "bar" | "line";

export interface ChartSeries {
  name: string;
  values: number[];
}

export interface ChartSpec {
  type: ChartKind;
  title?: string;
  labels: string[];
  series: ChartSeries[];
}

export const CHART_LIMITS = {
  labels: 50,
  series: 6,
  title: 120,
  label: 40,
  seriesName: 40,
  /** Больше по модулю значение не рисуется: шкала и подписи разваливаются, а такие числа в учёбе не встречаются. */
  maxAbs: 1e15,
} as const;

const clip = (value: string, max: number): string => {
  const text = value.replace(/\s+/g, " ").trim();
  return text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text;
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/**
 * JSON → `ChartSpec` или `null`. Строки обрезаются по `CHART_LIMITS`; число вместо подписи (год) становится
 * строкой. Неверный тип, длины рядов не по числу подписей, нечисловые и бесконечные значения — `null`.
 */
export function parseChartSpec(raw: string): ChartSpec | null {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(data)) return null;
  if (data.type !== "bar" && data.type !== "line") return null;
  if (!Array.isArray(data.labels) || !Array.isArray(data.series)) return null;
  if (data.labels.length < 1 || data.labels.length > CHART_LIMITS.labels) return null;
  if (data.series.length < 1 || data.series.length > CHART_LIMITS.series) return null;

  const labels: string[] = [];
  for (const label of data.labels) {
    if (typeof label === "number" && Number.isFinite(label))
      labels.push(clip(String(label), CHART_LIMITS.label));
    else if (typeof label === "string") labels.push(clip(label, CHART_LIMITS.label));
    else return null;
  }

  const series: ChartSeries[] = [];
  for (const [index, item] of data.series.entries()) {
    if (!isRecord(item) || !Array.isArray(item.values)) return null;
    if (item.values.length !== labels.length) return null;
    if (
      !item.values.every(
        (value) =>
          typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= CHART_LIMITS.maxAbs,
      )
    )
      return null;
    const name = typeof item.name === "string" ? clip(item.name, CHART_LIMITS.seriesName) : "";
    series.push({ name: name || `Серия ${index + 1}`, values: item.values as number[] });
  }

  const title = typeof data.title === "string" ? clip(data.title, CHART_LIMITS.title) : "";
  return { type: data.type, ...(title ? { title } : {}), labels, series };
}

export interface NiceScale {
  min: number;
  max: number;
  step: number;
  ticks: number[];
}

/** Знаков после запятой, нужных, чтобы записать шаг (0.25 → 2). */
function decimals(step: number): number {
  let count = 0;
  let value = step;
  while (count < 6 && Math.abs(value - Math.round(value)) > 1e-9) {
    value *= 10;
    count++;
  }
  return count;
}

/**
 * «Красивая» ось: границы и шаг кратны 1, 2 или 5 степени десяти, 0 попадает на засечку, если он внутри
 * диапазона. `target` — желаемое число интервалов.
 */
export function niceScale(low: number, high: number, target = 4): NiceScale {
  let min = Math.min(low, high);
  let max = Math.max(low, high);
  if (min === max) {
    const pad = Math.abs(min) > 0 ? Math.abs(min) * 0.1 : 1;
    min -= pad;
    max += pad;
  }
  const rough = (max - min) / Math.max(1, target);
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const residual = rough / magnitude;
  // Пороги √(2·5), √(5·10), √(1·2): ближайший «круглый» множитель по логарифмической шкале.
  const factor = residual > 7.07 ? 10 : residual > 3.16 ? 5 : residual > 1.41 ? 2 : 1;
  const step = factor * magnitude;
  const places = decimals(step);
  const niceMin = Number((Math.floor(min / step + 1e-9) * step).toFixed(places));
  const niceMax = Number((Math.ceil(max / step - 1e-9) * step).toFixed(places));
  const ticks: number[] = [];
  const count = Math.round((niceMax - niceMin) / step);
  for (let index = 0; index <= count; index++) ticks.push(Number((niceMin + index * step).toFixed(places)));
  return { min: niceMin, max: niceMax, step, ticks };
}

/** Диапазон значений всех рядов. */
export function chartExtent(spec: ChartSpec): { min: number; max: number } {
  const values = spec.series.flatMap((item) => item.values);
  return { min: Math.min(...values), max: Math.max(...values) };
}
