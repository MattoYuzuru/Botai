import { type ReactNode, useId, useLayoutEffect, useRef, useState } from "react";
import { cn } from "../../lib/cn";
import { pluralRu } from "../../lib/format";
import { Button } from "../button/button";
import { type ChartSpec, chartExtent, niceScale } from "./chart-spec";

/** Ряды различаются цветом, формой метки и штрихом: цвет — не единственный носитель смысла. */
const TONES = [
  { fill: "fill-accent", stroke: "stroke-accent", swatch: "bg-accent" },
  { fill: "fill-info", stroke: "stroke-info", swatch: "bg-info" },
  { fill: "fill-warning", stroke: "stroke-warning", swatch: "bg-warning" },
  { fill: "fill-success", stroke: "stroke-success", swatch: "bg-success" },
  { fill: "fill-control", stroke: "stroke-control", swatch: "bg-control" },
  { fill: "fill-fg-muted", stroke: "stroke-fg-muted", swatch: "bg-fg-muted" },
] as const;

const SHAPES = ["circle", "square", "triangle", "diamond", "ring", "cross"] as const;
type Shape = (typeof SHAPES)[number];
const DASHES = [undefined, "6 4", "2 3", "10 3 2 3", "6 4", "2 3"] as const;

const PLOT_HEIGHT = 200;
const PAD_TOP = 20;
const PAD_RIGHT = 12;
const AXIS_HEIGHT = 36;
/** Ширина знака подписи 13 px (Geist) с запасом. */
const CHAR_WIDTH = 7;
/** Меньше этого на категорию — график шире экрана и прокручивается. */
const MIN_BAND = 48;
const WIDE_FROM = 12;
const FALLBACK_WIDTH = 560;
const MIN_LABEL_PX = 56;

const numberFormat = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
const format = (value: number) => numberFormat.format(value);

function useElementWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const read = () => setWidth(Math.round(element.getBoundingClientRect().width));
    read();
    const observer = new ResizeObserver(read);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return { ref, width };
}

function Marker({ shape, x, y, tone }: { shape: Shape; x: number; y: number; tone: (typeof TONES)[number] }) {
  switch (shape) {
    case "square":
      return <rect x={x - 4} y={y - 4} width={8} height={8} className={tone.fill} />;
    case "triangle":
      return <path d={`M${x} ${y - 5}L${x + 5} ${y + 4}L${x - 5} ${y + 4}Z`} className={tone.fill} />;
    case "diamond":
      return <path d={`M${x} ${y - 5}L${x + 5} ${y}L${x} ${y + 5}L${x - 5} ${y}Z`} className={tone.fill} />;
    case "ring":
      return <circle cx={x} cy={y} r={3.5} strokeWidth={2} className={cn("fill-surface", tone.stroke)} />;
    case "cross":
      return (
        <path
          d={`M${x - 4} ${y - 4}L${x + 4} ${y + 4}M${x + 4} ${y - 4}L${x - 4} ${y + 4}`}
          strokeWidth={2}
          className={cn("fill-none", tone.stroke)}
        />
      );
    default:
      return <circle cx={x} cy={y} r={4} className={tone.fill} />;
  }
}

function SeriesKey({ kind, index }: { kind: ChartSpec["type"]; index: number }) {
  const tone = TONES[index % TONES.length] as (typeof TONES)[number];
  if (kind === "bar")
    return <span aria-hidden="true" className={cn("size-3 shrink-0 rounded-xs", tone.swatch)} />;
  return (
    <svg aria-hidden="true" width={24} height={12} viewBox="0 0 24 12" className="shrink-0">
      <line
        x1={0}
        x2={24}
        y1={6}
        y2={6}
        strokeWidth={2}
        strokeDasharray={DASHES[index % DASHES.length]}
        className={tone.stroke}
      />
      <Marker shape={SHAPES[index % SHAPES.length] as Shape} x={12} y={6} tone={tone} />
    </svg>
  );
}

/** Ключ позиции: подписи, ряды и метки одного графика статичны и не переупорядочиваются. */
const slot = (name: string, ...parts: number[]) => [name, ...parts].join("-");

const clipLabel = (label: string, max: number) =>
  label.length > max ? `${label.slice(0, max - 1)}…` : label;

export interface ChartBlockProps {
  spec: ChartSpec;
  className?: string;
}

/**
 * Простой график из fenced-блока ```chart: столбцы или линии, оси и сетка на токенах. Рисуется в пикселях контейнера
 * (подписи всегда 13 px). Доступность: `<figure>`, у графика `role="img"` с описанием, таблица с теми же данными
 * лежит рядом (для скринридера всегда, зрячим — кнопка «Показать таблицу»). Анимации нет.
 */
export function ChartBlock({ spec, className }: ChartBlockProps) {
  const { ref, width: measured } = useElementWidth<HTMLDivElement>();
  const [tableShown, setTableShown] = useState(false);
  const tableId = useId();
  const count = spec.labels.length;
  const bar = spec.type === "bar";
  const extent = chartExtent(spec);
  const low = bar ? Math.min(0, extent.min) : extent.min;
  const high = bar ? Math.max(0, extent.max) : extent.max;
  const scale = niceScale(low, high === low && bar ? low + 1 : high, 4);
  const tickLabels = scale.ticks.map(format);
  const left = Math.max(32, Math.max(...tickLabels.map((label) => label.length)) * CHAR_WIDTH + 12);
  const available = (measured || FALLBACK_WIDTH) - left - PAD_RIGHT;
  const plotWidth = Math.max(available, count > WIDE_FROM ? count * MIN_BAND : 0);
  const width = left + plotWidth + PAD_RIGHT;
  const height = PAD_TOP + PLOT_HEIGHT + AXIS_HEIGHT;
  const scrollable = width > (measured || FALLBACK_WIDTH) + 1;
  const band = plotWidth / count;
  const y = (value: number) => PAD_TOP + PLOT_HEIGHT * (1 - (value - scale.min) / (scale.max - scale.min));
  const plotBottom = PAD_TOP + PLOT_HEIGHT;
  const labelStep = Math.max(1, Math.ceil(MIN_LABEL_PX / band));
  const labelChars = Math.max(4, Math.floor((band * labelStep) / CHAR_WIDTH));
  const showValues = count <= WIDE_FROM;
  const kindName = bar ? "Столбчатый график" : "Линейный график";
  const summary = `${kindName}${spec.title ? ` «${spec.title}»` : ""}. Категорий: ${count}, ${pluralRu(
    spec.series.length,
    ["ряд", "ряда", "рядов"],
  )}: ${spec.series.length}. Значения от ${format(extent.min)} до ${format(extent.max)}. Те же данные — в таблице.`;
  const x = (index: number) => left + band * (index + 0.5);

  const marks: ReactNode[] = [];
  if (bar) {
    const group = Math.min(band * 0.72, 56 * spec.series.length);
    const barWidth = Math.max(2, group / spec.series.length);
    spec.series.forEach((series, seriesIndex) => {
      const tone = TONES[seriesIndex % TONES.length] as (typeof TONES)[number];
      series.values.forEach((value, index) => {
        if (value === 0) return;
        const barLeft = x(index) - (barWidth * spec.series.length) / 2 + seriesIndex * barWidth;
        const top = Math.min(y(value), y(0));
        const barHeight = Math.max(1, Math.abs(y(value) - y(0)));
        marks.push(
          <rect
            key={slot("bar", seriesIndex, index)}
            x={barLeft + 1}
            y={top}
            width={Math.max(1, barWidth - 2)}
            height={barHeight}
            rx={2}
            className={tone.fill}
          />,
        );
        const text = format(value);
        const labelY = value >= 0 ? y(value) - 5 : y(value) + 15;
        if (showValues && barWidth >= text.length * CHAR_WIDTH + 4 && labelY < plotBottom + 8) {
          marks.push(
            <text
              key={slot("value", seriesIndex, index)}
              x={barLeft + barWidth / 2}
              y={labelY}
              textAnchor="middle"
              className="fill-fg type-caption"
              data-chart-value=""
            >
              {text}
            </text>,
          );
        }
      });
    });
  } else {
    spec.series.forEach((series, seriesIndex) => {
      const tone = TONES[seriesIndex % TONES.length] as (typeof TONES)[number];
      const shape = SHAPES[seriesIndex % SHAPES.length] as Shape;
      if (count > 1) {
        marks.push(
          <path
            key={slot("line", seriesIndex)}
            d={series.values
              .map((value, index) => `${index === 0 ? "M" : "L"}${x(index)} ${y(value)}`)
              .join("")}
            strokeWidth={2}
            strokeLinejoin="round"
            strokeLinecap="round"
            strokeDasharray={DASHES[seriesIndex % DASHES.length]}
            className={cn("fill-none", tone.stroke)}
          />,
        );
      }
      series.values.forEach((value, index) => {
        marks.push(
          <Marker
            key={slot("dot", seriesIndex, index)}
            shape={shape}
            x={x(index)}
            y={y(value)}
            tone={tone}
          />,
        );
        if (showValues && spec.series.length === 1) {
          marks.push(
            <text
              key={slot("value", seriesIndex, index)}
              x={x(index)}
              y={y(value) - 10}
              textAnchor="middle"
              className="fill-fg type-caption"
              data-chart-value=""
            >
              {format(value)}
            </text>,
          );
        }
      });
    });
  }

  return (
    <figure
      data-chart={spec.type}
      className={cn("flex min-w-0 flex-col gap-2 rounded-lg border border-border bg-surface p-3", className)}
    >
      {spec.title && <figcaption className="type-label text-fg">{spec.title}</figcaption>}
      <div
        ref={ref}
        className="overflow-x-auto"
        {...(scrollable
          ? { role: "region", tabIndex: 0, "aria-label": `${kindName}: прокрутите по горизонтали` }
          : {})}
      >
        <svg
          role="img"
          aria-label={summary}
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          className="block max-w-none"
        >
          {scale.ticks.map((tick, index) => (
            <g key={tick}>
              <line
                x1={left}
                x2={left + plotWidth}
                y1={y(tick)}
                y2={y(tick)}
                strokeWidth={1}
                className="stroke-border"
              />
              <text
                x={left - 8}
                y={y(tick)}
                dy="0.32em"
                textAnchor="end"
                className="fill-fg-muted type-caption"
              >
                {tickLabels[index]}
              </text>
            </g>
          ))}
          {scale.min < 0 && scale.max > 0 && (
            <line
              x1={left}
              x2={left + plotWidth}
              y1={y(0)}
              y2={y(0)}
              strokeWidth={1}
              data-chart-zero=""
              className="stroke-control"
            />
          )}
          {bar && scale.min >= 0 && (
            <line
              x1={left}
              x2={left + plotWidth}
              y1={y(scale.min)}
              y2={y(scale.min)}
              strokeWidth={1}
              className="stroke-control"
            />
          )}
          {marks}
          {spec.labels.map((label, index) =>
            index % labelStep === 0 ? (
              <text
                key={slot("label", index)}
                x={x(index)}
                y={plotBottom + 20}
                textAnchor="middle"
                className="fill-fg-muted type-caption"
              >
                {clipLabel(label, labelChars)}
              </text>
            ) : null,
          )}
        </svg>
      </div>
      {spec.series.length > 1 && (
        <ul aria-label="Ряды" className="flex flex-wrap gap-x-4 gap-y-1 type-caption text-fg-muted">
          {spec.series.map((series, index) => (
            <li key={slot("legend", index)} className="flex items-center gap-2">
              <SeriesKey kind={spec.type} index={index} />
              {series.name}
            </li>
          ))}
        </ul>
      )}
      <div>
        <Button
          variant="ghost"
          size="sm"
          aria-expanded={tableShown}
          aria-controls={tableId}
          onClick={() => setTableShown((value) => !value)}
        >
          {tableShown ? "Скрыть таблицу" : "Показать таблицу"}
        </Button>
      </div>
      <div
        id={tableId}
        data-chart-table=""
        className={cn(tableShown ? "overflow-x-auto rounded-md border border-border" : "sr-only")}
        {...(tableShown ? { role: "region", tabIndex: 0, "aria-label": "Таблица данных графика" } : {})}
      >
        <table className="w-full border-collapse type-ui">
          <caption className="sr-only">{spec.title ?? "Данные графика"}</caption>
          <thead className="bg-bg-subtle">
            <tr>
              <th scope="col" className="px-3 py-2 text-left font-semibold text-fg">
                Категория
              </th>
              {spec.series.map((series, index) => (
                <th
                  key={slot("series", index)}
                  scope="col"
                  className="px-3 py-2 text-right font-semibold text-fg"
                >
                  {series.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {spec.labels.map((label, row) => (
              <tr key={slot("row", row)} className="border-t border-border">
                <th scope="row" className="px-3 py-2 text-left font-normal text-fg">
                  {label}
                </th>
                {spec.series.map((series, index) => (
                  <td key={slot("cell", index)} className="px-3 py-2 text-right tabular-nums text-fg">
                    {format(series.values[row] ?? 0)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </figure>
  );
}
