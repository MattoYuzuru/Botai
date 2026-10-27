import { type ReactElement, type ReactNode, useLayoutEffect, useRef } from "react";
import { cn } from "../../lib/cn";
import { Icon, type IconName } from "../icon";

export interface TabItem {
  id: string;
  label: string;
  icon: IconName;
  href?: string;
  /** Счётчик на иконке (например, «нужен ответ»). */
  badge?: number;
}

export interface TabLinkProps {
  className: string;
  "aria-current"?: "page";
  onClick?: () => void;
  children: ReactNode;
}

export interface TabBarProps {
  /** Ровно пять пунктов (styleguide §7.1). Шестой вкладки не бывает: всё новое — раздел внутри «Учёбы». */
  items: readonly TabItem[];
  activeId: string;
  onSelect?: (id: string) => void;
  /** Ссылка роутера вместо `<a href>`: получает готовые классы и содержимое. */
  renderLink?: (item: TabItem, props: TabLinkProps) => ReactElement;
  /** Знак сверху боковой панели (от 1024 px). */
  brand?: ReactNode;
  /** Низ боковой панели (от 1024 px): профиль, состояние обновления. */
  footer?: ReactNode;
  /** Скрыть панель — открыта клавиатура ввода. */
  hidden?: boolean;
  /** Нижняя полоса в потоке страницы без боковой панели: предпросмотр на витрине. */
  inline?: boolean;
  className?: string;
}

export const TAB_COUNT = 5;

/**
 * Главная навигация. До 1024 px — нижняя панель (иконка 24 + подпись 13 px, активная — `accent-fg`
 * и вес 500, бегунок плавно переезжает); от 1024 px — левая панель 240 px с `Wordmark` сверху.
 * Подписи на узкой панели (320 px) и при крупном тексте скрываются, иконки остаются.
 */
export function TabBar({
  items,
  activeId,
  onSelect,
  renderLink,
  brand,
  footer,
  hidden = false,
  inline = false,
  className,
}: TabBarProps) {
  const listRef = useRef<HTMLUListElement>(null);
  const indicatorRef = useRef<HTMLSpanElement>(null);

  if (items.length !== TAB_COUNT) {
    console.error(`TabBar: нужно ровно ${TAB_COUNT} пунктов, получено ${items.length} (styleguide §7.1).`);
  }

  // Бегунок: положение активного пункта пишем в CSS-переменные, движение — transition по transform.
  // biome-ignore lint/correctness/useExhaustiveDependencies: activeId меняет атрибут aria-current — бегунок переезжает
  useLayoutEffect(() => {
    const list = listRef.current;
    const indicator = indicatorRef.current;
    if (!list || !indicator) return;
    const place = () => {
      const active = list.querySelector<HTMLElement>('[aria-current="page"]');
      if (!active) return;
      indicator.style.setProperty(
        "--tab-x",
        `${list.offsetLeft + active.offsetLeft + active.offsetWidth / 2}px`,
      );
    };
    place();
    const observer = new ResizeObserver(place);
    observer.observe(list);
    return () => observer.disconnect();
  }, [activeId]);

  // В режиме inline панель остаётся нижней полосой в потоке страницы (витрина, предпросмотр).
  const wide = (classes: string) => (inline ? "" : classes);

  return (
    <nav
      aria-label="Основная навигация"
      data-hidden={hidden || undefined}
      className={cn(
        "@container relative z-dock border-t border-border bg-bg-subtle px-safe pb-safe-2",
        !inline && "fixed inset-x-0 bottom-0",
        "data-[hidden]:hidden",
        wide(
          "lg:inset-y-0 lg:right-auto lg:flex lg:w-rail lg:flex-col lg:gap-6 lg:border-t-0 lg:border-r lg:px-3 lg:pt-safe-5 lg:pb-safe-4",
        ),
        className,
      )}
    >
      {brand && <div className={cn("hidden px-3", wide("lg:block"))}>{brand}</div>}
      <span
        ref={indicatorRef}
        aria-hidden="true"
        data-tab-indicator=""
        className={cn(
          "pointer-events-none absolute top-0 left-0 h-0.5 w-6 rounded-full bg-accent transition-transform duration-slow ease-soft",
          wide("lg:hidden"),
        )}
        style={{ transform: "translateX(calc(var(--tab-x, 0px) - 50%))" }}
      />
      <ul
        ref={listRef}
        className={cn("relative flex justify-around", wide("lg:flex-col lg:justify-start lg:gap-0.5"))}
      >
        {items.map((item) => {
          const active = item.id === activeId;
          const className = cn(
            "flex min-h-14 min-w-11 flex-col items-center justify-center gap-0.5 rounded-md px-2 py-1.5",
            "transition-colors duration-fast ease-soft",
            wide("lg:min-h-11 lg:w-full lg:flex-row lg:justify-start lg:gap-3 lg:px-3 lg:py-2"),
            active
              ? cn("font-medium text-accent-fg", wide("lg:bg-accent-subtle"))
              : cn("text-fg-muted hover:text-fg", wide("lg:hover:bg-hover lg:active:bg-pressed")),
          );
          const content = (
            <>
              <span className="relative">
                <Icon name={item.icon} size={24} />
                {item.badge ? (
                  <span
                    data-tab-badge=""
                    className="absolute -top-1.5 -right-2.5 grid min-w-5 place-items-center rounded-full bg-accent px-1 type-caption font-medium text-fg-on-accent"
                  >
                    {item.badge}
                  </span>
                ) : null}
              </span>
              <span
                className={cn(
                  "type-label",
                  inline ? "@max-[22.5em]:sr-only" : "@max-[22.5em]:max-lg:sr-only",
                )}
              >
                {item.label}
              </span>
            </>
          );
          const props: TabLinkProps = {
            className,
            "aria-current": active ? "page" : undefined,
            onClick: onSelect ? () => onSelect(item.id) : undefined,
            children: content,
          };
          return (
            <li key={item.id} className={cn("flex min-w-11 justify-center", wide("lg:w-full"))}>
              {renderLink ? renderLink(item, props) : <a href={item.href ?? "#"} {...props} />}
            </li>
          );
        })}
      </ul>
      {footer && <div className={cn("mt-auto hidden", wide("lg:block"))}>{footer}</div>}
    </nav>
  );
}
