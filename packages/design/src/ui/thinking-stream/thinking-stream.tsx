import { useEffect, useRef } from "react";
import { cn } from "../../lib/cn";
import { formatDuration } from "../../lib/format";
import { Icon } from "../icon";
import { ScrollArea } from "../scroll-area/scroll-area";

export type StepState = "running" | "done" | "failed" | "waiting" | "skipped";

export interface ThinkingStep {
  id: string;
  label: string;
  state: StepState;
  detail?: string;
  /** Момент появления шага, мс. */
  at: number;
}

export interface ThinkingStreamProps {
  steps: readonly ThinkingStep[];
  /** Свёрнут до «Думает · 12 с». На `ProgressWall` развёрнут по умолчанию. */
  collapsed?: boolean;
  onToggle?: (collapsed: boolean) => void;
  /** «Сейчас» — для отсчёта прошедшего времени; обновляется снаружи раз в секунду. */
  nowMs: number;
  /** Работа закончилась: отсчёт останавливается на этом моменте, заголовок — «Думал». */
  finishedAt?: number;
  /** Высота развёрнутого списка ограничена и прокручивается; автопрокрутка к последнему шагу останавливается, если человек прокрутил вверх. */
  maxHeightClass?: string;
  className?: string;
}

const STATE_LABEL: Record<StepState, string> = {
  running: "выполняется",
  done: "готово",
  failed: "не получилось",
  waiting: "жду ответа",
  skipped: "пропущено",
};

function StepIcon({ state }: { state: StepState }) {
  switch (state) {
    case "running":
      return (
        <span
          aria-hidden="true"
          className="mt-1.5 ml-1.5 size-2 animate-pulse-soft rounded-full bg-accent-fg"
        />
      );
    case "done":
      return <Icon name="check" size={20} className="text-success" />;
    case "failed":
      return <Icon name="error" size={20} className="text-danger-fg" />;
    case "waiting":
      return <Icon name="waiting" size={20} className="text-warning" />;
    case "skipped":
      return <Icon name="paused" size={20} className="text-control" />;
  }
}

/**
 * Лог шагов агента (блоки `thinking`). Новый шаг добавляется снизу, завершённый — галочка, упавший —
 * иконка и текст `danger-fg` с причиной, ждущий — «Жду ответа» словами. Свёрнут до «Думает · 12 с».
 */
export function ThinkingStream({
  steps,
  collapsed = false,
  onToggle,
  nowMs,
  finishedAt,
  maxHeightClass = "max-h-72",
  className,
}: ThinkingStreamProps) {
  const first = steps[0]?.at ?? nowMs;
  const running = finishedAt === undefined && steps.some((step) => step.state === "running");
  const waiting = !running && finishedAt === undefined && steps.some((step) => step.state === "waiting");
  // Пока ждём человека, агент не думает: отсчёт стоит на последнем шаге, а не тикает минутами.
  const last = steps.reduce((latest, step) => Math.max(latest, step.at), first);
  const elapsed = (finishedAt ?? (running ? nowMs : last)) - first;
  const title = `${running ? "Думает" : waiting ? "Ждёт ответа" : "Думал"} · ${formatDuration(elapsed)}`;

  const viewport = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  // Автопрокрутка к последнему шагу, пока человек не прокрутил вверх.
  // biome-ignore lint/correctness/useExhaustiveDependencies: пересчёт при смене числа шагов
  useEffect(() => {
    const element = viewport.current;
    if (element && pinned.current) element.scrollTop = element.scrollHeight;
  }, [steps.length, collapsed]);

  return (
    <section data-collapsed={collapsed || undefined} className={cn("rounded-lg bg-bg-subtle", className)}>
      <h3 className="type-label text-fg-muted">
        <button
          type="button"
          aria-expanded={!collapsed}
          onClick={() => onToggle?.(!collapsed)}
          disabled={!onToggle}
          className="flex min-h-11 w-full cursor-pointer items-center gap-2 rounded-lg px-3 text-left disabled:cursor-default"
        >
          {running && (
            <span aria-hidden="true" className="size-2 animate-pulse-soft rounded-full bg-accent-fg" />
          )}
          <span className="flex-1" data-numeric="">
            {title}
          </span>
          {onToggle && <Icon name={collapsed ? "chevron-down" : "chevron-up"} size={20} />}
        </button>
      </h3>
      {!collapsed && (
        <ScrollArea
          className={cn("px-3 pb-3", maxHeightClass)}
          viewportRef={viewport}
          onViewportScroll={(event) => {
            const element = event.currentTarget;
            pinned.current = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
          }}
        >
          <ol className="flex flex-col gap-2">
            {steps.map((step) => (
              <li key={step.id} data-state={step.state} className="flex items-start gap-2">
                <StepIcon state={step.state} />
                <div className="min-w-0 flex-1">
                  <p
                    className={cn(
                      "type-ui",
                      step.state === "failed"
                        ? "text-danger-fg"
                        : step.state === "running"
                          ? "text-fg"
                          : "text-fg-muted",
                    )}
                  >
                    {step.label}
                    <span className="sr-only">: {STATE_LABEL[step.state]}</span>
                  </p>
                  {step.state === "waiting" && <p className="type-caption text-warning-fg">Жду ответа</p>}
                  {step.detail && (
                    <p
                      className={cn(
                        "type-caption",
                        step.state === "failed" ? "text-danger-fg" : "text-fg-subtle",
                      )}
                    >
                      {step.detail}
                    </p>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </ScrollArea>
      )}
    </section>
  );
}
