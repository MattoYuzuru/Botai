import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Badge } from "../badge/badge";

/** «12 из 140». Общее число не показывается без позиции и наоборот. */
export function rankSummary(position: number | string, total: number | null | undefined): string {
  return total === null || total === undefined ? `${position}` : `${position} из ${total}`;
}

export interface RankRowProps {
  /** Место в рейтинге, как дал источник: 3, «12», «1–3». */
  position: number | string;
  /** Кто или что: «Вы», «Группа БИ-21», «Направление». */
  label: string;
  /** Балл или показатель, как у источника: «8,42», «412». */
  score?: string | number | null;
  /** Подпись под названием: «поток», «период». */
  meta?: ReactNode;
  /** Строка студента. Выделение — не только цветом: метка «Это вы», вес шрифта и `aria-current`. */
  isMe?: boolean;
  className?: string;
}

/**
 * Строка рейтинга: место, название, балл. Для «это вы» цвет (подложка `accent-subtle`) дублируют текстовая метка
 * «Это вы», полужирное начертание и `aria-current="true"`. Место и балл — табличные цифры; название переносится.
 */
export function RankRow({ position, label, score, meta, isMe = false, className }: RankRowProps) {
  const hasScore = score !== undefined && score !== null && `${score}` !== "";
  return (
    <li
      aria-current={isMe ? "true" : undefined}
      data-rank-row=""
      data-me={isMe ? "" : undefined}
      className={cn(
        "flex min-h-14 items-center gap-3 rounded-md px-3 py-2",
        isMe ? "bg-accent-subtle" : "bg-transparent",
        className,
      )}
    >
      <span
        data-numeric=""
        className={cn(
          "grid h-8 min-w-8 shrink-0 place-items-center rounded-full px-1.5 type-label",
          isMe ? "bg-accent text-fg-on-accent" : "bg-bg-subtle text-fg-muted",
        )}
      >
        <span className="sr-only">Место </span>
        {position}
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className={cn("min-w-0 break-words text-fg", isMe ? "type-subheading font-bold" : "type-ui")}>
            {label}
          </span>
          {isMe && (
            <Badge tone="accent" variant="outline">
              Это вы
            </Badge>
          )}
        </span>
        {meta && <span className="type-caption text-fg-muted">{meta}</span>}
      </span>
      {hasScore && (
        <span
          data-numeric=""
          className={cn("shrink-0 text-right", isMe ? "type-subheading text-fg" : "type-ui text-fg-muted")}
        >
          <span className="sr-only">Балл </span>
          {score}
        </span>
      )}
    </li>
  );
}

export interface RankItem extends Omit<RankRowProps, "className"> {
  /** Ключ строки. Без него — позиция и название. */
  id?: string;
}

export interface RankListProps {
  /** Название рейтинга для скринридера: «Рейтинг потока». */
  label: string;
  items: readonly RankItem[];
  /** Сколько участников всего по данным источника. Вместе со строкой «это вы» даёт «12 из 140». */
  total?: number | null;
  /** Период или доска под списком: «осень 2026». */
  caption?: ReactNode;
  className?: string;
}

/**
 * Рейтинг списком: `ol` со строками `RankRow`, итог «Вы: 12 из 140» под списком (текстом, чтобы не зависеть от цвета).
 * Порядок строк — как дал источник; список не сортируется и не пересчитывается.
 */
export function RankList({ label, items, total, caption, className }: RankListProps) {
  const me = items.find((item) => item.isMe);
  const hasTotal = total !== undefined && total !== null;
  const summary = hasTotal ? (
    me ? (
      <span className="font-bold text-fg">Вы: {rankSummary(me.position, total)}</span>
    ) : (
      `Всего: ${total}`
    )
  ) : null;
  return (
    <div data-rank-list="" className={cn("flex min-w-0 flex-col gap-2", className)}>
      <ol aria-label={label} className="flex flex-col gap-1">
        {items.map((item) => (
          <RankRow key={item.id ?? `${item.position}-${item.label}`} {...item} />
        ))}
      </ol>
      {(summary || caption) && (
        <p className="px-3 type-caption text-fg-muted" data-rank-summary="">
          {summary}
          {summary && caption ? " · " : null}
          {caption}
        </p>
      )}
    </div>
  );
}
