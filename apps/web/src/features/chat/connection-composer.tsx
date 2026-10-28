import { ChatComposer } from "@botai/design";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { humanMessage } from "@/api/errors";
import { isTerminal, type JobSnapshot } from "@/features/jobs/job-state";
import { clientId } from "@/lib/client-id";
import { sendConnectText, type ThreadRuntime, threadRuntimeKey } from "./connection-api";
import { chatKeys } from "./queries";

export const SOURCE_JOBS = new Set(["connect", "sync", "repair", "learn"]);

/** Текст этого разговора живёт только в открытом editor, никогда в persisted Outbox. */
export function ConnectionComposer({
  threadId,
  runtime,
  job,
  onTyping,
}: {
  threadId: string | null;
  runtime: ThreadRuntime | undefined;
  job: JobSnapshot | null;
  onTyping: (typing: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const last = useRef<{ text: string; jobId: string | undefined; id: string } | null>(null);
  const request = useRef<AbortController | null>(null);
  const protectedRequest = runtime?.pending.find((item) => item.channel !== "dialogue" || item.secret);
  const sourceRunning = Boolean(
    job && !isTerminal(job.state) && SOURCE_JOBS.has(runtime?.activeJob?.kind ?? ""),
  );
  const targetJobId = sourceRunning && !protectedRequest ? job?.jobId : undefined;
  const ready = Boolean(
    threadId && runtime && !protectedRequest && (!job || isTerminal(job.state) || sourceRunning),
  );
  useEffect(() => {
    onTyping(value.trim().length > 0);
  }, [value, onTyping]);
  useEffect(
    () => () => {
      request.current?.abort();
      onTyping(false);
    },
    [onTyping],
  );
  useEffect(() => {
    if (protectedRequest) setValue("");
  }, [protectedRequest]);

  const send = async (text: string) => {
    if (!ready || busy || request.current || !threadId) return;
    const attempt =
      last.current?.text === text && last.current.jobId === targetJobId
        ? last.current
        : { text, jobId: targetJobId, id: clientId() };
    last.current = attempt;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError(null);
    try {
      await sendConnectText(threadId, text, attempt.id, attempt.jobId, controller.signal);
      // Сначала canonical messages: вопрос не окажется после принятого user turn.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: chatKeys.messages(threadId) }),
        queryClient.invalidateQueries({ queryKey: threadRuntimeKey(threadId) }),
        queryClient.invalidateQueries({ queryKey: chatKeys.threads }),
      ]);
      if (!controller.signal.aborted) {
        setValue("");
        last.current = null;
      }
    } catch (failure) {
      if (!controller.signal.aborted) {
        setError(humanMessage(failure));
        void queryClient.invalidateQueries({ queryKey: threadRuntimeKey(threadId) });
      }
    } finally {
      if (request.current === controller) request.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  };

  const placeholder =
    protectedRequest?.channel === "manual"
      ? "Завершите вход в браузере выше"
      : protectedRequest
        ? "Ответьте в блоке выше"
        : ready
          ? "Ответьте агенту или уточните задачу"
          : "Агент отвечает…";
  return (
    <div className="flex flex-col gap-2">
      {error && (
        <p role="alert" className="type-caption text-danger-fg">
          {error}
        </p>
      )}
      <ChatComposer
        value={value}
        onValueChange={setValue}
        onSend={(text) => void send(text)}
        disabled={!ready || busy}
        placeholder={busy ? "Отправляю…" : placeholder}
      />
    </div>
  );
}
