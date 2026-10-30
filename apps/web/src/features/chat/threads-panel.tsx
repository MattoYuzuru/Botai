import {
  Badge,
  Button,
  Dialog,
  formatAgo,
  Icon,
  IconButton,
  Input,
  Sheet,
  SkeletonText,
  toast,
  UnreadDot,
} from "@botai/design";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { humanMessage } from "@/api/errors";
import type { ThreadOut } from "@/api/v3";
import { chatApi } from "./api";
import { chatKeys } from "./queries";

const DEFAULT_TITLE = "Новый чат";

/** Начало ответа без разметки: сервер отдаёт текст как есть, а в списке нужна одна строка простым текстом. */
export function plainPreview(markdown: string): string {
  return markdown
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^\s*\|?[\s:|-]+\|[\s:|-]*$/gm, " ")
    .replace(/[*_`#>|]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function Attention({ thread }: { thread: ThreadOut }) {
  if (thread.attention === "waiting_user")
    return (
      <Badge tone="warning" icon="waiting">
        Ждёт вас
      </Badge>
    );
  if (thread.attention === "running")
    return (
      <Badge tone="info" icon="sync">
        Работает
      </Badge>
    );
  if (thread.attention === "failed")
    return (
      <Badge tone="danger" icon="warning">
        Не удалось
      </Badge>
    );
  return null;
}

/**
 * Список чатов (§3.9): лист на телефоне, правая панель на ноутбуке. Все треды по времени последнего сообщения;
 * кружок — есть непрочитанное, «Ждёт вас» — агент ждёт ответа, «Работает» — идёт задача. Название меняется
 * на месте, удаление спрашивает подтверждение: оно останавливает и задачи треда. Выбор чата — обычная ссылка
 * (`/chat?thread=…`): работают «назад» и открытие в новой вкладке.
 */
export function ThreadsPanel({
  open,
  onOpenChange,
  threads,
  loading,
  activeId,
  nowMs,
  timeZone,
  onNew,
  onDeleted,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  threads: readonly ThreadOut[] | undefined;
  loading: boolean;
  activeId: string | null;
  nowMs: number;
  timeZone: string;
  onNew: () => void;
  /** Удалён открытый чат: экран переходит на другой. */
  onDeleted: (id: string) => void;
}) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<{ id: string; title: string } | null>(null);
  const [removing, setRemoving] = useState<ThreadOut | null>(null);

  const rename = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) => chatApi.renameThread(id, title),
    onSuccess: (thread) => {
      queryClient.setQueryData<ThreadOut[]>(chatKeys.threads, (rows) =>
        rows?.map((row) => (row.id === thread.id ? { ...row, title: thread.title } : row)),
      );
      setEditing(null);
    },
    onError: (error) =>
      toast("Не удалось переименовать чат", { tone: "warning", description: humanMessage(error) }),
  });
  const remove = useMutation({
    mutationFn: (id: string) => chatApi.deleteThread(id),
    onSuccess: (_void, id) => {
      queryClient.setQueryData<ThreadOut[]>(chatKeys.threads, (rows) => rows?.filter((row) => row.id !== id));
      queryClient.removeQueries({ queryKey: chatKeys.messages(id) });
      queryClient.removeQueries({ queryKey: chatKeys.runtime(id) });
      toast("Чат удалён");
      onDeleted(id);
    },
    onError: (error) =>
      toast("Не удалось удалить чат", { tone: "warning", description: humanMessage(error) }),
  });

  const submitRename = (event: FormEvent) => {
    event.preventDefault();
    const title = editing?.title.trim();
    if (!editing || !title) return;
    rename.mutate({ id: editing.id, title });
  };

  return (
    <>
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
          <ul className="flex flex-col gap-1" aria-label="Чаты" data-threads-list="">
            {threads.map((thread) => {
              const active = thread.id === activeId;
              const preview = thread.preview ? plainPreview(thread.preview) : "";
              return (
                <li key={thread.id} data-thread-id={thread.id} className="flex items-center gap-1">
                  {editing?.id === thread.id ? (
                    <form onSubmit={submitRename} className="flex min-w-0 flex-1 items-center gap-1">
                      <Input
                        aria-label="Название чата"
                        value={editing.title}
                        maxLength={120}
                        autoFocus
                        onChange={(event) => setEditing({ id: thread.id, title: event.target.value })}
                        onKeyDown={(event) => {
                          if (event.key === "Escape") setEditing(null);
                        }}
                      />
                      <IconButton
                        type="submit"
                        icon="check"
                        label="Сохранить название"
                        disabled={!editing.title.trim() || rename.isPending}
                      />
                      <IconButton
                        icon="close"
                        label="Отменить переименование"
                        onClick={() => setEditing(null)}
                      />
                    </form>
                  ) : (
                    <>
                      <Link
                        to="/chat"
                        search={{ thread: thread.id }}
                        aria-current={active ? "true" : undefined}
                        onClick={() => onOpenChange(false)}
                        className="flex min-h-14 min-w-0 flex-1 items-center gap-3 rounded-md px-3 py-2 transition-colors duration-fast ease-soft hover:bg-hover active:bg-pressed aria-[current=true]:bg-accent-subtle"
                      >
                        <span className="relative shrink-0">
                          <Icon
                            name="chat"
                            size={20}
                            className={active ? "text-accent-fg" : "text-fg-muted"}
                          />
                          {thread.unread && <UnreadDot className="absolute -top-1 -right-1 ring-2 ring-bg" />}
                        </span>
                        <span className="flex min-w-0 flex-1 flex-col gap-1">
                          <span className="flex min-w-0 items-center gap-2">
                            <span
                              className={`truncate type-ui text-fg ${thread.unread ? "font-semibold" : ""}`}
                              data-thread-title=""
                            >
                              {thread.title}
                            </span>
                            <Attention thread={thread} />
                          </span>
                          {preview && <span className="truncate type-caption text-fg-muted">{preview}</span>}
                          <time className="type-caption text-fg-subtle" dateTime={thread.lastMessageAt}>
                            {formatAgo(Date.parse(thread.lastMessageAt), nowMs, timeZone)}
                          </time>
                        </span>
                      </Link>
                      <IconButton
                        icon="edit"
                        label={`Переименовать «${thread.title}»`}
                        size="sm"
                        data-touch-expanded=""
                        onClick={() => setEditing({ id: thread.id, title: thread.title })}
                      />
                      <IconButton
                        icon="trash"
                        label={`Удалить «${thread.title}»`}
                        size="sm"
                        data-touch-expanded=""
                        onClick={() => setRemoving(thread)}
                      />
                    </>
                  )}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="type-ui text-fg-muted">Чатов пока нет. Начните новый — он появится здесь.</p>
        )}
      </Sheet>
      <Dialog
        open={removing !== null}
        onOpenChange={(next) => {
          if (!next) setRemoving(null);
        }}
        title={`Удалить чат «${removing?.title ?? DEFAULT_TITLE}»?`}
        description="Переписка и вложения чата будут удалены, а запущенные в нём задачи остановятся. Подключённые источники и данные вузов останутся."
        confirmLabel="Удалить чат"
        onConfirm={async () => {
          if (removing) await remove.mutateAsync(removing.id);
        }}
      />
    </>
  );
}
