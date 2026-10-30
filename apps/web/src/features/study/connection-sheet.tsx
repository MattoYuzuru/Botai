import { Badge, Banner, Button, Dialog, formatWhen, Icon, Sheet, toast } from "@botai/design";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useRef, useState } from "react";
import { humanMessage } from "@/api/errors";
import type { Connection } from "@/api/types";
import { chatThreadLink, connectionChatLink, replaceCalendarLink } from "@/features/data/chat-links";
import {
  connectionKindTitle,
  describeConnection,
  describeTask,
  originLabel,
} from "@/features/data/connection-status";
import { DetailList, DetailRow } from "@/features/data/detail-row";
import { siteUrlOf } from "@/features/data/open-url";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import { deleteConnection, forgetPassword, refreshAfterChange } from "./connection-actions";
import { SecretForm } from "./secret-form";

export interface ConnectionSheetProps {
  /** Выбранное подключение; `null` — шит закрыт. Содержимое остаётся на время анимации закрытия. */
  connection: Connection | null;
  space: SpaceInfo | undefined;
  nowMs: number;
  onClose: () => void;
}

/**
 * Управление источником: статус человеческим языком, когда обновлялось, что сейчас делает агент, сохранён ли
 * пароль, «Открыть сайт» и чат источника, добавление ссылки календаря или ключа (значение больше не
 * показывается), «Забыть пароль» и «Удалить подключение» — оба через подтверждение, потому что необратимы.
 */
export function ConnectionSheet({ connection, space, nowMs, onClose }: ConnectionSheetProps) {
  const last = useRef<{ connection: Connection; space: SpaceInfo | undefined } | null>(null);
  if (connection) last.current = { connection, space };
  const shown = connection ? { connection, space } : last.current;
  return (
    <Sheet
      open={connection !== null}
      onOpenChange={(open) => !open && onClose()}
      title={shown?.connection.title ?? "Подключение"}
      description={
        shown ? `${connectionKindTitle(shown.connection.kind)} · ${originLabel(shown.connection)}` : undefined
      }
    >
      {shown && (
        <ConnectionDetails
          // Сброс формы и диалогов при смене подключения.
          key={shown.connection.id}
          connection={shown.connection}
          space={shown.space}
          nowMs={nowMs}
          onClose={onClose}
        />
      )}
    </Sheet>
  );
}

function ConnectionDetails({
  connection,
  space,
  nowMs,
  onClose,
}: {
  connection: Connection;
  space: SpaceInfo | undefined;
  nowMs: number;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [confirm, setConfirm] = useState<"password" | "delete" | null>(null);
  const timeZone = space?.timeZone;
  const view = describeConnection(connection, nowMs, { shortTitle: space?.shortTitle, timeZone });
  const lastSync = connection.lastSyncAt ? Date.parse(connection.lastSyncAt) : null;
  const nextSync = connection.nextSyncAt ? Date.parse(connection.nextSyncAt) : null;
  const site = siteUrlOf(connection);
  const chat = connectionChatLink(connection, space?.title ?? connection.title);

  const refresh = () => refreshAfterChange(queryClient, connection.spaceId);

  return (
    <div className="flex flex-col gap-4 pb-2">
      <div className="flex flex-wrap gap-1.5">
        {space && <Badge tone="neutral">{space.shortTitle}</Badge>}
        <Badge tone={view.tone} icon={view.icon}>
          {view.label}
        </Badge>
      </div>

      <Banner
        tone={view.tone}
        icon={view.icon}
        action={
          view.action === "reauth" || view.action === "link" ? (
            <Button variant="secondary" size="sm" data-touch-expanded="" asChild>
              <Link to={chat.to} search={chat.search} onClick={onClose}>
                {view.action === "link" ? "Новая ссылка" : "Войти снова"}
              </Link>
            </Button>
          ) : undefined
        }
      >
        <span data-source-state={connection.status}>{view.detail}</span>
      </Banner>

      <DetailList label="Сведения о подключении">
        <DetailRow icon="sync" label="Последнее обновление">
          {lastSync === null ? "Ещё не обновлялось" : formatWhen(lastSync, nowMs, timeZone)}
        </DetailRow>
        {nextSync !== null && (
          <DetailRow icon="time" label="Следующее обновление">
            {formatWhen(nextSync, nowMs, timeZone)}
          </DetailRow>
        )}
        {connection.activeTask && (
          <DetailRow icon="repairing" label="Сейчас в работе">
            {describeTask(connection.activeTask)}
          </DetailRow>
        )}
        <DetailRow icon="lock" label="Пароль">
          {connection.hasPassword
            ? "Сохранён зашифрованным: BotAI входит сам, когда сессия протухает"
            : "Не сохранён: при необходимости вход придётся подтвердить"}
        </DetailRow>
      </DetailList>

      {(site || connection.threadId) && (
        <section aria-label="Перейти" className="flex flex-col gap-2">
          {site && (
            <Button variant="secondary" asChild>
              <a href={site} target="_blank" rel="noopener noreferrer">
                <Icon name="external" size={20} />
                Открыть сайт
              </a>
            </Button>
          )}
          {connection.threadId && (
            <Button variant="secondary" asChild>
              <Link {...chatThreadLink(connection.threadId)} onClick={onClose}>
                <Icon name="chat" size={20} />
                Открыть чат источника
              </Link>
            </Button>
          )}
        </section>
      )}

      <section aria-label="Ссылка или ключ" className="flex flex-col gap-3">
        {connection.adapter === "ical" ? (
          // Календарь по ссылке: новую ссылку принимает защищённое поле чата, где её проверят и сразу прочитают.
          <>
            <p className="type-caption text-fg-muted">
              Ссылка календаря введена в защищённом поле чата и больше не показывается. Если она перестала
              работать, дайте новую: BotAI проверит её и сразу прочитает календарь.
            </p>
            <Button variant="secondary" leadingIcon="source" asChild>
              <Link
                {...replaceCalendarLink(connection.title, space?.title ?? connection.title)}
                onClick={onClose}
              >
                Ввести новую ссылку
              </Link>
            </Button>
          </>
        ) : adding ? (
          <SecretForm
            connection={connection}
            onSaved={() => {
              setAdding(false);
              void refresh();
            }}
          />
        ) : (
          <Button variant="secondary" leadingIcon="plus" onClick={() => setAdding(true)}>
            Добавить ссылку на календарь или ключ
          </Button>
        )}
      </section>

      <section aria-label="Необратимые действия" className="flex flex-col gap-2 border-t border-border pt-4">
        {connection.hasPassword && (
          <Button variant="secondary" tone="danger" leadingIcon="lock" onClick={() => setConfirm("password")}>
            Забыть пароль
          </Button>
        )}
        <Button variant="secondary" tone="danger" leadingIcon="trash" onClick={() => setConfirm("delete")}>
          Удалить подключение
        </Button>
      </section>

      <Dialog
        open={confirm === "password"}
        onOpenChange={(open) => !open && setConfirm(null)}
        title="Забыть пароль?"
        description={`BotAI удалит сохранённый пароль от «${connection.title}». Автоматический вход перестанет работать: когда сессия вуза закончится, вас попросят войти снова. Уже полученные данные останутся.`}
        confirmLabel="Забыть пароль"
        onConfirm={async () => {
          try {
            await forgetPassword(connection.id);
            toast("Пароль удалён", {
              tone: "success",
              description: "При следующем входе BotAI спросит его снова.",
            });
            await refresh();
          } catch (cause) {
            toast("Не удалось удалить пароль", { tone: "warning", description: humanMessage(cause) });
          }
        }}
      />
      <Dialog
        open={confirm === "delete"}
        onOpenChange={(open) => !open && setConfirm(null)}
        title={`Удалить «${connection.title}»?`}
        description="Данные этого источника исчезнут из расписания, заданий и ответов чата. Сохранённые пароль и ключи будут удалены. Подключить источник заново можно в любой момент."
        confirmLabel="Удалить подключение"
        onConfirm={async () => {
          try {
            await deleteConnection(connection.id);
            toast("Подключение удалено", { tone: "success" });
            onClose();
            await refresh();
          } catch (cause) {
            toast("Не удалось удалить подключение", { tone: "warning", description: humanMessage(cause) });
          }
        }}
      />
    </div>
  );
}
