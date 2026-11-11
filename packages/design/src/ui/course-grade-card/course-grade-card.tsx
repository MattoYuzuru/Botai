import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Icon } from "../icon";
import { ScoreBar, ScoreStrip, type ScoreStripItem } from "../score/score";

export interface CourseGradeHeadline {
  /** Что это за результат — словами: «Итог», «Накоплено», «Последняя оценка». */
  label: string;
  /** Значение, как его дал источник: «7,4», «зачтено». */
  value: string;
  /** Шкала рядом со значением — только «из 10», когда шкала известна числом. */
  suffix?: string | null;
  /**
   * Подпись источника, которая не шкала («оценка Smart LMS», «баллы за курс», «зачёт»): приглушённо в строке подписи,
   * одной строкой с многоточием — ширину колонки она не задаёт.
   */
  caption?: string | null;
  /** Сколько ещё записей того же вида у предмета: «ещё 1» (все — в листе предмета). */
  more?: number;
  /** Период источника: «Осень 2026», «2 модуль». */
  period?: string | null;
  /** Доля этой записи `valueNum / scaleMax` — полоса под значением; `null` — полосы нет. */
  fill?: number | null;
}

export interface CourseGradeCardProps {
  /** Название предмета. Длинное переносится на вторую строку и обрезается многоточием только после неё. */
  title: string;
  /** Главный результат предмета, как его дал источник. Без него карточка показывает только работы. */
  headline?: CourseGradeHeadline | null;
  /** Подпись под заголовком: «Проверено работ: 5». */
  caption?: ReactNode;
  /** Последние оценки за работы: полоса столбиков, слева старые, справа свежие. */
  strip?: readonly ScoreStripItem[];
  /** Метка вуза на сводном экране «Все вузы». */
  badge?: ReactNode;
  onOpen?: () => void;
  className?: string;
}

/**
 * Карточка предмета «Успеваемости» (D-046): название и число проверенных работ сверху; ниже главный результат
 * источника (итог или накопленное) крупно слева и полоса последних оценок справа; под ними — полоса заполнения
 * главного результата, если у него есть число и максимум. Цвет не оценивает результат:
 * полосы — акцент, чипы без числа — нейтральные. Вся карточка — одна кнопка (≥ 44 px); её имя для скринридера
 * собрано из названия, результата и подписи, полоса — для глаз (подробности — в карточке предмета).
 */
export function CourseGradeCard({
  title,
  headline,
  caption,
  strip = [],
  badge,
  onOpen,
  className,
}: CourseGradeCardProps) {
  const spokenHeadline = headline
    ? `${headline.label}${headline.period ? ` (${headline.period})` : ""}: ${headline.value}${headline.suffix ? ` ${headline.suffix}` : ""}${headline.caption ? `, ${headline.caption}` : ""}${headline.more ? `; ещё ${headline.more}` : ""}`
    : null;
  const headlineMeta = headline
    ? [headline.label, headline.period, headline.caption, headline.more ? `ещё ${headline.more}` : null]
        .filter(Boolean)
        .join(" · ")
    : "";
  const body = (
    <>
      <span className="flex min-w-0 items-start gap-2">
        <span className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="line-clamp-2 break-words type-subheading text-fg">{title}</span>
          {(caption || badge) && (
            <span className="flex flex-wrap items-center gap-x-2 gap-y-1 type-caption text-fg-muted">
              {badge}
              {caption && <span>{caption}</span>}
            </span>
          )}
        </span>
        {onOpen && <Icon name="chevron-right" size={20} className="shrink-0 text-fg-subtle" />}
      </span>
      {(headline || strip.length > 0) && (
        <span className="mt-auto flex min-w-0 items-end justify-between gap-4">
          {headline ? (
            <span className="flex min-w-0 flex-1 flex-col gap-0.5">
              <span className="flex min-w-0 flex-wrap items-baseline gap-x-1.5">
                <span className="min-w-0 break-words type-title text-fg" data-numeric="">
                  {headline.value}
                </span>
                {headline.suffix && (
                  <span className="type-ui text-fg-muted" data-numeric="">
                    {headline.suffix}
                  </span>
                )}
              </span>
              <span className="min-w-0 truncate type-caption text-fg-subtle" data-headline-meta="">
                {headlineMeta}
              </span>
            </span>
          ) : (
            <span />
          )}
          {strip.length > 0 && (
            <ScoreStrip
              items={strip}
              label="Последние работы"
              decorative
              size="md"
              maxChips={1}
              // Полоса не шире половины строки и обрезается внутри: значение и подпись слева всегда видны.
              className="max-w-[55%] shrink justify-end"
            />
          )}
        </span>
      )}
      {headline?.fill != null && <ScoreBar fill={headline.fill} />}
    </>
  );
  const shared = cn(
    "flex min-h-20 w-full min-w-0 flex-col gap-3 rounded-lg border border-border bg-surface p-4 text-left",
    className,
  );
  const label = [title, spokenHeadline, typeof caption === "string" ? caption : null]
    .filter(Boolean)
    .join(". ");
  if (!onOpen) {
    return (
      <div className={shared} data-course-card="">
        {body}
      </div>
    );
  }
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={label}
      data-course-card=""
      className={cn(
        shared,
        "cursor-pointer transition-colors duration-fast ease-soft hover:bg-hover active:bg-pressed",
      )}
    >
      {body}
    </button>
  );
}
