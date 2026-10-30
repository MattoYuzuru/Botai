import { toast } from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useRef } from "react";
import type { ThreadOut } from "@/api/v3";
import { useOpenThread } from "./open-thread";
import { threadsQuery } from "./queries";

/** Чаты, которым нужен человек: агент ждёт его или есть непрочитанный ответ. Число идёт значком на вкладку «Чат». */
export function attentionCount(
  threads: readonly ThreadOut[] | undefined,
  exceptId: string | null = null,
): number {
  return (threads ?? []).filter(
    (thread) => thread.id !== exceptId && (thread.attention === "waiting_user" || thread.unread),
  ).length;
}

/** Вход просят у задач подключения; остальное — вопрос или подтверждение. */
export function attentionToast(thread: ThreadOut): { title: string; description: string } {
  const login = thread.activeTask && ["connect", "sync", "repair"].includes(thread.activeTask.kind);
  return {
    title: login ? `Ботай просит войти: ${thread.title}` : `Ботай ждёт вас: ${thread.title}`,
    description: login ? "Нужен ваш вход или ответ, чтобы продолжить." : "Нужен ваш ответ, чтобы продолжить.",
  };
}

/**
 * Наблюдатель за списком чатов (опрос раз в ~10 секунд плюс обновление при возврате на вкладку). Тред перешёл
 * в `waiting_user`, а человек смотрит не на него — тост со ссылкой «Открыть» ведёт прямо в этот тред. Первый
 * ответ списка только запоминается: уже ожидающие чаты видны в списке и на значке вкладки, а не тостом при входе.
 */
export function useThreadWatcher(): readonly ThreadOut[] | undefined {
  const threads = useQuery(threadsQuery);
  const navigate = useNavigate();
  const known = useRef<Map<string, ThreadOut["attention"]> | null>(null);

  useEffect(() => {
    const rows = threads.data;
    if (!rows) return;
    const previous = known.current;
    known.current = new Map(rows.map((thread) => [thread.id, thread.attention]));
    if (previous === null) return;
    const openId = useOpenThread.getState().id;
    for (const thread of rows) {
      if (thread.attention !== "waiting_user" || previous.get(thread.id) === "waiting_user") continue;
      if (thread.id === openId) continue;
      const { title, description } = attentionToast(thread);
      toast(title, {
        description,
        tone: "warning",
        id: `attention:${thread.id}`,
        duration: 15_000,
        action: {
          label: "Открыть",
          onClick: () => void navigate({ to: "/chat", search: { thread: thread.id } }),
        },
      });
    }
  }, [threads.data, navigate]);

  return threads.data;
}
