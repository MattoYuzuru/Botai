import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef } from "react";
import { queryKeys } from "@/api/queries";
import type { Message } from "@/api/types";
import { isTaskKind, type ThreadOut, type ThreadRuntime } from "@/api/v3";
import { useActiveJob } from "@/features/jobs/active-job";
import { jobManager, useJobs } from "@/features/jobs/job-manager";
import { isTerminal, type JobSnapshot } from "@/features/jobs/job-state";
import { JOB_REF_TTL_MS, useChatJobs } from "./chat-jobs";
import { chatKeys } from "./queries";
import { mergeJob, runtimeJobs, type ThreadJob } from "./timeline";

/** Блоки задания, которых нет в истории: пока они живут только в потоке, задание из ленты не убирают. */
export function unsavedBlocks(snapshot: JobSnapshot, messages: readonly Message[]): number {
  const saved = new Set<string>();
  for (const message of messages) {
    if (message.jobId !== snapshot.jobId) continue;
    for (const block of message.blocks) if (typeof block.id === "string") saved.add(block.id);
  }
  return snapshot.blocks.filter((block) => block.type !== "thinking" && !saved.has(block.id)).length;
}

const THROTTLE_MS = 400;

/**
 * Задания открытого треда: ход агента и фоновые задачи (§3.1). Потоки поднимаются по серверному состоянию треда
 * (`runtime`) и по ссылке на ход, который только что запустило сообщение; новые события и завершение
 * подтягивают историю, состояние треда и список чатов.
 */
export function useThreadJobs(
  threadId: string | null,
  runtime: ThreadRuntime | undefined,
  messages: readonly Message[],
): ThreadJob[] {
  const queryClient = useQueryClient();
  const chatRef = useChatJobs((state) => (threadId ? state.byThread[threadId] : undefined));
  const saved = useMemo(() => runtimeJobs(runtime), [runtime]);
  const ids = useMemo(() => {
    const list = saved.map((job) => job.id);
    if (chatRef && !list.includes(chatRef.jobId)) list.push(chatRef.jobId);
    return list;
  }, [saved, chatRef]);
  const snapshots = useJobs(ids);

  // Серверное состояние поднимает потоки незавершённых заданий; после перезагрузки сервер отдаёт события из буфера.
  const savedKey = saved.map((job) => `${job.id}:${job.state}`).join("|");
  // biome-ignore lint/correctness/useExhaustiveDependencies: состав и состояния заданий уже в ключе
  useEffect(() => {
    for (const job of saved) if (!isTerminal(job.state)) jobManager.start(job.id);
  }, [savedKey]);

  // Ход агента, запущенный сообщением: привязка живёт, пока ответ не сохранён в истории целиком.
  useEffect(() => {
    if (!threadId || !chatRef) return;
    const snapshot = snapshots.get(chatRef.jobId);
    const answered = messages.some((message) => message.role === "agent" && message.jobId === chatRef.jobId);
    const expired = Date.now() - chatRef.at > JOB_REF_TTL_MS;
    const kept = snapshot ? unsavedBlocks(snapshot, messages) > 0 : false;
    if (expired || (answered && !kept && (!snapshot || isTerminal(snapshot.state)))) {
      jobManager.stop(chatRef.jobId);
      useChatJobs.getState().clear(threadId);
      return;
    }
    jobManager.start(chatRef.jobId);
  }, [threadId, chatRef, messages, snapshots]);

  const jobs = useMemo<ThreadJob[]>(() => {
    const rows = new Map<string, ThreadJob>();
    for (const job of runtime ? [...(runtime.activeJob ? [runtime.activeJob] : []), ...saved] : []) {
      rows.set(job.id, {
        id: job.id,
        kind: job.kind,
        connectionId: job.connectionId,
        snapshot: mergeJob(job, snapshots.get(job.id)),
      });
    }
    if (chatRef && !rows.has(chatRef.jobId)) {
      const snapshot = snapshots.get(chatRef.jobId);
      if (snapshot)
        rows.set(chatRef.jobId, { id: chatRef.jobId, kind: "chat", connectionId: null, snapshot });
    }
    return [...rows.values()];
  }, [runtime, saved, snapshots, chatRef]);

  // Новые события потока: история и состояние треда обновляются, но не чаще раза в 400 мс.
  const eventsKey = jobs.map((job) => `${job.id}:${job.snapshot.lastEventId ?? ""}`).join("|");
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (!threadId || !eventsKey) return;
    if (timer.current !== undefined) return;
    timer.current = window.setTimeout(() => {
      timer.current = undefined;
      void queryClient.invalidateQueries({ queryKey: chatKeys.messages(threadId) });
      void queryClient.invalidateQueries({ queryKey: chatKeys.runtime(threadId) });
      void queryClient.invalidateQueries({ queryKey: chatKeys.threads });
    }, THROTTLE_MS);
  }, [threadId, eventsKey, queryClient]);
  useEffect(
    () => () => {
      if (timer.current !== undefined) window.clearTimeout(timer.current);
      timer.current = undefined;
    },
    [],
  );

  // Задание закончилось: итог в историю, шкалу контекста и данные вуза — в экраны.
  const finished = useRef(new Set<string>());
  const doneKey = jobs
    .filter((job) => isTerminal(job.snapshot.state))
    .map((job) => job.id)
    .join("|");
  // biome-ignore lint/correctness/useExhaustiveDependencies: завершение заданий уже в ключе
  useEffect(() => {
    if (!threadId || !doneKey) return;
    for (const job of jobs) {
      if (!isTerminal(job.snapshot.state) || finished.current.has(job.id)) continue;
      finished.current.add(job.id);
      void queryClient.invalidateQueries({ queryKey: chatKeys.messages(threadId) });
      void queryClient.invalidateQueries({ queryKey: chatKeys.context(threadId) });
      void queryClient.invalidateQueries({ queryKey: chatKeys.runtime(threadId) });
      void queryClient.invalidateQueries({ queryKey: chatKeys.threads });
      if (isTaskKind(job.kind)) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.spaces });
        void queryClient.invalidateQueries({ queryKey: queryKeys.today });
      }
    }
  }, [threadId, doneKey, queryClient]);

  // Свёрнутая долгая работа видна в шапке других экранов («Подключаю ВШЭ — Читаю список курсов»).
  const task = jobs.find((job) => isTaskKind(job.kind) && !isTerminal(job.snapshot.state));
  const taskId = task?.id;
  const taskConnection = task?.connectionId ?? undefined;
  useEffect(() => {
    if (!threadId || !taskId) return;
    const title = queryClient
      .getQueryData<ThreadOut[]>(chatKeys.threads)
      ?.find((thread) => thread.id === threadId)?.activeTask?.title;
    useActiveJob
      .getState()
      .set({ jobId: taskId, title: title ?? "Работаю над задачей", connectionId: taskConnection, threadId });
  }, [threadId, taskId, taskConnection, queryClient]);

  return jobs;
}
