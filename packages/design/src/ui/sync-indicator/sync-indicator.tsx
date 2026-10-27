import { type ReactNode, useEffect, useRef, useState } from "react";
import { cn } from "../../lib/cn";

export type SyncState = "idle" | "syncing" | "repairing";

export interface SyncIndicatorProps {
  state: SyncState;
  /** `dot` — точка с мягкой пульсацией; `mascot` — Варя 20 px из слота `mascot` (если слот не передан, остаётся точка). */
  size?: "dot" | "mascot";
  mascot?: ReactNode;
  className?: string;
}

const LABELS: Record<Exclude<SyncState, "idle">, string> = {
  syncing: "Обновляется",
  repairing: "Вуз что-то поменял, Варя чинит",
};

/**
 * Фоновое обновление: ретраи идут в фоне, и человек видит только маленькую точку или Варю в шапке
 * (кнопки «Обновить» нет). `aria-live` объявляет только итог — «Данные обновлены», а не сам процесс.
 */
export function SyncIndicator({ state, size = "dot", mascot, className }: SyncIndicatorProps) {
  const previous = useRef<SyncState>(state);
  const [announcement, setAnnouncement] = useState("");

  useEffect(() => {
    if (previous.current !== "idle" && state === "idle") setAnnouncement("Данные обновлены");
    else if (state !== "idle") setAnnouncement("");
    previous.current = state;
  }, [state]);

  return (
    <span className={cn("inline-flex items-center", className)}>
      <span role="status" aria-live="polite" className="sr-only">
        {announcement}
      </span>
      {state !== "idle" &&
        (size === "mascot" && mascot ? (
          <span
            data-sync-mascot=""
            className="inline-flex size-5 items-center justify-center"
            aria-hidden="true"
          >
            {mascot}
          </span>
        ) : (
          <span
            data-sync-dot={state}
            aria-hidden="true"
            className={cn(
              "size-2 animate-pulse-soft rounded-full",
              state === "repairing" ? "bg-info" : "bg-accent-fg",
            )}
          />
        ))}
      {state !== "idle" && <span className="sr-only">{LABELS[state]}</span>}
    </span>
  );
}
