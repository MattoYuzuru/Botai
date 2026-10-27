import {
  ChatComposer,
  ChatLayout,
  IconButton,
  MascotDock,
  ScreenHeader,
  ScrollArea,
  SkeletonText,
  toast,
  useLatest,
  useNow,
} from "@botai/design";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { humanMessage } from "@/api/errors";
import { meQuery } from "@/api/queries";
import type { Message } from "@/api/types";
import { useComposerKeyboard } from "@/app/keyboard";
import { documentsQuery, todayQuery } from "@/features/data/queries";
import { useSpaceDirectory } from "@/features/data/use-space-directory";
import { jobManager, useJob } from "@/features/jobs/job-manager";
import { isTerminal, needsHuman } from "@/features/jobs/job-state";
import { toDeadlineView } from "@/features/tasks/deadlines";
import { JOB_REF_TTL_MS, useChatJobs } from "./chat-jobs";
import { ContextButton } from "./context-meter-view";
import { EmptyChat } from "./empty-chat";
import { chatKeys, messagesQuery, threadsQuery } from "./queries";
import { outbox, useOutbox } from "./runtime";
import { buildSuggestions } from "./suggestions";
import { ThreadsSheet } from "./threads-sheet";
import { awaitingReply, buildTimeline, isReplying, keptBlocks } from "./timeline";
import { TimelineView } from "./timeline-view";
import { useChatMascot } from "./use-chat-mascot";
import { useStickToBottom } from "./use-stick-to-bottom";

const NEW_THREAD = "new";
const POLL_WHILE_AWAITING_MS = 4_000;

/**
 * Чат (`/chat`): последний чат или новый, история чатов в шите, шкала контекста в шапке. Сообщение уходит через
 * очередь отправки: нет сети — остаётся в ленте с подписью и уходит само. Ответ приходит по потоку задания:
 * шаги, вопросы агента (общие блоки `AgentUiBlock`) и текст с источниками.
 */
export function ChatScreen() {
  const { thread: param } = useSearch({ from: "/_app/chat" });
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const me = useQuery(meQuery);
  const threads = useQuery(threadsQuery);
  const directory = useSpaceDirectory();
  const keyboardOpen = useComposerKeyboard();
  const pendingAll = useOutbox();
  const [draft, setDraft] = useState("");
  // Сохраняем принадлежность черновику, пока очередь получает threadId раньше маршрута.
  const [draftMessages, setDraftMessages] = useState<
    readonly { id: string; ownerId: string; threadId: string | null }[]
  >(() =>
    pendingAll
      .filter((item) => item.threadId === null)
      .map(({ id, ownerId, threadId }) => ({ id, ownerId, threadId })),
  );
  const [historyOpen, setHistoryOpen] = useState(false);

  // Какой чат открыт: явный `?thread=`, иначе последний, иначе пустой новый.
  const latestId = threads.data?.[0]?.id ?? null;
  const activeId = param === NEW_THREAD ? null : (param ?? latestId);
  const resolving = param === undefined && threads.isPending;
  const activeThread = threads.data?.find((thread) => thread.id === activeId) ?? null;

  const pending = useMemo(
    () =>
      pendingAll.filter(
        (item) =>
          item.ownerId === me.data?.user.id &&
          (activeId === null
            ? draftMessages.some((message) => message.id === item.id)
            : item.threadId === activeId),
      ),
    [pendingAll, activeId, draftMessages, me.data?.user.id],
  );
  useEffect(() => {
    if (activeId !== null) setDraftMessages([]);
  }, [activeId]);
  const assignedThreadId = draftMessages.find(
    (message) => message.ownerId === me.data?.user.id && message.threadId !== null,
  )?.threadId;
  useEffect(() => {
    if (activeId === null && assignedThreadId) {
      void navigate({ to: "/chat", search: { thread: assignedThreadId }, replace: true });
    }
  }, [activeId, assignedThreadId, navigate]);
  const jobRef = useChatJobs((state) => (activeId ? state.byThread[activeId] : undefined));
  const job = useJob(jobRef?.jobId) ?? null;

  const messagesResult = useQuery({
    ...messagesQuery(activeId ?? ""),
    enabled: activeId !== null,
    // Ответ мог прийти, пока страница была закрыта, или задание неизвестно: спрашиваем историю, пока ждём.
    refetchInterval: (query) =>
      !job && awaitingReply(query.state.data ?? [], pending, Date.now()) ? POLL_WHILE_AWAITING_MS : false,
  });
  const messages = useMemo<readonly Message[]>(() => messagesResult.data ?? [], [messagesResult.data]);

  const awaiting = !job && awaitingReply(messages, pending, Date.now());
  const replying = isReplying(job, awaiting);
  const waitingForHuman = job ? needsHuman(job) : false;
  const nowMs = useNow(replying ? 1_000 : 30_000);
  const timeZone = directory.homeZone;

  // Поток ответа: после перезагрузки привязка к заданию поднимает его заново; устаревшая — снимается.
  useEffect(() => {
    if (!activeId || !jobRef) return;
    const answered = messages.some((message) => message.role === "agent" && message.jobId === jobRef.jobId);
    const expired = Date.now() - jobRef.at > JOB_REF_TTL_MS;
    const kept = job ? keptBlocks(job).length > 0 : false;
    if (expired || (answered && !kept && (!job || isTerminal(job.state)))) {
      jobManager.stop(jobRef.jobId);
      useChatJobs.getState().clear(activeId);
      return;
    }
    jobManager.start(jobRef.jobId);
  }, [activeId, jobRef, messages, job]);

  // Задание закончилось: подтягиваем историю и шкалу; успешное — отпускаем, сбой остаётся в ленте до следующего вопроса.
  const finishedJobId = job && isTerminal(job.state) ? job.jobId : null;
  const finishedState = job?.state;
  const finishedKeeps = job ? keptBlocks(job).length > 0 : false;
  useEffect(() => {
    if (!activeId || !finishedJobId) return;
    let cancelled = false;
    void (async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: chatKeys.messages(activeId) }),
        queryClient.invalidateQueries({ queryKey: chatKeys.context(activeId) }),
        queryClient.invalidateQueries({ queryKey: chatKeys.threads }),
      ]);
      // Сбой и готовый текст для ручной отправки остаются в ленте до следующего вопроса.
      if (cancelled || finishedState !== "done" || finishedKeeps) return;
      jobManager.stop(finishedJobId);
      useChatJobs.getState().clear(activeId);
    })();
    return () => {
      cancelled = true;
    };
  }, [activeId, finishedJobId, finishedState, finishedKeeps, queryClient]);

  // Очередь отправки: новый чат появляется в списке, отправленное сообщение — в ленте, отказ возвращает текст в поле.
  const latest = useLatest({ activeId, param, draftMessages, ownerId: me.data?.user.id });
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
            setDraft((current) => current || event.item.text);
          }
        }
      }),
    [queryClient, latest],
  );

  const timeline = useMemo(
    () => buildTimeline({ messages, pending, job, nowMs }),
    [messages, pending, job, nowMs],
  );
  const startingThread =
    activeId === null && draftMessages.some((message) => message.ownerId === me.data?.user.id);
  const showEmpty =
    !startingThread && !resolving && !(activeId && messagesResult.isPending) && timeline.length === 0;

  const suggestions = useSuggestions(showEmpty, nowMs);

  const send = (text: string) => {
    if (!me.data) return;
    const item = outbox.send(text, { ownerId: me.data.user.id, threadId: activeId, spaceId: null });
    if (activeId === null) {
      setDraftMessages((items) => [...items, { id: item.id, ownerId: item.ownerId, threadId: null }]);
    }
    setDraft("");
  };

  const startNewChat = () => {
    setDraft("");
    setDraftMessages([]);
    void navigate({ to: "/chat", search: { thread: NEW_THREAD } });
  };

  // Варя: на сцене, пока чат пуст, на насесте — когда разговор идёт. Сообщений чата она не получает.
  const mascot = useChatMascot({
    mode: showEmpty || (activeId === null && !startingThread && timeline.length === 0) ? "floor" : "perch",
    replying,
    waitingForHuman,
    stage: job && !isTerminal(job.state) ? job.stage : null,
    typing: draft.trim().length > 0,
    keyboardOpen,
    replyCount: timeline.filter(
      (item) =>
        item.kind === "agent" || (item.kind === "live" && item.blocks.some((block) => block.type === "text")),
    ).length,
    threadKey: activeId ?? NEW_THREAD,
  });

  const scroll = useStickToBottom(timeline.length + (job?.blocks.length ?? 0) + (job?.state ?? ""), activeId);
  // Название экрана — «Чат», как у остальных вкладок; тема открытого чата — подзаголовок.
  const subtitle = activeThread && activeThread.title !== "Новый чат" ? activeThread.title : undefined;

  return (
    <ChatLayout
      keyboardOpen={keyboardOpen}
      header={
        <ScreenHeader
          narrow
          sticky={false}
          title="Чат"
          subtitle={subtitle}
          trailing={
            <>
              <ContextButton threadId={activeId} nowMs={nowMs} />
              <IconButton icon="stale" label="История чатов" onClick={() => setHistoryOpen(true)} />
              <IconButton icon="plus" label="Новый чат" onClick={startNewChat} />
            </>
          }
        />
      }
      composer={
        // Насест — верхняя кромка поля: слой Вари стоит в той же обёртке и едет вместе с полем над клавиатурой.
        <div className="relative">
          <ChatComposer
            value={draft}
            onValueChange={setDraft}
            onSend={send}
            disabled={replying}
            placeholder={
              waitingForHuman
                ? "Ответьте на вопрос выше"
                : replying
                  ? "Агент отвечает…"
                  : "Спросите про пары и сроки"
            }
          />
          <MascotDock {...mascot} />
        </div>
      }
    >
      <ScrollArea
        className="h-full"
        viewportRef={scroll.viewport}
        onViewportScroll={scroll.onScroll}
        viewportClassName="px-safe-4 xs:px-safe-5"
      >
        <div
          className={`mx-auto flex min-h-full w-full max-w-col flex-col justify-end pt-4 ${showEmpty ? "pb-4" : "pb-10"}`}
        >
          {resolving || (activeId && messagesResult.isPending && timeline.length === 0) ? (
            <SkeletonText lines={4} label="Загрузка чата" />
          ) : showEmpty ? (
            <EmptyChat
              suggestions={suggestions}
              noSpaces={directory.loaded && directory.none}
              onPick={send}
            />
          ) : (
            <TimelineView items={timeline} job={job} nowMs={nowMs} timeZone={timeZone} />
          )}
        </div>
      </ScrollArea>
      <ThreadsSheet
        open={historyOpen}
        onOpenChange={setHistoryOpen}
        threads={threads.data}
        loading={threads.isPending}
        activeId={activeId}
        nowMs={nowMs}
        timeZone={timeZone}
        onNew={() => {
          startNewChat();
        }}
      />
    </ChatLayout>
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
