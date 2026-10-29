import { Button, IconButton, type IconName, UnreadDot, useMediaQuery } from "@botai/design";
import type { ReactNode } from "react";

/**
 * Верхняя полоса чата на всю ширину области. Слева название треда и подпись «Чат», справа фиксированные
 * «Источники», «Чаты», «Новый чат» (как «Поделиться» и меню в ChatGPT) — на одной линии со знаком рейла. На телефоне
 * та же полоса компактнее: те же действия иконками.
 */
export function ChatTopBar({
  title,
  context,
  othersNeedYou,
  onSources,
  onThreads,
  onNew,
}: {
  title: string;
  /** Шкала контекста агента: кнопка слева от действий. */
  context?: ReactNode;
  /** В других чатах есть непрочитанное или агент ждёт ответа. */
  othersNeedYou: boolean;
  onSources: () => void;
  onThreads: () => void;
  onNew: () => void;
}) {
  const wide = useMediaQuery("(min-width: 64rem)");
  const action = (label: string, icon: IconName, onClick: () => void, dot = false) =>
    wide ? (
      <Button
        key={label}
        variant="ghost"
        size="sm"
        leadingIcon={icon}
        onClick={onClick}
        data-touch-expanded=""
      >
        {label}
        {dot && <UnreadDot label="В других чатах есть непрочитанное" className="ml-1" />}
      </Button>
    ) : (
      <span key={label} className="relative">
        <IconButton icon={icon} label={label} onClick={onClick} />
        {dot && (
          <UnreadDot
            label="В других чатах есть непрочитанное"
            className="pointer-events-none absolute top-2 right-2"
          />
        )}
      </span>
    );
  return (
    <div
      data-chat-topbar=""
      className="flex min-h-topbar flex-wrap items-center gap-x-3 gap-y-1 px-safe-4 py-1 xs:px-safe-5 lg:px-6"
    >
      <div className="flex min-w-0 flex-1 basis-32 flex-col justify-center">
        <h1 className="truncate type-title text-fg">{title}</h1>
        <p className="truncate type-caption text-fg-muted">Чат</p>
      </div>
      <div className="ml-auto flex max-w-full flex-wrap items-center justify-end gap-1">
        {context}
        {action("Источники", "connection", onSources)}
        {action("Чаты", "chat", onThreads, othersNeedYou)}
        {action("Новый чат", "plus", onNew)}
      </div>
    </div>
  );
}
