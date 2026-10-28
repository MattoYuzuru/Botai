import { Button, ChatMessage, ThinkingStream } from "@botai/design";
import { useMemo, useState } from "react";
import { api, call } from "@/api/client";
import { ApiError, humanMessage } from "@/api/errors";
import { AgentBlockView } from "@/features/agent-blocks/agent-block";
import type { BlockContext } from "@/features/agent-blocks/context";
import { jobManager } from "@/features/jobs/job-manager";
import { isTerminal, type JobSnapshot, jobFailed, jobProblem } from "@/features/jobs/job-state";
import { useManualLogin } from "@/widgets/progress-wall/use-manual-login";
import type { ThreadRuntime } from "./connection-api";
import { SOURCE_JOBS } from "./connection-composer";
import { type ConnectTurn, interactionKey } from "./connection-timeline";
import { useInternalLink } from "./timeline-view";

export function ConnectTimeline({
  turns,
  runtime,
  job,
  nowMs,
  timeZone,
  onChanged,
  onRestart,
  restarting,
}: {
  turns: readonly ConnectTurn[];
  runtime: ThreadRuntime;
  job: JobSnapshot | null;
  nowMs: number;
  timeZone: string;
  onChanged: () => void;
  onRestart: () => void;
  restarting: boolean;
}) {
  const internalLink = useInternalLink();
  const connectionId = runtime.connection?.id;
  const openBrowser = useMemo(
    () =>
      connectionId
        ? async (signal: AbortSignal) => {
            await call(
              api.POST("/api/v1/connections/{connection_id}/login-session", {
                params: { path: { connection_id: connectionId } },
                signal,
              }),
            );
          }
        : undefined,
    [connectionId],
  );
  const manual = useManualLogin(job?.jobId ?? "", job ?? undefined, connectionId, openBrowser);
  const pending = new Map(runtime.pending.map((item) => [interactionKey(item.jobId, item.blockId), item]));
  const history: BlockContext = {
    jobId: "",
    answers: {},
    nowMs,
    timeZone,
    readOnly: true,
    onAnswer: () => {},
    onApprove: () => {},
    onReject: () => {},
    internalLink,
  };
  const active: BlockContext = {
    ...history,
    jobId: job?.jobId ?? "",
    answers: job?.answers ?? {},
    readOnly: false,
    editorOnly: true,
    manualLogin: connectionId && openBrowser ? { ...manual, connectionId } : undefined,
    onAnswer: async (id, values, shown) => {
      if (!job) return;
      await jobManager.answer(job.jobId, id, values, shown, connectionId);
      onChanged();
    },
    onApprove: async (id, proposal, fields) => {
      if (!job) return;
      await jobManager.approve(job.jobId, id, proposal, fields);
      onChanged();
    },
    onReject: async (id, proposal) => {
      if (!job) return;
      await jobManager.reject(job.jobId, id, proposal);
      onChanged();
    },
  };
  const problem = job ? jobProblem(job) : null;
  const failed = job ? jobFailed(job) : false;
  const source = SOURCE_JOBS.has(runtime.activeJob?.kind ?? "");
  return (
    <ol className="flex flex-col gap-4" aria-label="Сообщения" data-timeline="">
      {turns.map((turn) => (
        <li key={turn.key} data-conversation-turn={turn.key}>
          {turn.kind === "user" ? (
            <ChatMessage role="user" markdown={turn.text} at={turn.at} nowMs={nowMs} timeZone={timeZone} />
          ) : turn.kind === "thinking" ? (
            <ConnectThinking turn={turn} nowMs={nowMs} />
          ) : (
            (() => {
              if (turn.block.type === "summary" && turn.jobId === job?.jobId && failed) return null;
              const request = turn.jobId ? pending.get(interactionKey(turn.jobId, turn.block.id)) : undefined;
              const live = request && job?.jobId === turn.jobId && !isTerminal(job.state);
              const question = ["input", "choice", "confirm"].includes(turn.block.type);
              const receipt = runtime.receipts.findLast(
                (item) => item.jobId === turn.jobId && item.blockId === turn.block.id,
              );
              return (
                <div className="flex flex-col gap-3" data-conversation-block={turn.block.id}>
                  {(question || !live) && <AgentBlockView block={turn.block} ctx={history} />}
                  {live && request.channel !== "dialogue" && (
                    <div data-chat-input="">
                      <AgentBlockView block={turn.block} ctx={active} />
                    </div>
                  )}
                  {question && !live && receipt && (
                    <p className="type-caption text-fg-muted">
                      {receipt.status === "superseded"
                        ? "Вопрос закрыт"
                        : receipt.status === "rejected"
                          ? "Ответ не принят"
                          : "Ответ отправлен"}
                    </p>
                  )}
                </div>
              );
            })()
          )}
        </li>
      ))}
      {failed && (
        <li data-connection-failure="" className="flex flex-col items-start gap-3">
          <ChatMessage
            role="agent"
            markdown={
              problem?.code === "login_failed" && !problem.detail
                ? "Вход не завершён. Можно войти заново."
                : problem
                  ? humanMessage(new ApiError(500, problem.code, problem.detail, problem.extra))
                  : "Подключение не завершено."
            }
          />
          {source && connectionId && (
            <Button variant="secondary" loading={restarting} disabled={restarting} onClick={onRestart}>
              Войти заново
            </Button>
          )}
        </li>
      )}
      {source && job?.state === "cancelled" && (
        <li>
          <ChatMessage role="agent" markdown="Подключение отменено. Уже найденные данные сохранены." />
        </li>
      )}
    </ol>
  );
}

function ConnectThinking({
  turn,
  nowMs,
}: {
  turn: Extract<ConnectTurn, { kind: "thinking" }>;
  nowMs: number;
}) {
  const [collapsed, setCollapsed] = useState(true);
  return (
    <ThinkingStream
      steps={turn.steps}
      nowMs={nowMs}
      finishedAt={turn.finishedAt}
      collapsed={collapsed}
      onToggle={setCollapsed}
    />
  );
}
