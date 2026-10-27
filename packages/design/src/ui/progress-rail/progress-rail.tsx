import { type ReactNode, useMemo } from "react";
import { cn } from "../../lib/cn";
import { formatDuration } from "../../lib/format";

export interface ProgressRailProps {
  /** Сколько стадий уже завершено. Это номер стадии, а не процент. */
  stage: number;
  /** Сколько стадий известно в целом. Без числа рельс не делится на засечки и не обещает конца. */
  stageCount?: number | null;
  /** Прошло времени, мс — настоящее, из сервера или часов клиента. */
  elapsedMs: number;
  /** Сервер дал оценку остатка. Если нет — «Сколько осталось — пока неизвестно» и никаких чисел. */
  etaKnown: boolean;
  /** Оценка остатка, мс; читается только при `etaKnown`. */
  etaMs?: number | null;
  tone?: "accent" | "warning" | "danger" | "success";
  /** Варя бежит по рельсу: координата — номер завершённой стадии. Слот выставляется на позицию. */
  runner?: ReactNode;
  /** Движение остановлено (reduced motion): рельс — ровная заливка, импульса нет. */
  calm?: boolean;
  className?: string;
}

const FILL = {
  accent: "bg-accent",
  warning: "bg-warning",
  danger: "bg-danger",
  success: "bg-success",
} as const;

/**
 * Рельс стадий без процентов и без придуманного времени. Пройденные стадии закрашены `accent`,
 * текущая пульсирует (при сокращённом движении — статичная заливка). Подпись времени всегда настоящая.
 */
export function ProgressRail({
  stage,
  stageCount,
  elapsedMs,
  etaKnown,
  etaMs,
  tone = "accent",
  runner,
  calm = false,
  className,
}: ProgressRailProps) {
  const count = stageCount && stageCount > 0 ? stageCount : null;
  const done = count ? Math.min(Math.max(stage, 0), count) : Math.max(stage, 0);
  const ticks = useMemo(() => (count ? Array.from({ length: count }, (_, index) => index) : []), [count]);

  const label = count
    ? `Стадия ${Math.min(done + 1, count)} из ${count}`
    : "Работа идёт, число стадий пока неизвестно";

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <div
        role="img"
        aria-label={label}
        data-stage={done}
        data-etaknown={etaKnown}
        className="relative h-9 pt-6"
      >
        {runner && (
          <div
            data-rail-runner=""
            className="absolute bottom-1 -translate-x-1/2 transition-[left] duration-slow ease-soft"
            style={{ left: count ? `${(done / count) * 100}%` : "0%" }}
          >
            {runner}
          </div>
        )}
        <div className="relative h-1.5 w-full overflow-hidden rounded-full bg-bg-subtle">
          {count ? (
            <>
              <div
                className={cn(
                  "absolute inset-y-0 left-0 rounded-full transition-[width] duration-slow ease-soft",
                  FILL[tone],
                )}
                style={{ width: `${(done / count) * 100}%` }}
              />
              {done < count && (
                <div
                  data-current=""
                  className={cn(
                    "absolute inset-y-0 rounded-full opacity-60",
                    !calm && "animate-rail",
                    FILL[tone],
                  )}
                  style={{ left: `${(done / count) * 100}%`, width: `${100 / count}%` }}
                />
              )}
              {ticks.slice(1).map((tick) => (
                <span
                  key={tick}
                  aria-hidden="true"
                  className="absolute inset-y-0 w-0.5 bg-bg"
                  style={{ left: `${(tick / count) * 100}%` }}
                />
              ))}
            </>
          ) : (
            <div
              className={cn(
                "absolute inset-y-0 left-0 w-1/4 rounded-full opacity-60",
                !calm && "animate-rail",
                FILL[tone],
              )}
            />
          )}
        </div>
      </div>
      <p className="flex flex-wrap items-baseline justify-between gap-x-4 type-caption text-fg-muted">
        <span data-numeric="">Идёт {formatDuration(elapsedMs)}</span>
        <span>
          {etaKnown && etaMs != null
            ? `Осталось примерно ${formatDuration(etaMs)}`
            : "Сколько осталось — пока неизвестно"}
        </span>
      </p>
    </div>
  );
}
