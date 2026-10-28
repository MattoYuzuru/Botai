import { Banner, Button, Dialog, Field, IconButton, Input, Sheet } from "@botai/design";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { type FormEvent, useEffect, useRef, useState } from "react";
import { humanMessage } from "@/api/errors";
import { universitiesQuery } from "@/api/queries";
import type { University } from "@/api/types";
import { UniversityPicker } from "@/features/connect/university-picker";
import { useSpaceDirectory } from "@/features/data/use-space-directory";
import { jobManager } from "@/features/jobs/job-manager";
import { isTerminal, type JobSnapshot } from "@/features/jobs/job-state";
import { clientId } from "@/lib/client-id";
import { sendConnectText, type ThreadRuntime, threadRuntimeKey } from "./connection-api";
import { SOURCE_JOBS } from "./connection-composer";
import { chatKeys } from "./queries";
import { useConnectionChat } from "./use-connection-chat";

/** Необязательный контекст разговора. Название вуза — подсказка, адрес сообщает сам человек. */
export function ConnectionContext({
  threadId,
  runtime,
  job,
}: {
  threadId: string | null;
  runtime: ThreadRuntime | undefined;
  job: JobSnapshot | null;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [selected, setSelected] = useState<University | null>(null);
  const [own, setOwn] = useState(false);
  const [ownTitle, setOwnTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [cancelOpen, setCancelOpen] = useState(false);
  const last = useRef<{ title: string; jobId: string; id: string } | null>(null);
  const directory = useSpaceDirectory();
  const existing = directory.spaces.find((space) => selected && space.universityId === selected.id);
  const conversation = useConnectionChat();
  const queryClient = useQueryClient();
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(query.trim()), 200);
    return () => window.clearTimeout(timer);
  }, [query]);
  const universities = useQuery({
    ...universitiesQuery(debounced),
    enabled: open,
    placeholderData: keepPreviousData,
  });
  const running = Boolean(job && !isTerminal(job.state) && SOURCE_JOBS.has(runtime?.activeJob?.kind ?? ""));
  const canClarify = running && !runtime?.pending.some((item) => item.channel !== "dialogue" || item.secret);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const title = selected?.title ?? ownTitle.trim();
    if (!threadId || !job || !canClarify || !title || busy) return;
    setBusy(true);
    setFailure(null);
    try {
      const attempt =
        last.current?.title === title && last.current.jobId === job.jobId
          ? last.current
          : { title, jobId: job.jobId, id: clientId() };
      last.current = attempt;
      await sendConnectText(threadId, `Мой вуз — ${title}.`, attempt.id, job.jobId);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: chatKeys.messages(threadId) }),
        queryClient.invalidateQueries({ queryKey: threadRuntimeKey(threadId) }),
      ]);
      last.current = null;
      setOpen(false);
    } catch (error) {
      setFailure(humanMessage(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <IconButton icon="university" label="Источник и подключения" onClick={() => setOpen(true)} />
      <Sheet open={open} onOpenChange={setOpen} title="Источник и подключения" size="full">
        <div className="flex flex-col gap-6">
          {runtime?.connection && (
            <div className="flex flex-col gap-1">
              <h2 className="type-heading">{runtime.connection.title}</h2>
              <p className="break-words type-caption text-fg-muted">{runtime.connection.origin}</p>
            </div>
          )}
          <p className="type-ui text-fg-muted">
            Расскажите в чате, что нужно подключить. Справочник названий помогает уточнить вуз; готовых
            интеграций в нём нет.
          </p>
          {running && (
            <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-4">
              <UniversityPicker
                query={query}
                onQuery={setQuery}
                items={universities.data ?? []}
                selected={selected}
                onSelect={(value) => {
                  setSelected(value);
                  setOwn(false);
                }}
                own={own}
                onOwn={() => {
                  setOwn(true);
                  setSelected(null);
                  setOwnTitle(query.trim());
                }}
                busy={busy}
                loading={universities.isFetching || query.trim() !== debounced}
              />
              {universities.isError && (
                <p role="status" className="type-caption text-fg-muted">
                  Список недоступен. Название можно написать в чате.
                </p>
              )}
              {selected?.thirdPartyWarning && (
                <Banner tone="warning" icon="warning">
                  {selected.thirdPartyWarning}
                </Banner>
              )}
              {existing && (
                <Banner tone="info" icon="university">
                  Этот вуз уже добавлен. Откройте разговор прежнего подключения ниже. Для нового источника
                  пришлите другую ссылку в чат.
                </Banner>
              )}
              {own && (
                <Field label="Название вуза">
                  <Input
                    value={ownTitle}
                    onChange={(event) => setOwnTitle(event.target.value)}
                    autoComplete="organization"
                    disabled={busy}
                  />
                </Field>
              )}
              {!canClarify && (
                <p className="type-caption text-fg-muted">
                  Сначала завершите вход или ответьте на текущий вопрос в чате.
                </p>
              )}
              {failure && (
                <p role="alert" className="type-caption text-danger-fg">
                  {failure}
                </p>
              )}
              <Button
                type="submit"
                variant="secondary"
                loading={busy}
                disabled={!canClarify || !(selected || ownTitle.trim()) || busy}
              >
                Указать вуз в чате
              </Button>
            </form>
          )}
          <section className="flex flex-col gap-3" aria-labelledby="conversation-connections">
            <h2 id="conversation-connections" className="type-heading">
              Ваши подключения
            </h2>
            {directory.loaded && directory.none && (
              <p className="type-ui text-fg-muted">Пока нет подключённых источников.</p>
            )}
            {directory.spaces.flatMap((space) =>
              space.connections
                .filter((item) => item.status !== "disabled")
                .map((connection) => (
                  <div
                    key={connection.id}
                    className="flex flex-wrap items-center justify-between gap-2 border-b border-border py-2"
                  >
                    <span className="min-w-0 break-words type-ui">
                      {space.shortTitle} · {connection.title}
                    </span>
                    <Button
                      variant="secondary"
                      loading={conversation.isPending && conversation.variables === connection.id}
                      disabled={busy || conversation.isPending}
                      onClick={() => {
                        conversation.mutate(connection.id, { onSuccess: () => setOpen(false) });
                      }}
                    >
                      Открыть разговор
                    </Button>
                  </div>
                )),
            )}
            <Button variant="secondary" leadingIcon="plus" asChild>
              <Link to="/connect" onClick={() => setOpen(false)}>
                Новый источник
              </Link>
            </Button>
          </section>
          {running && (
            <Button variant="ghost" onClick={() => setCancelOpen(true)} disabled={busy}>
              Отменить подключение
            </Button>
          )}
        </div>
      </Sheet>
      <Dialog
        open={cancelOpen}
        onOpenChange={setCancelOpen}
        title="Отменить подключение?"
        description="Агент остановится. Уже найденные данные сохранятся."
        confirmLabel="Отменить подключение"
        cancelLabel="Продолжить"
        onConfirm={() => {
          if (!job) return;
          void jobManager.cancel(job.jobId).then(() => {
            setOpen(false);
            if (threadId) void queryClient.invalidateQueries({ queryKey: threadRuntimeKey(threadId) });
          });
        }}
      />
    </>
  );
}
