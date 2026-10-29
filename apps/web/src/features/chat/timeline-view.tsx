import { Button, ChatMessage, QuickReplies, ThinkingStream, type ViewerAttachment } from "@botai/design";
import { useMemo, useState } from "react";
import { api, call } from "@/api/client";
import { attachmentContentUrl, type PendingRef, type ThreadRuntime } from "@/api/v3";
import { AgentBlockView } from "@/features/agent-blocks/agent-block";
import { viewerFromBlock } from "@/features/agent-blocks/attachments";
import type { BlockContext } from "@/features/agent-blocks/context";
import type { ParsedBlock } from "@/features/jobs/blocks";
import { jobManager } from "@/features/jobs/job-manager";
import { isTerminal } from "@/features/jobs/job-state";
import { useManualLogin } from "@/widgets/progress-wall/use-manual-login";
import type { ChatLinks } from "./chat-links";
import { resultConfidence, textConfidence } from "./confidence";
import type { ThreadJob, Timeline, Turn } from "./timeline";

const noop = () => {};

/** Открытый вопрос задания, на который отвечают блоком (не текстом в поле ввода). */
export interface PendingEditorRef {
  key: string;
  request: PendingRef;
  job: ThreadJob;
  block: ParsedBlock;
}

/**
 * Вопросы, которые ждут ответа блоком: секрет, нативные поля, согласие, ожидание и вход в браузере. Текстовые
 * вопросы в диалоге (`dialogue`) отвечаются в поле ввода и здесь не нужны. Вопрос показывается, когда поток
 * задания уже прислал сам блок: без него рисовать нечего.
 */
export function pendingEditors(
  runtime: ThreadRuntime | undefined,
  jobs: readonly ThreadJob[],
): PendingEditorRef[] {
  if (!runtime) return [];
  const byId = new Map(jobs.map((job) => [job.id, job]));
  const editors: PendingEditorRef[] = [];
  for (const request of runtime.pending) {
    if (request.channel === "dialogue" && !request.secret) continue;
    const job = byId.get(request.jobId);
    if (!job || isTerminal(job.snapshot.state)) continue;
    const block = job.snapshot.blocks.find((item) => item.id === request.blockId);
    if (block) editors.push({ key: `block:${job.id}:${block.id}`, request, job, block });
  }
  return editors;
}

interface TimelineViewProps {
  timeline: Timeline;
  jobs: readonly ThreadJob[];
  runtime: ThreadRuntime | undefined;
  threadId: string | null;
  nowMs: number;
  timeZone: string;
  links: ChatLinks;
  onQuickReply: (text: string) => void;
  quickRepliesDisabled: boolean;
  /** «Войти заново» у источника, вход в который не завершился. */
  onRestart: (connectionId: string) => void;
  restarting: boolean;
  /** Ответ на вопрос или решение по действию принято: состояние треда нужно обновить. */
  onChanged: () => void;
}

const viewerFromAttachment = (item: {
  id?: string;
  attachmentId?: string;
  name: string;
  mime: string;
  size: number;
  kind: ViewerAttachment["kind"];
}): ViewerAttachment => ({
  id: item.attachmentId ?? item.id ?? item.name,
  name: item.name,
  mime: item.mime,
  size: item.size,
  kind: item.kind,
  url: attachmentContentUrl(item.attachmentId ?? item.id ?? ""),
});

/**
 * Лента одного треда: вопросы человека, ответы агента, шаги размышления, карточки задач и сбои — в серверном
 * порядке. Открытые вопросы со вводом стоят у поля ввода (`PendingQuestions`), а здесь остаются записью в истории.
 */
export function TimelineView({
  timeline,
  jobs,
  runtime,
  threadId,
  nowMs,
  timeZone,
  links,
  onQuickReply,
  quickRepliesDisabled,
  onRestart,
  restarting,
  onChanged,
}: TimelineViewProps) {
  const editors = useMemo(() => pendingEditors(runtime, jobs), [runtime, jobs]);
  const open = useMemo(() => new Set(editors.map((editor) => editor.key)), [editors]);
  const byId = useMemo(() => new Map(jobs.map((job) => [job.id, job])), [jobs]);
  const taskStates = useMemo(() => {
    const states = new Map<string, string>();
    for (const task of runtime?.tasks ?? []) states.set(task.id, task.state);
    if (runtime?.activeJob) states.set(runtime.activeJob.id, runtime.activeJob.state);
    for (const job of jobs) states.set(job.id, job.snapshot.state);
    return states;
  }, [runtime, jobs]);

  const base = useMemo(
    () => ({
      nowMs,
      timeZone,
      threadId: threadId ?? undefined,
      resolveLink: links.resolveLink,
      groupSources: links.groupSources,
      openAttachment: links.openAttachment,
      taskState: (jobId: string) => taskStates.get(jobId),
    }),
    [nowMs, timeZone, threadId, links, taskStates],
  );

  const receipts = runtime?.receipts ?? [];
  const visible = timeline.turns.filter((turn) => !(turn.kind === "block" && open.has(turn.key)));
  return (
    <>
      <ol className="flex flex-col gap-5" aria-label="Сообщения" data-timeline="">
        {visible.map((turn) => (
          <li key={turn.key} data-turn={turn.kind} data-conversation-turn={turn.key}>
            <TurnView
              turn={turn}
              base={base}
              job={turn.kind === "block" || turn.kind === "thinking" ? byId.get(turn.jobId ?? "") : undefined}
              receipt={
                turn.kind === "block"
                  ? receipts.findLast((item) => item.jobId === turn.jobId && item.blockId === turn.block.id)
                  : undefined
              }
              nowMs={nowMs}
              timeZone={timeZone}
              onRestart={onRestart}
              restarting={restarting}
              failedJob={turn.kind === "failure" ? byId.get(turn.jobId) : undefined}
            />
            {timeline.quickReplies?.key === turn.key && (
              <div className="pt-3">
                <QuickReplies
                  options={timeline.quickReplies.options}
                  onPick={onQuickReply}
                  disabled={quickRepliesDisabled}
                />
              </div>
            )}
          </li>
        ))}
      </ol>
      {editors.length > 0 && (
        <div className="flex flex-col gap-4 pt-5" data-pending-questions="">
          {editors.map((editor) => (
            <PendingEditor key={editor.key} editor={editor} base={base} onChanged={onChanged} />
          ))}
        </div>
      )}
    </>
  );
}

type BaseContext = Pick<
  BlockContext,
  "nowMs" | "timeZone" | "threadId" | "resolveLink" | "groupSources" | "openAttachment" | "taskState"
>;

function TurnView({
  turn,
  base,
  job,
  failedJob,
  receipt,
  nowMs,
  timeZone,
  onRestart,
  restarting,
}: {
  turn: Turn;
  base: BaseContext;
  job: ThreadJob | undefined;
  failedJob: ThreadJob | undefined;
  receipt: ThreadRuntime["receipts"][number] | undefined;
  nowMs: number;
  timeZone: string;
  onRestart: (connectionId: string) => void;
  restarting: boolean;
}) {
  switch (turn.kind) {
    case "user":
      return (
        <ChatMessage
          role="user"
          markdown={turn.text || undefined}
          attachments={turn.attachments.map((item) =>
            "attachmentId" in item ? viewerFromBlock(item) : viewerFromAttachment(item),
          )}
          onOpenAttachment={base.openAttachment}
          at={turn.at}
          nowMs={nowMs}
          timeZone={timeZone}
          status={turn.status}
          resolveLink={base.resolveLink}
        />
      );
    case "thinking":
      return <ThinkingTurn turn={turn} nowMs={nowMs} />;
    case "failure":
      return (
        <div className="flex flex-col items-start gap-3" data-failure="">
          <ChatMessage
            role="agent"
            className="w-full min-w-0"
            confidence={failedJob?.kind === "chat" ? "unsure" : "ok"}
            markdown={turn.text}
          />
          {turn.connectionId && (
            <Button
              variant="secondary"
              loading={restarting}
              disabled={restarting}
              onClick={() => onRestart(turn.connectionId as string)}
              className="h-auto min-h-11 max-w-full whitespace-normal [overflow-wrap:anywhere] [&>span]:min-w-0"
            >
              Войти заново
            </Button>
          )}
        </div>
      );
    case "block": {
      // Итог подключения, которое не завершилось, не выдаётся за результат.
      if (turn.block.type === "summary" && job && job.snapshot.state !== "done") return null;
      const answers = job?.snapshot.answers ?? {};
      const unsure =
        job !== undefined &&
        job.snapshot.state === "done" &&
        resultConfidence(job.snapshot.result) === "unsure";
      const ctx: BlockContext = {
        ...base,
        jobId: turn.jobId ?? "",
        answers,
        readOnly: true,
        onAnswer: noop,
        onApprove: noop,
        onReject: noop,
        confidence: (markdown) => (unsure ? "unsure" : textConfidence(markdown)),
      };
      return <HistoryBlock block={turn.block} ctx={ctx} receipt={receipt} />;
    }
  }
}

function HistoryBlock({
  block,
  ctx,
  receipt,
}: {
  block: ParsedBlock;
  ctx: BlockContext;
  receipt: ThreadRuntime["receipts"][number] | undefined;
}) {
  if (block.type === "input" || block.type === "choice" || block.type === "confirm") {
    const answer = ctx.answers[block.id];
    return (
      <div className="flex flex-col gap-3" data-conversation-block={block.id}>
        <ChatMessage role="agent" markdown={block.question} />
        {answer?.status === "answered" ? (
          <ChatMessage role="user" markdown={answer.shown ?? "Введено"} />
        ) : (
          receipt && (
            <p className="type-caption text-fg-muted">
              {receipt.status === "superseded"
                ? "Вопрос закрыт"
                : receipt.status === "rejected"
                  ? "Ответ не принят"
                  : "Ответ отправлен"}
            </p>
          )
        )}
      </div>
    );
  }
  return (
    <div data-conversation-block={block.id}>
      <AgentBlockView block={block} ctx={ctx} />
    </div>
  );
}

function ThinkingTurn({ turn, nowMs }: { turn: Extract<Turn, { kind: "thinking" }>; nowMs: number }) {
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

/** Ввод для открытого вопроса задания: тот же блок, что в ленте, но с формой; секреты идут через `jobs/answers`. */
function PendingEditor({
  editor,
  base,
  onChanged,
}: {
  editor: PendingEditorRef;
  base: BaseContext;
  onChanged: () => void;
}) {
  const { job, block } = editor;
  const connectionId = job.connectionId ?? undefined;
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
  const manual = useManualLogin(job.id, job.snapshot, connectionId, openBrowser);
  const ctx: BlockContext = {
    ...base,
    jobId: job.id,
    answers: job.snapshot.answers,
    onAnswer: async (id, values, shown) => {
      await jobManager.answer(job.id, id, values, shown, connectionId);
      onChanged();
    },
    onApprove: async (id, proposal, fields) => {
      await jobManager.approve(job.id, id, proposal, fields);
      onChanged();
    },
    onReject: async (id, proposal) => {
      await jobManager.reject(job.id, id, proposal);
      onChanged();
    },
    manualLogin: connectionId && openBrowser ? { ...manual, connectionId } : undefined,
  };
  return (
    <div data-chat-input="" data-pending-question={block.id}>
      <AgentBlockView block={block} ctx={ctx} />
    </div>
  );
}
