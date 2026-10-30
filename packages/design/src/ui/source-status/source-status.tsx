import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { DEFAULT_TIME_ZONE, formatDay, formatTime, formatUpdated } from "../../lib/format";
import { Banner } from "../banner/banner";
import { Button } from "../button/button";
import { Icon, type IconName } from "../icon";

export type SourceState = "ok" | "syncing" | "stale" | "offline" | "reauth" | "repairing";

export interface SourceStatusProps {
  state: SourceState;
  /** Идентификатор пространства — для атрибута и тестов; цветом вуз не кодируется. */
  spaceId: string;
  /** Короткое название вуза для текста: «ВШЭ». */
  spaceTitle?: string;
  /** Время последнего снимка, мс. */
  updatedAt: number | null;
  /** «Сейчас» — для детерминированных подписей. */
  nowMs: number;
  timeZone?: string;
  /** «Войти снова» — единственное действие, и только для `reauth`. */
  onReauth?: () => void;
  /** `inline` — иконка и подпись; `banner` — полоса. Чипы в шапке — `SourceStatusBar`. */
  variant?: "inline" | "banner";
  className?: string;
}

interface Presentation {
  icon: IconName;
  tone: "info" | "warning" | "success" | "neutral";
  text: string;
}

/** Тексты шести состояний. Технических кодов в них нет, повторить нельзя — обновление идёт само. */
export function describeSource(
  props: Pick<SourceStatusProps, "state" | "updatedAt" | "nowMs" | "spaceTitle" | "timeZone">,
): Presentation {
  const { state, updatedAt, nowMs, spaceTitle, timeZone = DEFAULT_TIME_ZONE } = props;
  const snapshot =
    updatedAt === null
      ? null
      : `${formatDay(updatedAt, nowMs, timeZone)}, ${formatTime(updatedAt, timeZone)}`;
  switch (state) {
    case "ok":
      return {
        icon: "ok",
        tone: "success",
        text: updatedAt === null ? "Данные получены" : formatUpdated(updatedAt, nowMs, timeZone),
      };
    case "syncing":
      return { icon: "sync", tone: "info", text: "Обновляется" };
    case "stale":
      return {
        icon: "stale",
        tone: "warning",
        text: updatedAt === null ? "Данные устарели" : `Данные от ${formatDay(updatedAt, nowMs, timeZone)}`,
      };
    case "offline":
      return {
        icon: "offline",
        tone: "info",
        text: snapshot === null ? "Нет сети" : `Нет сети. Показан снимок от ${snapshot}`,
      };
    case "reauth":
      return {
        icon: "reauth",
        tone: "warning",
        text: spaceTitle ? `Нужно снова войти в ЛК ${spaceTitle}` : "Нужно снова войти в личный кабинет",
      };
    case "repairing":
      return { icon: "repairing", tone: "info", text: "Вуз что-то поменял, Варя чинит" };
  }
}

const inlineTone = {
  info: "text-info",
  warning: "text-warning",
  success: "text-success",
  neutral: "text-control",
} as const;

/**
 * Шесть видимо разных состояний источника: иконка, текст и цвет. Кнопки повтора нет ни в одном —
 * сбой сети и сайта вуза обрабатывается в фоне. «Войти снова» — бизнес-действие, не повтор запроса.
 */
export function SourceStatus(props: SourceStatusProps): ReactNode {
  const { state, spaceId, onReauth, variant = "inline", className } = props;
  const view = describeSource(props);
  const reauthAction =
    state === "reauth" && onReauth ? (
      <Button variant="secondary" size="sm" data-touch-expanded="" onClick={onReauth}>
        Войти снова
      </Button>
    ) : undefined;

  if (variant === "banner") {
    return (
      <Banner
        tone={view.tone === "success" ? "success" : view.tone}
        icon={view.icon}
        action={reauthAction}
        className={className}
      >
        <span data-source-state={state} data-space-id={spaceId}>
          {view.text}
        </span>
      </Banner>
    );
  }

  return (
    <span
      data-source-state={state}
      data-space-id={spaceId}
      className={cn("inline-flex min-w-0 items-center gap-1.5 type-caption text-fg-muted", className)}
    >
      <Icon name={view.icon} size={16} className={inlineTone[view.tone]} />
      <span className="min-w-0 truncate">{view.text}</span>
      {reauthAction}
    </span>
  );
}
