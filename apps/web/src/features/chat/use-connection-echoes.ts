import { useCallback, useEffect, useRef, useState } from "react";
import type { Message } from "@/api/types";
import { onQueueReset } from "@/features/auth/data-reset";
import type { ConnectionEcho } from "./connection-timeline";

type Echo = ConnectionEcho & {
  ownerId: string;
  threadId: string;
  beforeMessages: readonly Message[];
};

function knownReset(ownerId: string | null): number {
  if (!ownerId || typeof window === "undefined") return 0;
  try {
    const value = Number(window.localStorage.getItem(`botai.data-reset.${ownerId}`));
    return Number.isFinite(value) ? value : 0;
  } catch {
    return 0;
  }
}

/** Только память открытого разговора: ни outbox, ни browser storage. */
export function useConnectionEchoes(ownerId: string | null, threadId: string | null) {
  const [echoes, setEchoes] = useState<readonly Echo[]>([]);
  const [generation, setGeneration] = useState(0);
  const current = useRef<readonly Echo[]>([]);
  const scope = useRef({ ownerId, threadId, resetAt: knownReset(ownerId) });
  const update = useCallback((next: readonly Echo[]) => {
    current.current = next;
    setEchoes(next);
  }, []);
  useEffect(() => {
    scope.current = { ownerId, threadId, resetAt: knownReset(ownerId) };
    update([]);
    return () => {
      current.current = [];
      scope.current = { ownerId: null, threadId: null, resetAt: 0 };
    };
  }, [ownerId, threadId, update]);
  useEffect(
    () =>
      onQueueReset(({ userId, resetAt }) => {
        if (scope.current.ownerId !== userId || resetAt <= scope.current.resetAt) return;
        // Server epochs, not the browser clock: even a skewed local timestamp
        // cannot retain a pre-reset echo or revive it with a late acknowledgement.
        scope.current.resetAt = resetAt;
        update([]);
        setGeneration((value) => value + 1);
      }),
    [update],
  );
  const matches = () =>
    ownerId !== null &&
    threadId !== null &&
    scope.current.ownerId === ownerId &&
    scope.current.threadId === threadId;
  const visible = echoes.filter((echo) => echo.ownerId === ownerId && echo.threadId === threadId);
  return {
    echoes: visible,
    generation,
    // GET may arrive before POST's ID is known. Keep the preceding canonical snapshot
    // until the acknowledgement permits exact-ID deduplication, never text matching.
    messages: (messages: readonly Message[]) =>
      visible.find((echo) => echo.message === null)?.beforeMessages ?? messages,
    start: (
      id: string,
      text: string,
      precedingKeys: readonly string[],
      beforeMessages: readonly Message[],
    ) => {
      if (!matches() || !ownerId || !threadId) return false;
      update([
        ...current.current.filter((echo) => echo.id !== id),
        {
          id,
          text,
          // Stable canonical aliases preserve this one anchor across ACK/GET;
          // do not retain a quadratic copy of the whole transcript per message.
          precedingKeys: precedingKeys.filter((key) => !key.startsWith("thinking:")).slice(-1),
          beforeMessages,
          ownerId,
          threadId,
          at: Date.now(),
          message: null,
        },
      ]);
      return true;
    },
    accept: (id: string, message: Message) => {
      if (!matches() || !current.current.some((echo) => echo.id === id)) return false;
      update(
        current.current.map((echo) =>
          echo.id === id ? { ...echo, text: "", message, beforeMessages: [] } : echo,
        ),
      );
      return true;
    },
    reject: (id: string) => {
      if (matches()) update(current.current.filter((echo) => echo.id !== id));
    },
  };
}
