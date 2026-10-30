import { type RefObject, useEffect, useLayoutEffect, useRef, useState } from "react";
import { type LevelMeter, peakLevel, smoothLevel } from "../../lib/audio-levels";
import { cn } from "../../lib/cn";
import { useCalmMotion } from "../../lib/hooks";

/** Столбик 4 px и промежуток 4 px: шаг, по которому число столбиков подбирается под ширину. */
const BAR_STEP = 8;
const GAP = 4;
const MIN_BARS = 12;
const MAX_BARS = 32;
const DEFAULT_BARS = 28;
/** Высота молчащего столбика — точка: линия по центру видна, даже когда никто не говорит. */
const REST = 0.12;
/** Статичная полоса уровня обновляется редко: это показание прибора, а не анимация. */
const STRIP_TICK_MS = 250;

export interface VoiceEqualizerProps {
  /** Источник громкости; `null` — звука нет (микрофон отпущен или анализатор недоступен): столбики лежат точками. */
  meter: LevelMeter | null;
  className?: string;
}

function useBarCount(ref: RefObject<HTMLElement | null>, enabled: boolean): number {
  const [bars, setBars] = useState(DEFAULT_BARS);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!enabled || !element) return;
    const measure = () => {
      const width = element.getBoundingClientRect().width;
      if (width <= 0) return;
      setBars(Math.max(MIN_BARS, Math.min(MAX_BARS, Math.floor((width + GAP) / BAR_STEP))));
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref, enabled]);
  return bars;
}

/**
 * Эквалайзер записи. Столбики стоят в ряд по горизонтальному центру и растут вверх и вниз от средней линии; высота
 * каждого — громкость своей части спектра голоса, слева низкие частоты, справа высокие. Кадры рисует `requestAnimationFrame`
 * прямо в DOM (React на каждый кадр не перерисовывается); взлёт быстрый, спад медленнее. Число столбиков (12…32)
 * подбирается под ширину. При сокращённом движении вместо столбиков — тонкая статичная полоса общего уровня, которая
 * обновляется четыре раза в секунду. Для скринридера блок скрыт: «Идёт запись» и время сообщает поле ввода.
 */
export function VoiceEqualizer({ meter, className }: VoiceEqualizerProps) {
  const calm = useCalmMotion();
  const box = useRef<HTMLDivElement>(null);
  const fill = useRef<HTMLSpanElement>(null);
  const bars = useBarCount(box, !calm);

  // biome-ignore lint/correctness/useExhaustiveDependencies: число столбиков меняет дерево под `box`, эффекту нужны новые узлы
  useEffect(() => {
    const element = box.current;
    if (calm || !element) return;
    const nodes = Array.from(element.children) as HTMLElement[];
    const raw = new Float32Array(nodes.length);
    const shown = new Float32Array(nodes.length);
    let frame = 0;
    let last = 0;
    const paint = (now: number) => {
      const dt = last ? now - last : 16;
      last = now;
      if (meter) meter.read(raw);
      else raw.fill(0);
      for (let index = 0; index < nodes.length; index += 1) {
        const level = smoothLevel(shown[index] ?? 0, raw[index] ?? 0, dt);
        shown[index] = level;
        const node = nodes[index];
        if (node) node.style.transform = `scaleY(${(REST + (1 - REST) * level).toFixed(3)})`;
      }
    };
    paint(0); // до первого кадра столбики уже точки, а не полная высота
    const tick = (now: number) => {
      paint(now);
      frame = window.requestAnimationFrame(tick);
    };
    frame = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(frame);
  }, [calm, meter, bars]);

  useEffect(() => {
    const element = fill.current;
    if (!calm || !element) return;
    const raw = new Float32Array(8);
    const read = () => {
      if (meter) meter.read(raw);
      else raw.fill(0);
      element.style.transform = `scaleX(${peakLevel(raw).toFixed(2)})`;
    };
    read();
    const timer = window.setInterval(read, STRIP_TICK_MS);
    return () => window.clearInterval(timer);
  }, [calm, meter]);

  if (calm) {
    return (
      <div
        data-equalizer="calm"
        aria-hidden="true"
        className={cn("flex h-8 min-w-0 flex-1 items-center", className)}
      >
        <div className="h-1 w-full overflow-hidden rounded-full bg-border">
          <span ref={fill} className="block h-full origin-left rounded-full bg-accent" />
        </div>
      </div>
    );
  }
  return (
    <div
      ref={box}
      data-equalizer="bars"
      aria-hidden="true"
      className={cn("flex h-8 min-w-0 flex-1 items-center justify-center gap-1 overflow-hidden", className)}
    >
      {Array.from({ length: bars }, (_, index) => (
        <span
          // biome-ignore lint/suspicious/noArrayIndexKey: столбики неразличимы и не меняют порядок
          key={index}
          data-bar=""
          className="h-full w-1 shrink-0 rounded-full bg-accent will-change-transform"
        />
      ))}
    </div>
  );
}
