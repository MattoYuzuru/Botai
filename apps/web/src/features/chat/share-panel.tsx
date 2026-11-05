import { Banner, Button, copyText, Input, Sheet, SkeletonText, toast } from "@botai/design";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { humanMessage } from "@/api/errors";
import { chatApi } from "./api";
import { chatKeys } from "./queries";

/** Адрес чата «только чтение» в этом приложении. */
export const sharedChatUrl = (shareId: string, origin = window.location.origin) =>
  `${origin}/shared/${encodeURIComponent(shareId)}`;

/**
 * «Поделиться чатом»: ссылка только для чтения, которую откроет любой, у кого есть аккаунт BotAI. По ссылке видны
 * слова переписки и названия файлов — без шагов агента, задач и самих файлов; новые сообщения тоже видны.
 * «Закрыть доступ» гасит ссылку, повторное «Поделиться» даёт новую.
 */
export function SharePanel({
  threadId,
  open,
  onOpenChange,
}: {
  threadId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const key = chatKeys.share(threadId);
  const share = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => chatApi.share(threadId, signal),
    enabled: open,
  });
  const openShare = useMutation({
    mutationFn: () => chatApi.openShare(threadId),
    onSuccess: async (created) => {
      queryClient.setQueryData(key, created);
      await copyLink(created.shareId);
    },
  });
  const closeShare = useMutation({
    mutationFn: () => chatApi.closeShare(threadId),
    onSuccess: () => {
      queryClient.setQueryData(key, null);
      toast("Доступ по ссылке закрыт", { tone: "success", id: "chat-share" });
    },
  });
  const link = share.data ? sharedChatUrl(share.data.shareId) : null;
  const failure = share.error ?? openShare.error ?? closeShare.error;

  return (
    <Sheet
      open={open}
      onOpenChange={onOpenChange}
      title="Поделиться чатом"
      description="Ссылку откроет любой, у кого есть аккаунт BotAI. Только чтение: видна переписка и названия файлов, без шагов агента и самих файлов. Новые сообщения тоже будут видны."
      size="auto"
      footer={
        link ? (
          <div className="flex flex-col gap-2">
            <Button
              variant="primary"
              fullWidth
              leadingIcon="copy"
              onClick={() => void copyLink(share.data?.shareId)}
            >
              Скопировать ссылку
            </Button>
            <Button
              variant="secondary"
              tone="danger"
              fullWidth
              loading={closeShare.isPending}
              onClick={() => closeShare.mutate()}
            >
              Закрыть доступ
            </Button>
          </div>
        ) : (
          <Button
            variant="primary"
            fullWidth
            leadingIcon="share"
            loading={openShare.isPending}
            disabled={share.isPending}
            onClick={() => openShare.mutate()}
          >
            Создать ссылку
          </Button>
        )
      }
    >
      <div className="flex flex-col gap-4" data-share-panel="">
        {failure ? (
          <Banner tone="danger" icon="error" live>
            {humanMessage(failure)}
          </Banner>
        ) : null}
        {share.isPending ? (
          <SkeletonText lines={2} label="Загрузка ссылки" />
        ) : link ? (
          <div className="flex items-center gap-2">
            <Input
              aria-label="Ссылка на чат"
              value={link}
              readOnly
              onFocus={(event) => event.currentTarget.select()}
              wrapperClassName="min-w-0 flex-1"
            />
            <Button variant="ghost" leadingIcon="external" asChild>
              <a href={link} target="_blank" rel="noopener noreferrer">
                Открыть
              </a>
            </Button>
          </div>
        ) : (
          <p className="type-body text-fg-muted">Чат пока никому не открыт.</p>
        )}
      </div>
    </Sheet>
  );
}

async function copyLink(shareId: string | undefined) {
  if (!shareId) return;
  if (await copyText(sharedChatUrl(shareId)))
    toast("Ссылка скопирована", { tone: "success", id: "chat-share" });
  else
    toast("Не удалось скопировать", {
      description: "Выделите ссылку и скопируйте вручную.",
      tone: "warning",
    });
}
