import { type ReactNode, useEffect, useRef, useState } from "react";
import { cn } from "../../lib/cn";
import { Icon } from "../icon";
import { describeSource, type SourceState } from "./source-status";

export interface SourceStatusBarItem {
  /** Идентификатор пространства вуза: атрибут `data-space-id` и ключ. */
  id: string;
  /** Короткое название вуза («ВШЭ»), а не адрес сайта. */
  title: string;
  state: SourceState;
  /** Время последнего снимка, мс. */
  updatedAt: number | null;
  timeZone?: string;
}

export interface SourceStatusBarProps {
  items: readonly SourceStatusBarItem[];
  /** «Сейчас» — для детерминированных подписей. */
  nowMs: number;
  /** Подпись списка для скринридера. */
  label?: string;
  className?: string;
}

/** Минимальная ширина соседа вместе с промежутком: зона нажатия 44 px (`min-w-11`) и `gap-1`, в rem. */
const NEIGHBOUR_MIN_REM = 3;

const iconTone = {
  info: "text-info",
  warning: "text-warning",
  success: "text-success",
  neutral: "text-control",
} as const;

/** Пояснение чипа: для устаревших данных сначала сказано, что они устарели. */
function describeChip(item: SourceStatusBarItem, nowMs: number) {
  const view = describeSource({
    state: item.state,
    updatedAt: item.updatedAt,
    nowMs,
    spaceTitle: item.title,
    timeZone: item.timeZone,
  });
  const detail =
    item.state === "stale" && item.updatedAt !== null ? `Данные устарели. ${view.text}` : view.text;
  return { view, detail };
}

/**
 * Статусы вузов одной полосой чипов: «ВШЭ ✓», «ЦУ ⟳». Один компонент на «Сегодня», «Расписании» и «Заданиях» —
 * то же место (шапка) и то же поведение. Пояснение («Обновлено 20 минут назад») раскрывается по наведению,
 * фокусу с клавиатуры и касанию; нажатие ничего не фильтрует и не запускает.
 *
 * Состояние кнопки — «закреплено касанием» (`aria-pressed`): наведение и фокус раскрывают чип без него, а
 * полное пояснение уже есть в имени кнопки (`aria-label`).
 *
 * Плавность (styleguide §5): ширина пояснения анимируется через `grid-template-columns: 0fr → 1fr`, то есть
 * ровно до ширины текста за всё время перехода и без скачка `max-width`; соседние чипы стоят в одной строке
 * без переноса и сдвигаются вместе с ним, при уходе курсора возвращаются тем же переходом. Раскрытый чип не
 * сжимается — сжимаются соседи (подпись усекается). Сокращённое движение отключает переход глобальным правилом.
 */
export function SourceStatusBar({
  items,
  nowMs,
  label = "Статусы вузов",
  className,
}: SourceStatusBarProps): ReactNode {
  // Закреплённое касанием/нажатием пояснение; наведение и фокус раскрывают чип без состояния.
  const [pinned, setPinned] = useState<string | null>(null);
  const list = useRef<HTMLUListElement>(null);

  useEffect(() => {
    if (pinned === null) return;
    const onOutside = (event: PointerEvent) => {
      if (!list.current?.contains(event.target as Node)) setPinned(null);
    };
    const onEscape = (event: KeyboardEvent) => {
      // Escape принадлежит открытому слою: шит или диалог закрывается первым, а чип остаётся как был.
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (document.querySelector('[role="dialog"], [role="alertdialog"]')) return;
      setPinned(null);
    };
    document.addEventListener("pointerdown", onOutside);
    document.addEventListener("keydown", onEscape);
    return () => {
      document.removeEventListener("pointerdown", onOutside);
      document.removeEventListener("keydown", onEscape);
    };
  }, [pinned]);

  if (items.length === 0) return null;
  // Раскрытый чип не вытесняет соседей за край: каждому соседу остаётся своя минимальная зона нажатия.
  const neighbourSafeWidth = `calc(100% - ${(items.length - 1) * NEIGHBOUR_MIN_REM}rem)`;
  return (
    <ul
      ref={list}
      aria-label={label}
      data-source-status-bar=""
      className={cn("flex min-w-0 items-center gap-1", className)}
    >
      {items.map((item) => {
        const { view, detail } = describeChip(item, nowMs);
        const expanded = pinned === item.id;
        return (
          <li
            key={item.id}
            data-expanded={expanded || undefined}
            style={{ maxWidth: neighbourSafeWidth }}
            className="group/chip max-w-full min-w-11 shrink hover:shrink-0 has-[:focus-visible]:shrink-0 data-[expanded]:shrink-0"
          >
            <button
              type="button"
              aria-label={`${item.title}: ${detail}`}
              aria-pressed={expanded}
              data-source-state={item.state}
              data-space-id={item.id}
              data-expanded={expanded || undefined}
              onClick={() => setPinned(expanded ? null : item.id)}
              className="inline-flex h-11 max-w-full min-w-11 cursor-pointer items-center justify-center rounded-md px-2 type-caption text-fg-muted transition-colors duration-fast ease-soft hover:bg-hover hover:text-fg active:bg-pressed"
            >
              <span className="min-w-0 truncate">{item.title}</span>
              <Icon name={view.icon} size={16} className={cn("ml-1.5", iconTone[view.tone])} />
              <span
                data-source-detail=""
                className="grid min-w-0 grid-cols-[0fr] opacity-0 transition-[grid-template-columns,opacity] duration-slow ease-soft group-hover/chip:grid-cols-[1fr] group-hover/chip:opacity-100 group-has-[:focus-visible]/chip:grid-cols-[1fr] group-has-[:focus-visible]/chip:opacity-100 group-data-[expanded]/chip:grid-cols-[1fr] group-data-[expanded]/chip:opacity-100"
              >
                <span className="min-w-0 overflow-hidden">
                  <span className="block truncate pl-1.5">{detail}</span>
                </span>
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
