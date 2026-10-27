import { Dialog as DialogPrimitive } from "radix-ui";
import {
  type FocusEvent,
  type PointerEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { cn } from "../../lib/cn";
import { IconButton } from "../icon-button/icon-button";

export interface SheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Заголовок шита — `type-heading`, он же имя диалога для скринридера. */
  title: ReactNode;
  description?: ReactNode;
  /** Главное действие шита лежит внизу, в зоне большого пальца. */
  footer?: ReactNode;
  /** `auto` — по содержимому, `half` — 60 % экрана, `full` — почти весь экран. На ноутбуке шит — правая панель 420 px. */
  size?: "auto" | "half" | "full";
  /** `false` — закрыть можно только кнопкой в самом шите (идёт необратимая операция). */
  dismissible?: boolean;
  children: ReactNode;
  className?: string;
  /** Скрывает заголовок визуально (остаётся для скринридера). */
  hideTitle?: boolean;
}

const sizes = {
  auto: "max-h-[92dvh]",
  half: "h-[60dvh] max-h-[92dvh]",
  full: "h-[92dvh]",
} as const;

/** Порог закрытия свайпом: сдвиг в пикселях или скорость в px/мс. */
const CLOSE_DISTANCE = 96;
const CLOSE_VELOCITY = 0.6;

/**
 * Шит вместо модалки (styleguide §7): вторичная задача открывается снизу, `rounded-xl` сверху,
 * ручка 32×4 px, закрытие свайпом вниз, кнопкой и `Esc`. На ≥ 1024 px — правая панель 420 px.
 * Свайп всегда дублируется кнопкой «Закрыть». Активное поле прокручивается в вид при фокусе.
 */
export function Sheet({
  open,
  onOpenChange,
  title,
  description,
  footer,
  size = "auto",
  dismissible = true,
  children,
  className,
  hideTitle = false,
}: SheetProps) {
  const [dragY, setDragY] = useState(0);
  const [dragging, setDragging] = useState(false);
  const start = useRef<{ y: number; t: number; id: number } | null>(null);
  const closeTimer = useRef<number | undefined>(undefined);
  // Шит открывают кнопкой, которая не Radix Trigger: фокус возвращаем сами.
  const opener = useRef<HTMLElement | null>(null);

  useEffect(() => () => window.clearTimeout(closeTimer.current), []);
  // Закрытый шит не должен помнить сдвиг прошлого жеста.
  useEffect(() => {
    if (!open) setDragY(0);
  }, [open]);

  const onPointerDown = useCallback((event: PointerEvent<HTMLElement>) => {
    if (event.pointerType === "mouse") return;
    start.current = { y: event.clientY, t: event.timeStamp, id: event.pointerId };
    event.currentTarget.setPointerCapture?.(event.pointerId);
    setDragging(true);
  }, []);

  const onPointerMove = useCallback((event: PointerEvent<HTMLElement>) => {
    const origin = start.current;
    if (!origin || origin.id !== event.pointerId) return;
    setDragY(Math.max(0, event.clientY - origin.y));
  }, []);

  const finishDrag = useCallback(
    (event: PointerEvent<HTMLElement>) => {
      const origin = start.current;
      if (!origin || origin.id !== event.pointerId) return;
      start.current = null;
      setDragging(false);
      const distance = Math.max(0, event.clientY - origin.y);
      const velocity = distance / Math.max(1, event.timeStamp - origin.t);
      if (dismissible && (distance > CLOSE_DISTANCE || (distance > 24 && velocity > CLOSE_VELOCITY))) {
        onOpenChange(false);
      } else setDragY(0);
    },
    [dismissible, onOpenChange],
  );

  // Клавиатура на телефоне закрывает нижнюю часть: поле, получившее фокус, прокручиваем в вид.
  const revealField = (event: FocusEvent<HTMLElement>) => {
    const target = event.target;
    if (target instanceof HTMLElement && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) {
      window.setTimeout(() => target.scrollIntoView({ block: "nearest" }), 250);
    }
  };

  return (
    <DialogPrimitive.Root
      open={open}
      onOpenChange={(next) => (dismissible || next ? onOpenChange(next) : undefined)}
    >
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="motion-overlay fixed inset-0 z-scrim bg-overlay" />
        <DialogPrimitive.Content
          {...(description ? {} : { "aria-describedby": undefined })}
          onOpenAutoFocus={() => {
            opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            opener.current?.focus();
          }}
          onInteractOutside={(event) => {
            if (!dismissible) event.preventDefault();
          }}
          onEscapeKeyDown={(event) => {
            if (!dismissible) event.preventDefault();
          }}
          onFocusCapture={revealField}
          data-dragging={dragging || undefined}
          style={dragY ? { transform: `translateY(${dragY}px)` } : undefined}
          className={cn(
            "motion-sheet fixed inset-x-0 bottom-0 z-layer mx-auto flex w-full max-w-col flex-col rounded-t-xl border border-b-0 border-border bg-surface-raised text-fg shadow-lg outline-none",
            "lg:inset-y-0 lg:right-0 lg:left-auto lg:mx-0 lg:max-h-none lg:h-auto lg:w-panel lg:max-w-none lg:rounded-none lg:border-y-0 lg:border-r-0 lg:border-l",
            "data-[dragging]:transition-none",
            dragY === 0 && "transition-transform duration-base ease-soft",
            sizes[size],
            className,
          )}
        >
          <div
            data-sheet-handle=""
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={finishDrag}
            onPointerCancel={finishDrag}
            className="flex shrink-0 touch-none flex-col gap-1 px-4 pt-2 pb-3 lg:touch-auto"
          >
            <div aria-hidden="true" className="mx-auto h-1 w-8 rounded-full bg-border-strong lg:hidden" />
            <div className="flex items-start justify-between gap-3 pt-2">
              <div className="min-w-0 flex-1">
                <DialogPrimitive.Title className={cn("type-heading text-fg", hideTitle && "sr-only")}>
                  {title}
                </DialogPrimitive.Title>
                {description && (
                  <DialogPrimitive.Description className="mt-1 type-ui text-fg-muted">
                    {description}
                  </DialogPrimitive.Description>
                )}
              </div>
              <DialogPrimitive.Close asChild>
                <IconButton icon="close" label="Закрыть" size="sm" className="-mt-1 -mr-1" />
              </DialogPrimitive.Close>
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pb-4">{children}</div>
          {footer && <div className="shrink-0 border-t border-border px-4 pt-3 pb-safe-3">{footer}</div>}
          {!footer && <div className="shrink-0 pb-safe" />}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
