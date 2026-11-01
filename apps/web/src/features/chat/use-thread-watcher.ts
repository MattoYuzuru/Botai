import { toast } from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useRef } from "react";
import type { ThreadOut } from "@/api/v3";
import { compareInstants } from "@/lib/instants";
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
  const openId = useOpenThread((state) => state.id);
  const known = useRef<Map<string, ThreadOut> | null>(null);

  useEffect(() => {
    const rows = threads.data;
    if (!rows) return;
    const previous = known.current;
    known.current = new Map(rows.map((thread) => [thread.id, thread]));
    if (previous === null) return;
    for (const thread of rows) {
      if (thread.id === openId && document.visibilityState === "visible") {
        toast.dismiss(`attention:${thread.id}`);
        toast.dismiss(`answer:${thread.id}`);
        continue;
      }
      const before = previous.get(thread.id);
      const waiting = thread.attention === "waiting_user" && before?.attention !== "waiting_user";
      const answered =
        before &&
        thread.unread &&
        thread.attention !== "waiting_user" &&
        compareInstants(thread.lastMessageAt, before.lastMessageAt) > 0;
      if (!waiting && !answered) continue;
      const { title, description } = waiting
        ? attentionToast(thread)
        : {
            title: `Ботай ответил: ${thread.title}`,
            description: "Новый ответ в чате.",
          };
      toast(title, {
        description,
        tone: waiting ? "warning" : "default",
        id: `${waiting ? "attention" : "answer"}:${thread.id}`,
        duration: 15_000,
        action: {
          label: "Открыть",
          onClick: () => void navigate({ to: "/chat", search: { thread: thread.id }, resetScroll: false }),
        },
      });
    }
  }, [threads.data, navigate, openId]);

  return threads.data;
}
