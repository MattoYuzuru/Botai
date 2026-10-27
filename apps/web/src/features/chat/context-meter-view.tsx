import { Button, ContextMeter, ContextMeterBar, formatTokens, Sheet, SkeletonText } from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { contextQuery } from "./queries";

/**
 * Шкала контекста в шапке чата (`GET /chat/threads/{id}/context`): тонкая плоская полоса и число. Нажатие открывает
 * шит с полной полосой по сегментам и списком источников последнего ответа. Нет данных — кнопки нет: шкала
 * не рисует «пустой» контекст, которого сервер не присылал.
 */
export function ContextButton({ threadId, nowMs }: { threadId: string | null; nowMs: number }) {
  const [open, setOpen] = useState(false);
  const context = useQuery({ ...contextQuery(threadId ?? ""), enabled: threadId !== null });
  if (threadId === null || !context.data) return null;
  const { used, limit, segments } = context.data;
  return (
    <>
      <Button
        variant="ghost"
        aria-label={`Контекст агента: занято ${formatTokens(used)} из ${formatTokens(limit)} токенов`}
        onClick={() => setOpen(true)}
        data-context-button=""
        className="h-11 flex-col gap-0.5 px-2"
      >
        <ContextMeterBar limit={limit} segments={segments} className="w-12" />
        <span className="type-caption text-fg-muted" data-numeric="">
          {formatTokens(used)}
        </span>
      </Button>
      <ContextSheet open={open} onOpenChange={setOpen} threadId={threadId} nowMs={nowMs} />
    </>
  );
}

function ContextSheet({
  open,
  onOpenChange,
  threadId,
  nowMs,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  threadId: string;
  nowMs: number;
}) {
  const context = useQuery({ ...contextQuery(threadId), enabled: open });
  return (
    <Sheet
      open={open}
      onOpenChange={onOpenChange}
      title="Контекст агента"
      description="Что агент держит в памяти этого чата. Числа — оценка, а не счёт."
      size="half"
    >
      {context.data ? (
        <ContextMeter
          limit={context.data.limit}
          used={context.data.used}
          segments={context.data.segments}
          sources={context.data.sources.map((source) => ({
            title: source.title,
            kind: source.kind,
            fetchedAt: source.fetchedAt ? Date.parse(source.fetchedAt) : null,
          }))}
          nowMs={nowMs}
        />
      ) : (
        <SkeletonText lines={5} label="Загрузка шкалы контекста" />
      )}
    </Sheet>
  );
}
