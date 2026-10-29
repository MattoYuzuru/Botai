import {
  Banner,
  ChatComposer,
  ChatDropZone,
  ChatLayout,
  IconButton,
  MascotDock,
  SkeletonText,
  toast,
  useLatest,
  useNow,
} from "@botai/design";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { humanMessage } from "@/api/errors";
import { meQuery } from "@/api/queries";
import type { Message } from "@/api/types";
import { useComposerKeyboard } from "@/app/keyboard";
import { documentsQuery, todayQuery } from "@/features/data/queries";
import { useReconnect } from "@/features/data/use-reconnect";
import { useSpaceDirectory } from "@/features/data/use-space-directory";
import { isTerminal as isJobTerminal } from "@/features/jobs/job-state";
import { toDeadlineView } from "@/features/tasks/deadlines";
import { AttachmentViewerHost, useAttachmentViewer } from "./attachment-viewer-host";
import { useChatLinks } from "./chat-links";
import { ChatTopBar } from "./chat-top-bar";
import { composerState } from "./composer-state";
import { ContextButton } from "./context-meter-view";
import { EmptyChat } from "./empty-chat";
import { useOpenThread } from "./open-thread";
import { chatKeys, messagesQuery, runtimeQuery, threadsQuery } from "./queries";
import { outbox, useOutbox } from "./runtime";
import { SourcesPanel } from "./sources-panel";
import { buildSuggestions } from "./suggestions";
import { ThreadsPanel } from "./threads-panel";
import { awaitingReply, buildTimeline } from "./timeline";
import { TimelineView } from "./timeline-view";
import { useAttachments } from "./use-attachments";
import { useChatMascot } from "./use-chat-mascot";
import { useMarkRead } from "./use-mark-read";
import { useStickToBottom } from "./use-stick-to-bottom";
import { useThreadJobs } from "./use-thread-jobs";

const NEW_THREAD = "new";
const POLL_WHILE_AWAITING_MS = 4_000;
const POLL_RUNTIME_MS = 5_000;
const DEFAULT_TITLE = "Новый чат";

/**
 * Чат (`/chat`) на весь экран (§3.9): один тред — один таймлайн. Подключение, вход, вопросы агента и ответы идут в
 * одной ленте; ход агента и фоновые задачи — задания этого треда. Сообщение уходит через очередь отправки: нет
 * сети — остаётся в ленте с подписью и уходит само. Вложения грузятся сразу, как выбраны, и уходят с сообщением.
 */
export function ChatScreen() {
  const { thread: param, draft: draftParam } = useSearch({ from: "/_app/chat" });
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const me = useQuery(meQuery);
  const threads = useQuery(threadsQuery);
  const directory = useSpaceDirectory();
  const keyboardOpen = useComposerKeyboard();
  const pendingAll = useOutbox();
  const reconnect = useReconnect();
  const links = useChatLinks();
  const [historyOpen, setHistoryOpen] = useState(false);
  const [sourcesOpen, setSourcesOpen] = useState(false);

  // Какой чат открыт: явный `?thread=`, иначе последний; `?draft=` без чата — новый.
  const newRequested = param === NEW_THREAD || (param === undefined && draftParam !== undefined);
  const latestId = threads.data?.[0]?.id ?? null;
  const activeId = newRequested ? null : (param ?? latestId);
  const resolving = !newRequested && param === undefined && threads.isPending;
  const activeThread = threads.data?.find((thread) => thread.id === activeId) ?? null;
  const draftKey = activeId ?? NEW_THREAD;

  // Текст в поле у каждого чата свой: переключение чата не теряет начатое.
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const draft = drafts[draftKey] ?? "";
  const setDraft = useCallback(
    (value: string) => setDrafts((all) => (all[draftKey] === value ? all : { ...all, [draftKey]: value })),
    [draftKey],
  );

  // `/chat?draft=…`: заготовка ложится в поле и сразу уходит из адреса; сообщение не отправляется.
  useEffect(() => {
    if (draftParam === undefined) return;
    const key = param && param !== NEW_THREAD ? param : NEW_THREAD;
    setDrafts((all) => ({ ...all, [key]: draftParam }));
    void navigate({ to: "/chat", search: { thread: key }, replace: true });
    queueMicrotask(() => document.querySelector<HTMLTextAreaElement>("[data-composer] textarea")?.focus());
  }, [draftParam, param, navigate]);

  // Сообщения нового чата живут в очереди, пока сервер не назвал чат; их принадлежность запоминаем.
  const [draftMessages, setDraftMessages] = useState<
    readonly { id: string; ownerId: string; threadId: string | null }[]
  >(() =>
    pendingAll
      .filter((item) => item.threadId === null)
      .map(({ id, ownerId, threadId }) => ({ id, ownerId, threadId })),
  );
  const ownerId = me.data?.user.id;
  const pending = useMemo(
    () =>
      pendingAll.filter(
        (item) =>
          item.ownerId === ownerId &&
          (activeId === null
            ? draftMessages.some((message) => message.id === item.id)
            : item.threadId === activeId),
      ),
    [pendingAll, activeId, draftMessages, ownerId],
  );
  useEffect(() => {
    if (activeId !== null) setDraftMessages([]);
  }, [activeId]);
  const assignedThreadId = draftMessages.find(
    (message) => message.ownerId === ownerId && message.threadId !== null,
  )?.threadId;
  useEffect(() => {
    if (activeId === null && assignedThreadId) {
      void navigate({ to: "/chat", search: { thread: assignedThreadId }, replace: true });
    }
  }, [activeId, assignedThreadId, navigate]);

  const jobsRef = useRef(0);
  const runtimeResult = useQuery({
    ...runtimeQuery(activeId ?? ""),
    enabled: activeId !== null,
    // Поток может оборваться: пока задание идёт, состояние треда спрашиваем и само.
    refetchInterval: (query) => {
      const data = query.state.data;
      const running =
        (data?.activeJob && !isJobTerminal(data.activeJob.state)) ||
        data?.tasks.some((task) => !isJobTerminal(task.state));
      return running ? POLL_RUNTIME_MS : false;
    },
  });
  const runtime = runtimeResult.data;
  const messagesResult = useQuery({
    ...messagesQuery(activeId ?? ""),
    enabled: activeId !== null,
    // Ответ мог прийти, пока страница была закрыта, а задание неизвестно: спрашиваем историю, пока ждём.
    refetchInterval: (query) =>
      jobsRef.current === 0 && awaitingReply(query.state.data ?? [], pending, Date.now())
        ? POLL_WHILE_AWAITING_MS
        : false,
  });
  const messages = useMemo<readonly Message[]>(() => messagesResult.data ?? [], [messagesResult.data]);
  const jobs = useThreadJobs(activeId, runtime, messages);
  jobsRef.current = jobs.filter((job) => !isJobTerminal(job.snapshot.state)).length;

  const running = jobsRef.current > 0;
  // Задание неизвестно (страницу перезагрузили, ответ потерялся), но вопрос свежий: ответ ещё может прийти.
  const awaiting = jobs.length === 0 && awaitingReply(messages, pending, Date.now());
  const composer = composerState({
    threadId: activeId,
    runtime,
    runtimeLoading: activeId !== null && runtimeResult.isPending,
    runtimeFailed: activeId !== null && runtimeResult.isError,
    jobs,
    awaiting,
  });
  const busy = composer.replying || running;
  const nowMs = useNow(busy ? 1_000 : 30_000);
  const timeZone = directory.homeZone;

  const timeline = useMemo(
    () => buildTimeline({ messages, pending, jobs, nowMs }),
    [messages, pending, jobs, nowMs],
  );
  const startingThread = activeId === null && draftMessages.some((message) => message.ownerId === ownerId);
  const loaded =
    activeId === null || (!messagesResult.isPending && !runtimeResult.isPending && !runtimeResult.isError);
  const showEmpty = !resolving && !startingThread && loaded && timeline.turns.length === 0;
  const suggestions = useSuggestions(showEmpty, nowMs);

  // Вложения поля: у каждого чата свой набор, файл грузится сразу.
  const attachments = useAttachments(draftKey);
  const openAttachment = useAttachmentViewer((state) => state.open);
  const known = useMemo(() => {
    const items = [...attachments.viewer];
    for (const turn of timeline.turns) {
      if (turn.kind === "user")
        for (const item of turn.attachments)
          items.push({
            id: "attachmentId" in item ? item.attachmentId : item.id,
            name: item.name,
            mime: item.mime,
            size: item.size,
            kind: item.kind,
            url: `/api/v1/attachments/${encodeURIComponent("attachmentId" in item ? item.attachmentId : item.id)}/content`,
          });
    }
    return items;
  }, [attachments.viewer, timeline.turns]);

  // Очередь отправки: новый чат появляется в списке, отправленное сообщение — в ленте, отказ возвращает текст в поле.
  const latest = useLatest({ activeId, draftMessages, ownerId });
  useEffect(
    () =>
      outbox.on((event) => {
        if (event.type === "thread") {
          void queryClient.invalidateQueries({ queryKey: chatKeys.threads });
          setDraftMessages((items) =>
            items.map((item) =>
              item.id === event.item.id && item.ownerId === event.item.ownerId
                ? { ...item, threadId: event.thread.id }
                : item,
            ),
          );
        } else if (event.type === "sent") {
          const sent = event.message;
          if (sent) {
            queryClient.setQueryData<Message[]>(chatKeys.messages(event.threadId), (old) =>
              old && !old.some((message) => message.id === sent.id) ? [...old, sent] : old,
            );
          }
          void queryClient.invalidateQueries({ queryKey: chatKeys.runtime(event.threadId) });
          void queryClient.invalidateQueries({ queryKey: chatKeys.threads });
        } else {
          setDraftMessages((items) => items.filter((item) => item.id !== event.item.id));
          toast("Сообщение не отправлено", { tone: "warning", description: humanMessage(event.error) });
          const current = latest.current;
          if (
            event.item.ownerId === current.ownerId &&
            (current.activeId === null
              ? current.draftMessages.some((message) => message.id === event.item.id)
              : event.item.threadId === current.activeId)
          ) {
            setDrafts((all) => {
              const key = current.activeId ?? NEW_THREAD;
              return all[key] ? all : { ...all, [key]: event.item.text };
            });
          }
        }
      }),
    [queryClient, latest],
  );

  const send = useCallback(
    (text: string, withAttachments: boolean) => {
      if (!ownerId) return;
      const files = withAttachments ? attachments.take() : [];
      const item = outbox.send(text, {
        ownerId,
        threadId: activeId,
        spaceId: null,
        attachments: files,
        targetJobId: composer.targetJobId,
      });
      if (activeId === null) {
        setDraftMessages((items) => [...items, { id: item.id, ownerId: item.ownerId, threadId: null }]);
      }
      if (withAttachments) setDraft("");
    },
    [ownerId, attachments, activeId, composer.targetJobId, setDraft],
  );

  const startNewChat = () => {
    // Новый чат начинается пустым: сообщение, которое ещё создаёт прежний чат, остаётся в очереди и уйдёт туда.
    setDraftMessages([]);
    setDrafts((all) => ({ ...all, [NEW_THREAD]: "" }));
    void navigate({ to: "/chat", search: { thread: NEW_THREAD } });
  };

  // Ответ или решение по действию принято: история и состояние треда обновляются.
  const refreshThread = useCallback(() => {
    if (!activeId) return;
    void queryClient.invalidateQueries({ queryKey: chatKeys.messages(activeId) });
    void queryClient.invalidateQueries({ queryKey: chatKeys.runtime(activeId) });
    void queryClient.invalidateQueries({ queryKey: chatKeys.threads });
  }, [activeId, queryClient]);

  // Тред открыт: наблюдатель списка чатов не присылает уведомление о нём.
  useEffect(() => {
    useOpenThread.getState().set(activeId);
    return () => useOpenThread.getState().set(null);
  }, [activeId]);

  const changeKey = `${timeline.turns.length}|${jobs.map((job) => `${job.snapshot.blocks.length}${job.snapshot.state}`).join(",")}`;
  const stick = useStickToBottom(changeKey, activeId);
  useMarkRead(
    activeId,
    activeThread?.unread ?? false,
    stick.atBottom,
    activeId !== null && !messagesResult.isPending,
  );

  // Варя: на сцене, пока чат пуст, на насесте — когда разговор идёт. Сообщений чата она не получает.
  const activeJob = jobs.find((job) => !isJobTerminal(job.snapshot.state));
  const mascot = useChatMascot({
    mode: showEmpty ? "floor" : "perch",
    replying: busy,
    waitingForHuman: composer.waitingForHuman,
    stage: activeJob?.snapshot.stage ?? null,
    typing: draft.trim().length > 0,
    keyboardOpen,
    replyCount: timeline.turns.filter((turn) => turn.kind === "block" && turn.block.type === "text").length,
    threadKey: draftKey,
  });

  const title = activeThread && activeThread.title !== DEFAULT_TITLE ? activeThread.title : DEFAULT_TITLE;
  const othersNeedYou = (threads.data ?? []).some(
    (thread) => thread.id !== activeId && (thread.unread || thread.attention === "waiting_user"),
  );
  const restart = (connectionId: string) => {
    const space = directory.spaces.find((item) => item.connections.some((c) => c.id === connectionId));
    reconnect.mutate({ connectionId, spaceId: space?.id, spaceTitle: space?.shortTitle ?? "вуз" });
  };

  return (
    <ChatDropZone onFiles={(files, source) => attachments.add(files, source, activeId)} disabled={!ownerId}>
      <ChatLayout
        keyboardOpen={keyboardOpen}
        header={
          <ChatTopBar
            title={title}
            context={<ContextButton threadId={activeId} nowMs={nowMs} />}
            othersNeedYou={othersNeedYou}
            onSources={() => setSourcesOpen(true)}
            onThreads={() => setHistoryOpen(true)}
            onNew={startNewChat}
          />
        }
        floating={
          stick.atBottom ? undefined : (
            <IconButton
              icon="chevron-down"
              label="К последнему сообщению"
              variant="secondary"
              shape="round"
              className="shadow-md"
              onClick={stick.toBottom}
            />
          )
        }
        composer={
          // Насест — верхняя кромка поля: слой Вари стоит в той же обёртке и едет вместе с полем над клавиатурой.
          <div className="relative">
            <ChatComposer
              value={draft}
              onValueChange={setDraft}
              onSend={(text) => send(text, true)}
              disabled={composer.disabled}
              placeholder={composer.placeholder}
              attachments={attachments.items}
              onAddFiles={(files, source) => attachments.add(files, source, activeId)}
              onRemoveAttachment={attachments.remove}
              onOpenAttachment={openAttachment}
            />
            <MascotDock {...mascot} />
          </div>
        }
      >
        <div ref={stick.content} className="flex min-h-full flex-1 flex-col pt-4 pb-10" data-chat-content="">
          {activeId && runtimeResult.isError ? (
            <Banner tone="danger" icon="warning">
              Не удалось открыть чат. {humanMessage(runtimeResult.error)}
            </Banner>
          ) : resolving || (activeId && !loaded && timeline.turns.length === 0) ? (
            <SkeletonText lines={4} label="Загрузка чата" />
          ) : showEmpty ? (
            <EmptyChat
              suggestions={suggestions}
              noSpaces={directory.loaded && directory.none}
              onPick={(text) => send(text, false)}
            />
          ) : (
            <div className="mt-auto">
              <TimelineView
                timeline={timeline}
                jobs={jobs}
                runtime={runtime}
                threadId={activeId}
                nowMs={nowMs}
                timeZone={timeZone}
                links={links}
                onQuickReply={(text) => send(text, false)}
                quickRepliesDisabled={composer.disabled}
                onRestart={restart}
                restarting={reconnect.isPending}
                onChanged={refreshThread}
              />
            </div>
          )}
        </div>
      </ChatLayout>
      <ThreadsPanel
        open={historyOpen}
        onOpenChange={setHistoryOpen}
        threads={threads.data}
        loading={threads.isPending}
        activeId={activeId}
        nowMs={nowMs}
        timeZone={timeZone}
        onNew={startNewChat}
        onDeleted={(id) => {
          if (id === activeId) void navigate({ to: "/chat", search: { thread: NEW_THREAD }, replace: true });
        }}
      />
      <SourcesPanel open={sourcesOpen} onOpenChange={setSourcesOpen} nowMs={nowMs} />
      <AttachmentViewerHost known={known} />
    </ChatDropZone>
  );
}

/** Подсказки пустого чата из настоящих данных: «Сегодня», сроки, документы, состояние источников. */
function useSuggestions(enabled: boolean, nowMs: number): string[] {
  const directory = useSpaceDirectory();
  const ready = enabled && directory.loaded && !directory.none;
  const today = useQuery({ ...todayQuery(), enabled: ready });
  const firstSpace = directory.spaces[0];
  const documents = useQuery({
    ...documentsQuery(firstSpace?.id ?? ""),
    enabled: ready && Boolean(firstSpace),
  });
  const events = today.data?.data.events;
  const deadlines = today.data?.data.deadlines;
  const docs = documents.data?.data;
  const attention = directory.spaces
    .filter((space) => space.health.state === "reauth" || space.health.state === "stale")
    .map((space) => space.shortTitle)
    .join("|");
  return useMemo(
    () =>
      ready
        ? buildSuggestions({
            nowMs,
            events: events ?? [],
            deadlines: (deadlines ?? [])
              .map((row) => toDeadlineView(row, nowMs))
              .filter((view) => !view.done)
              .map((view) => ({ title: view.deadline.title, dueMs: view.dueMs })),
            documentTitles: (docs ?? []).map((row) => row.title),
            needsAttention: attention ? attention.split("|") : [],
          })
        : [],
    [ready, nowMs, events, deadlines, docs, attention],
  );
}
