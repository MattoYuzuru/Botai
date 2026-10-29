import { Badge, Button, EmptyState, Icon, Sheet, SkeletonText } from "@botai/design";
import { Link } from "@tanstack/react-router";
import type { ConnectionExtras } from "@/api/v3";
import { connectionKindTitle, describeConnection, originLabel } from "@/features/data/connection-status";
import { useSpaceDirectory } from "@/features/data/use-space-directory";
import { addSourceDraft } from "./drafts";

/**
 * Панель «Источники» (§3.9): вузы пользователя и их источники со статусами — только чтение, данные из тех же
 * запросов, что и экраны «Учёба» и «Сегодня». Источник с чатом ведёт в его тред. «Добавить источник» открывает
 * новый тред с готовым запросом: подключение — это разговор с агентом, а не отдельная форма.
 */
export function SourcesPanel({
  open,
  onOpenChange,
  nowMs,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  nowMs: number;
}) {
  const directory = useSpaceDirectory();
  return (
    <Sheet
      open={open}
      onOpenChange={onOpenChange}
      title="Источники"
      description="Вузы и то, откуда Ботай берёт данные. Подключения добавляются в чате."
      size="full"
      footer={
        <Button variant="primary" fullWidth leadingIcon="plus" asChild>
          <Link to="/chat" search={{ draft: addSourceDraft(null) }} onClick={() => onOpenChange(false)}>
            Добавить вуз
          </Link>
        </Button>
      }
    >
      {!directory.loaded ? (
        <SkeletonText lines={5} label="Загрузка источников" />
      ) : directory.none ? (
        <EmptyState
          icon="university"
          size="sm"
          title="Вузов пока нет"
          description="Напишите агенту, какой вуз подключить, — он найдёт сайт и проведёт через вход."
        />
      ) : (
        <div className="flex flex-col gap-6" data-sources-list="">
          {directory.spaces.map((space) => (
            <section
              key={space.id}
              className="flex flex-col gap-2"
              aria-labelledby={`source-space-${space.id}`}
            >
              <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
                <h3
                  id={`source-space-${space.id}`}
                  className="min-w-0 flex-1 basis-40 break-words type-subheading text-fg"
                >
                  {space.title}
                </h3>
                <Button variant="ghost" size="sm" leadingIcon="plus" data-touch-expanded="" asChild>
                  <Link
                    to="/chat"
                    search={{ draft: addSourceDraft(space) }}
                    onClick={() => onOpenChange(false)}
                    aria-label={`Добавить источник для ${space.shortTitle}`}
                  >
                    Добавить источник
                  </Link>
                </Button>
              </div>
              {space.connections.length === 0 ? (
                <p className="type-ui text-fg-muted">В этом вузе пока нет источников.</p>
              ) : (
                <ul className="flex flex-col divide-y divide-border rounded-lg border border-border">
                  {space.connections.map((connection) => {
                    const view = describeConnection(connection, nowMs, {
                      shortTitle: space.shortTitle,
                      timeZone: space.timeZone,
                    });
                    const threadId = (connection as ConnectionExtras).threadId;
                    const body = (
                      <>
                        <Icon name={view.icon} size={20} className="mt-1 shrink-0 text-fg-muted" />
                        <span className="flex min-w-0 flex-1 flex-col gap-1">
                          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                            <span className="min-w-0 break-words type-ui text-fg">{connection.title}</span>
                            <Badge tone={view.tone}>{view.label}</Badge>
                          </span>
                          <span className="type-caption text-fg-muted">
                            {connectionKindTitle(connection.kind)} · {originLabel(connection)}
                          </span>
                          <span className="type-caption text-fg-subtle">{view.detail}</span>
                        </span>
                        {threadId && (
                          <Icon name="chevron-right" size={16} className="mt-1 shrink-0 text-fg-subtle" />
                        )}
                      </>
                    );
                    return (
                      <li key={connection.id} data-connection-id={connection.id}>
                        {threadId ? (
                          <Link
                            to="/chat"
                            search={{ thread: threadId }}
                            onClick={() => onOpenChange(false)}
                            className="flex min-h-14 items-start gap-3 px-3 py-3 transition-colors duration-fast ease-soft hover:bg-hover active:bg-pressed"
                          >
                            {body}
                          </Link>
                        ) : (
                          <div className="flex min-h-14 items-start gap-3 px-3 py-3">{body}</div>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>
          ))}
        </div>
      )}
    </Sheet>
  );
}
