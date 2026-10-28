import { useCallback, useEffect, useRef, useState } from "react";
import { humanMessage, isApiError } from "@/api/errors";
import { isAuthInput, isTerminal, type JobSnapshot, pendingQuestions } from "@/features/jobs/job-state";

export const MANUAL_LOGIN_DEADLINE_MS = 15_000;

interface Attempt {
  controller: AbortController;
  timer: ReturnType<typeof setTimeout>;
  previousScreens: Set<string>;
  after: number;
  connectionId: string;
}

/** HTTP просит ручной вход; только свежий SSE LoginBlock действительно открывает экран. */
export function useManualLogin(
  jobId: string,
  job: JobSnapshot | undefined,
  connectionId: string | undefined,
  open: ((signal: AbortSignal) => Promise<void>) | undefined,
) {
  const attempt = useRef<Attempt | null>(null);
  const identity = useRef({ jobId, connectionId, open });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const finish = useCallback((message: string | null) => {
    const current = attempt.current;
    attempt.current = null;
    if (current) {
      clearTimeout(current.timer);
      current.controller.abort();
    }
    setBusy(false);
    setError(message);
  }, []);

  useEffect(() => {
    const previous = identity.current;
    if (previous.jobId === jobId && previous.connectionId === connectionId && previous.open === open) return;
    identity.current = { jobId, connectionId, open };
    finish(null);
  }, [jobId, connectionId, open, finish]);

  useEffect(
    () => () => {
      const current = attempt.current;
      attempt.current = null;
      if (current) {
        clearTimeout(current.timer);
        current.controller.abort();
      }
    },
    [],
  );

  useEffect(() => {
    const current = attempt.current;
    if (!current || !job) return;
    if (
      job.blocks.some(
        (block, index) =>
          block.type === "login" &&
          block.connectionId === current.connectionId &&
          !current.previousScreens.has(block.id) &&
          index > current.after,
      )
    ) {
      finish(null);
    } else if (isTerminal(job.state)) {
      finish("Вход уже завершился. Если нужно, начните его заново.");
    }
  }, [job, finish]);

  const onOpen = useCallback(
    (blockId: string) => {
      if (!open || !connectionId || !job || attempt.current || isTerminal(job.state)) return;
      if (job.answers[blockId]?.status === "sending") return;
      if (!pendingQuestions(job, connectionId).some((block) => block.id === blockId && isAuthInput(block)))
        return;
      const controller = new AbortController();
      const current: Attempt = {
        controller,
        connectionId,
        previousScreens: new Set(
          job.blocks.filter((block) => block.type === "login").map((block) => block.id),
        ),
        after: job.blocks.findIndex((block) => block.id === blockId),
        timer: setTimeout(
          () => finish("Браузер пока не открылся. Откройте его ещё раз или ответьте в полях BotAI."),
          MANUAL_LOGIN_DEADLINE_MS,
        ),
      };
      attempt.current = current;
      setBusy(true);
      setError(null);
      void open(controller.signal).catch((failure: unknown) => {
        if (attempt.current !== current) return;
        finish(
          isApiError(failure) && failure.status === 404
            ? "Браузер пока недоступен для этого шага входа. Попробуйте открыть его ещё раз."
            : humanMessage(failure),
        );
      });
    },
    [job, connectionId, open, finish],
  );

  return { busy, error, onOpen };
}
