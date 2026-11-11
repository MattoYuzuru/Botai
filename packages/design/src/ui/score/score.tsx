import { cn } from "../../lib/cn";

/** Доля 0…1 для ширины/высоты полосы; за пределами — к границе. */
const clamp = (fill: number) => Math.min(1, Math.max(0, Number.isFinite(fill) ? fill : 0));
const percent = (fill: number) => `${Math.round(clamp(fill) * 1000) / 10}%`;

export interface ScoreBarProps {
  /** Доля заполнения одной записи: `valueNum / scaleMax` источника. Ничего не суммируется и не пересчитывается. */
  fill: number;
  className?: string;
}

/**
 * Горизонтальная полоса заполнения одной оценки: «8 из 10» — 80 %. Цвет — акцент, без «хорошо/плохо»: смысл шкалы
 * знает только источник. Декоративна: значение всегда стоит рядом текстом («8 из 10»), скринридер читает его.
 */
export function ScoreBar({ fill, className }: ScoreBarProps) {
  return (
    <span
      aria-hidden="true"
      data-score-bar=""
      className={cn("relative block h-1.5 w-full min-w-0 overflow-hidden rounded-full bg-pressed", className)}
    >
      <span className="absolute inset-y-0 left-0 rounded-full bg-accent" style={{ width: percent(fill) }} />
    </span>
  );
}

export interface ScoreStripItem {
  id: string;
  /** Доля 0…1 или `null` — у оценки нет числа и максимума: показывается текстом («зачтено»). */
  fill: number | null;
  /** Значение словами для скринридера и для оценки без числа: «8 из 10», «зачтено». */
  text: string;
  /** За что оценка: «Контрольная №2». */
  title?: string | null;
}

export interface ScoreStripProps {
  /** Последние оценки за работы, слева старые, справа свежие. */
  items: readonly ScoreStripItem[];
  /** Подпись списка для скринридера. */
  label: string;
  /**
   * Только для глаз: полоса внутри кнопки (карточки предмета), чьё имя уже называет результат. Рисуется
   * строчными элементами без списка и скрыта от скринридера.
   */
  decorative?: boolean;
  /** `sm` — столбики 24 px (строка), `md` — 32 px (карточка предмета). */
  size?: "sm" | "md";
  className?: string;
}

/**
 * Полоса последних оценок предмета: маленький столбик на каждую работу, высота — доля этой записи. Оценка без
 * числа (зачёт, буква) — нейтральный чип со словом источника. Столбики не сравнивают разные шкалы: каждый — своя
 * пара чисел. Для скринридера это список «Контрольная №2: 8 из 10».
 */
export function ScoreStrip({ items, label, decorative = false, size = "sm", className }: ScoreStripProps) {
  if (items.length === 0) return null;
  const Root = decorative ? "span" : "ul";
  const Item = decorative ? "span" : "li";
  return (
    <Root
      {...(decorative ? { "aria-hidden": true } : { "aria-label": label })}
      data-score-strip=""
      className={cn("flex min-w-0 items-end gap-1", className)}
    >
      {items.map((item) => {
        const spoken = item.title ? `${item.title}: ${item.text}` : item.text;
        return (
          <Item key={item.id} className="flex shrink-0 items-end" data-fill={item.fill ?? undefined}>
            {!decorative && <span className="sr-only">{spoken}</span>}
            {item.fill === null ? (
              <span
                aria-hidden="true"
                className="inline-flex h-6 max-w-24 items-center truncate rounded-sm bg-pressed px-1.5 type-caption text-fg-muted"
              >
                {item.text}
              </span>
            ) : (
              <span
                aria-hidden="true"
                className={cn(
                  "relative block overflow-hidden rounded-full bg-pressed",
                  size === "md" ? "h-8 w-2.5" : "h-6 w-2",
                )}
                data-score-column=""
              >
                <span
                  className="absolute inset-x-0 bottom-0 rounded-full bg-accent"
                  style={{ height: percent(item.fill > 0 ? Math.max(item.fill, 0.12) : 0) }}
                />
              </span>
            )}
          </Item>
        );
      })}
    </Root>
  );
}
