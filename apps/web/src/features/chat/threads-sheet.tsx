import { Button, formatAgo, Icon, Sheet, SkeletonText } from "@botai/design";
import { Link } from "@tanstack/react-router";
import type { Thread } from "@/api/types";

/**
 * История чатов: список по времени последнего сообщения и «Новый чат» внизу, в зоне большого пальца.
 * Выбор чата — обычная ссылка (`/chat?thread=…`): работают «назад» и открытие в новой вкладке.
 */
export function ThreadsSheet({
  open,
  onOpenChange,
  threads,
  loading,
  activeId,
  nowMs,
  timeZone,
  onNew,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  threads: readonly Thread[] | undefined;
  loading: boolean;
  activeId: string | null;
  nowMs: number;
  timeZone: string;
  onNew: () => void;
}) {
  return (
    <Sheet
      open={open}
      onOpenChange={onOpenChange}
      title="Чаты"
      size="half"
      footer={
        <Button
          variant="primary"
          fullWidth
          leadingIcon="plus"
          onClick={() => {
            onNew();
            onOpenChange(false);
          }}
        >
          Новый чат
        </Button>
      }
    >
      {loading ? (
        <SkeletonText lines={4} label="Загрузка чатов" />
      ) : threads && threads.length > 0 ? (
        <ul className="flex flex-col gap-1" aria-label="Чаты">
          {threads.map((thread) => {
            const active = thread.id === activeId;
            return (
              <li key={thread.id}>
                <Link
                  to="/chat"
                  search={{ thread: thread.id }}
                  aria-current={active ? "true" : undefined}
                  onClick={() => onOpenChange(false)}
                  className="flex min-h-14 items-center gap-3 rounded-md px-3 py-2 transition-colors duration-fast ease-soft hover:bg-hover active:bg-pressed aria-[current=true]:bg-accent-subtle"
                >
                  <Icon name="chat" size={20} className={active ? "text-accent-fg" : "text-fg-muted"} />
                  <span className="flex min-w-0 flex-1 flex-col">
                    <span className="truncate type-ui text-fg">{thread.title}</span>
                    <time
                      className="type-caption text-fg-muted"
                      dateTime={thread.updatedAt}
                    >{`Последнее сообщение ${formatAgo(Date.parse(thread.updatedAt), nowMs, timeZone)}`}</time>
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="type-ui text-fg-muted">Чатов пока нет. Начните новый — он появится здесь.</p>
      )}
    </Sheet>
  );
}
