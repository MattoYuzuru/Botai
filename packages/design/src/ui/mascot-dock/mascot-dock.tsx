import { type CSSProperties, type ReactNode, useEffect, useRef, useState } from "react";
import { cn } from "../../lib/cn";
import { stageStep } from "../../mascot/stage-plan";
import type { MascotMotion, MascotSituationId, MascotSkinId, MascotTheme } from "../../mascot/types";
import { Mascot, type MascotHandle } from "../mascot/mascot";

/** Прыжок со сцены на насест — не длиннее `duration-slow` (280 мс), styleguide §5. */
export const HOP_MS = 280;
/** Блик в `blink`/`look` на насесте: моргает часто, голову поворачивает раз в 8–14 с (mascot.md §4.1). */
const IDLE_RANGE = [2400, 5000] as const;
const LOOK_RANGE = [8000, 14000] as const;
/** Шаг прогулки по полу: доля свободной ширины. */
const WALK_STEP = 0.35;

export interface MascotDockProps {
  /**
   * `floor` — сцена пустого чата: «пол» над полем ввода, Варя гуляет и клюёт (§4.2). `perch` — насест на
   * верхней кромке поля справа, лапы на линии рамки (§4.1). Смена `floor → perch` — один прыжок ≤ 280 мс,
   * при `motion: 'static'` — мгновенная смена без перелёта.
   */
  mode: "floor" | "perch";
  situation: MascotSituationId;
  theme?: MascotTheme;
  skin?: MascotSkinId | null;
  motion?: MascotMotion;
  /** Зерно детерминированной сцены: порядок и длительности действий. */
  seed?: number;
  /** Пузырь-реплика над Варей (mascot.md §4.3). Живёт в слое Вари и едет вместе с ней. */
  bubble?: ReactNode;
  /** Нажатие на саму Варю: следующая реплика, пасхалка. Не единственный способ: действие дублируют кнопки экрана. */
  onTap?: () => void;
  className?: string;
}

/**
 * Слой Вари над полем ввода чата. Кладётся в `relative`-обёртку над `ChatComposer`: нижняя кромка слоя — верхняя
 * кромка поля. Сам слой не перехватывает касания: `pointer-events` есть только у спрайта. Одна Варя
 * на всю сцену: пол и насест — два места одного и того же экземпляра, поэтому прыжок не делает вторую.
 * Положение считается в CSS (`cqw`), ширину не измеряет ни JS, ни ResizeObserver.
 */
export function MascotDock({
  mode,
  situation,
  theme,
  skin,
  motion = "animated",
  seed = 1,
  bubble,
  onTap,
  className,
}: MascotDockProps) {
  const sprite = useRef<MascotHandle>(null);
  const body = useRef<HTMLDivElement>(null);
  const previousMode = useRef(mode);
  const [position, setPosition] = useState(0.5);
  const [facing, setFacing] = useState<"left" | "right">("left");
  const [walkMs, setWalkMs] = useState<number | null>(null);
  const animated = motion === "animated";

  // Прыжок со сцены на насест: дуга на внутреннем слое, перемещение — CSS-переход на внешнем.
  useEffect(() => {
    const was = previousMode.current;
    previousMode.current = mode;
    if (was === "floor" && mode === "perch" && animated) {
      sprite.current?.act("hop");
      const element = body.current;
      if (typeof element?.animate === "function") {
        element.animate(
          [
            { transform: "translateY(0)" },
            { transform: "translateY(-1.5rem)", offset: 0.5 },
            { transform: "translateY(0)" },
          ],
          { duration: HOP_MS, easing: "cubic-bezier(0.22, 1, 0.36, 1)" },
        );
      }
    }
  }, [mode, animated]);

  // Сцена пустого чата: пауза 6–10 с → действие 1.5–2.5 с. Порядок — из зерна, без Math.random().
  useEffect(() => {
    if (mode !== "floor" || !animated) return;
    let cancelled = false;
    let index = 0;
    let at = 0.5;
    const timers: number[] = [];
    const later = (ms: number, run: () => void) => {
      timers.push(
        window.setTimeout(() => {
          if (!cancelled) run();
        }, ms),
      );
    };
    const schedule = () => {
      const step = stageStep(seed, index++);
      later(step.pauseMs, () => {
        if (step.action === "walk") {
          const direction = at - WALK_STEP < 0 ? 1 : at + WALK_STEP > 1 ? -1 : step.direction;
          at = Math.min(1, Math.max(0, at + direction * WALK_STEP));
          setFacing(direction < 0 ? "left" : "right");
          setWalkMs(step.durationMs);
          setPosition(at);
          sprite.current?.setLoop("run");
        } else if (step.action === "peck") sprite.current?.act("peck");
        else if (step.action === "shake") sprite.current?.act("shake");
        else if (step.action === "look") sprite.current?.act("look");
        else sprite.current?.act("wave");
        later(step.durationMs, () => {
          if (step.action === "walk") sprite.current?.settle();
          setWalkMs(null);
          schedule();
        });
      });
    };
    schedule();
    return () => {
      cancelled = true;
      for (const timer of timers) window.clearTimeout(timer);
      sprite.current?.settle();
      setWalkMs(null);
    };
  }, [mode, animated, seed]);

  const perch = mode === "perch";
  const style = {
    "--dock-p": String(position),
    transitionDuration: !animated ? "0s" : perch ? `${HOP_MS}ms` : walkMs !== null ? `${walkMs}ms` : "0s",
    transitionTimingFunction: !perch && walkMs !== null ? "linear" : "cubic-bezier(0.22, 1, 0.36, 1)",
  } as CSSProperties;

  return (
    <div
      data-dock={mode}
      className={cn(
        "@container pointer-events-none absolute inset-x-0 bottom-full h-0",
        perch ? "z-dock" : "z-stage",
        className,
      )}
    >
      <div
        className={cn(
          "absolute inset-x-0 bottom-0",
          // Размеры по ширине слоя (он равен ширине поля ввода): пол 96 px, от экрана 390 px — 120 px;
          // насест 36 px, от экрана 430 px — 40 px (mascot.md §4.1–4.2).
          "[--dock-floor:6rem] [--dock-perch:2.25rem] [--dock-scale:0.375]",
          "@min-[22.375rem]:[--dock-floor:7.5rem] @min-[22.375rem]:[--dock-scale:0.3]",
          "@min-[24.375rem]:[--dock-perch:2.5rem] @min-[24.375rem]:[--dock-scale:0.3333]",
        )}
      >
        <div
          style={style}
          className={cn(
            "absolute bottom-0 left-0 size-(--dock-floor) origin-bottom-left transition-transform will-change-transform",
            perch
              ? "translate-x-[calc(100cqw_-_1rem_-_var(--dock-perch))] translate-y-[calc(var(--dock-perch)_*_0.05)] scale-(--dock-scale)"
              : "translate-x-[calc((100cqw_-_var(--dock-floor))_*_var(--dock-p))] translate-y-[calc(var(--dock-floor)_*_0.05)]",
          )}
        >
          <div ref={body} className="relative size-full">
            {bubble && (
              <div className="pointer-events-auto absolute right-0 bottom-full mb-2 w-max max-w-[min(18rem,80cqw)]">
                {bubble}
              </div>
            )}
            {/* Нажатие по спрайту — дополнительный способ (реплика, пасхалка); смысл дублирует текст экрана. */}
            <div
              aria-hidden="true"
              onClick={onTap}
              className={cn(
                "size-full",
                onTap ? "pointer-events-auto cursor-pointer" : "pointer-events-none",
              )}
            >
              <Mascot
                ref={sprite}
                situation={situation}
                theme={theme}
                skin={skin}
                motion={motion}
                size="fill"
                seed={seed}
                idleRange={IDLE_RANGE}
                lookRange={LOOK_RANGE}
                flip={facing === "right"}
                className="size-full"
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
