import { useEffect, useRef } from "react";
import { cn } from "../../lib/cn";
import type { MascotIntent, MascotPresentation } from "../../mascot/types";
import { Badge } from "../badge/badge";
import { Button } from "../button/button";
import { Mascot, type MascotHandle } from "../mascot/mascot";

/** Разовая реакция короче 1.5 с (mascot.md §2): столько интерфейс держит событие, прежде чем подтвердить его. */
export const REACTION_MS = 1500;

export interface MascotStageProps {
  presentation: MascotPresentation;
  /** Сторона картинки, px (кратна 40 для чётких пикселей). */
  size?: number;
  /** Нажатие кнопки действия. Разрешает намерение frontend; renderer ничего не исполняет. */
  onIntent?: (intent: MascotIntent) => void;
  /** Разовая реакция проиграна: событие пора подтвердить, чтобы она не повторилась. */
  onReactionEnd?: (eventId: string) => void;
  /** Зерно idle: одно и то же зерно — одна и та же Варя. */
  seed?: number;
  /** Текст рядом с картинкой читается как подпись к экрану, а не как реплика агента. */
  label?: string;
  className?: string;
}

/**
 * Оболочка Вари: картинка (`aria-hidden`), реплика, бейдж и одно действие. Смысл всегда существует текстом:
 * при `visible: false` картинки нет, а реплика, бейдж и кнопка остаются (mascot.md §5). Действие дублируется
 * обычной кнопкой — нажатие по самому спрайту не единственный способ.
 */
export function MascotStage({
  presentation,
  size = 80,
  onIntent,
  onReactionEnd,
  seed,
  label = "Варя",
  className,
}: MascotStageProps) {
  const { situation, theme, motion, line, badge, intent, skin, visible, reactionEventId } = presentation;
  const handle = useRef<MascotHandle>(null);

  // Одна реакция: играем, держим ≤ 1.5 с и подтверждаем событие. Пока картинка скрыта, ничего не тратим.
  useEffect(() => {
    if (!reactionEventId || !visible) return;
    if (motion === "animated") handle.current?.react();
    const timer = window.setTimeout(() => onReactionEnd?.(reactionEventId), REACTION_MS);
    return () => window.clearTimeout(timer);
  }, [reactionEventId, visible, motion, onReactionEnd]);

  const hasText = Boolean(line || badge || intent);
  if (!visible && !hasText) return null;

  return (
    <section
      aria-label={label}
      data-mascot-stage=""
      data-situation={situation}
      data-motion={motion}
      className={cn("flex items-center gap-3", className)}
    >
      {visible && (
        <Mascot
          ref={handle}
          situation={situation}
          theme={theme}
          skin={skin}
          motion={motion}
          size={size}
          seed={seed}
        />
      )}
      {hasText && (
        <div className="flex min-w-0 flex-col items-start gap-2">
          {badge && (
            <Badge tone={badge.tone} data-mascot-badge="">
              {badge.text}
            </Badge>
          )}
          {line && (
            <p data-mascot-line="" className="type-ui text-fg">
              {line}
            </p>
          )}
          {intent && (
            <Button variant="secondary" onClick={() => onIntent?.(intent)}>
              {intent.label}
            </Button>
          )}
        </div>
      )}
    </section>
  );
}
