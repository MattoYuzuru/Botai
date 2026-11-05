import {
  Badge,
  Button,
  ChatMessage,
  DEFAULT_TIME_ZONE,
  EmptyState,
  formatBytes,
  formatDay,
  Icon,
  type LinkResolver,
  SkeletonText,
  useNow,
} from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { humanMessage, isApiError } from "@/api/errors";
import { Screen } from "@/app/screen";
import { chatApi, type SharedMessage } from "@/features/chat/api";
import { internalHref, internalLabel, parseInternalLink } from "@/features/chat/internal-links";
import { chatKeys } from "@/features/chat/queries";

/**
 * Внутренние ссылки чужого чата открываются в новой вкладке: тред, документ или вложение откроются, только если у
 * зрителя к ним есть доступ (иначе там «не найдено»), а сам общий чат остаётся на месте.
 */
const resolveSharedLink: LinkResolver = (url) => {
  const link = parseInternalLink(url);
  if (!link) return null;
  const href = internalHref(link);
  return {
    href,
    label: internalLabel(link),
    onOpen: () => window.open(href, "_blank", "noopener,noreferrer"),
  };
};

/**
 * Чат по ссылке «Поделиться» (`/shared/<id>`): только чтение для любого пользователя BotAI. Видны слова
 * переписки и названия файлов; поля ввода, шагов агента и задач нет. Каждое сообщение можно скопировать.
 */
export function SharedChatScreen({ shareId }: { shareId: string }) {
  const nowMs = useNow(60_000);
  const shared = useQuery({
    queryKey: chatKeys.shared(shareId),
    queryFn: ({ signal }) => chatApi.shared(shareId, signal),
    retry: false,
  });

  if (shared.isPending) {
    return (
      <Screen title="Чат" narrow>
        <SkeletonText lines={6} label="Загрузка чата" />
      </Screen>
    );
  }
  if (shared.isError) {
    const gone = isApiError(shared.error) && shared.error.status === 404;
    return (
      <Screen title="Чат" narrow>
        <EmptyState
          icon="lock"
          title={gone ? "Ссылка не открывается" : "Не удалось открыть чат"}
          description={
            gone
              ? "Владелец закрыл доступ или удалил чат. Попросите новую ссылку."
              : humanMessage(shared.error)
          }
        />
      </Screen>
    );
  }

  const thread = shared.data;
  return (
    <Screen
      title={thread.title}
      subtitle={`${thread.ownerName} · ${formatDay(Date.parse(thread.sharedAt), nowMs, DEFAULT_TIME_ZONE)}`}
      narrow
      trailing={
        thread.mine && thread.threadId ? (
          <Button variant="secondary" asChild>
            <Link to="/chat" search={{ thread: thread.threadId }}>
              Открыть чат
            </Link>
          </Button>
        ) : (
          <Badge tone="neutral" icon="show">
            Только чтение
          </Badge>
        )
      }
    >
      {thread.messages.length === 0 ? (
        <EmptyState icon="chat" size="sm" title="Сообщений пока нет" />
      ) : (
        <ol className="flex flex-col gap-5" aria-label="Сообщения" data-shared-chat="">
          {thread.messages.map((message) => (
            <li key={message.id}>
              <SharedMessageView message={message} nowMs={nowMs} />
            </li>
          ))}
        </ol>
      )}
    </Screen>
  );
}

function SharedMessageView({ message, nowMs }: { message: SharedMessage; nowMs: number }) {
  // Файлы чужие: видно только название и размер, открыть их нельзя.
  const files =
    message.attachments.length > 0 ? (
      <ul aria-label="Файлы" className="flex flex-col gap-1">
        {message.attachments.map((file, index) => (
          <li
            // Порядок файлов в сообщении не меняется.
            // biome-ignore lint/suspicious/noArrayIndexKey: у файла без id нет другого ключа
            key={index}
            className="flex min-w-0 items-center gap-2 type-caption text-fg-muted"
          >
            <Icon name="attach" size={16} />
            <span className="truncate">{file.name}</span>
            <span className="shrink-0" data-numeric="">
              {formatBytes(file.size)}
            </span>
          </li>
        ))}
      </ul>
    ) : undefined;
  return (
    <ChatMessage
      role={message.role}
      markdown={message.markdown || undefined}
      quote={message.quote ?? undefined}
      at={Date.parse(message.createdAt)}
      nowMs={nowMs}
      timeZone={DEFAULT_TIME_ZONE}
      resolveLink={resolveSharedLink}
      copyText={message.markdown || undefined}
    >
      {files}
    </ChatMessage>
  );
}
