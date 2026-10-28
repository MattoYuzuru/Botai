import { ChatMessage, ThinkingStream } from "@botai/design";
import { useRouter } from "@tanstack/react-router";
import { useCallback, useMemo, useState } from "react";
import { AgentBlockView } from "@/features/agent-blocks/agent-block";
import type { BlockContext } from "@/features/agent-blocks/context";
import { type JobManager, jobManager } from "@/features/jobs/job-manager";
import type { JobSnapshot } from "@/features/jobs/job-state";
import { textConfidence } from "./confidence";
import { internalHref, internalIcon, internalLabel, parseInternalLink } from "./internal-links";
import type { TimelineItem } from "./timeline";

/** Внутренняя ссылка источника открывается в приложении (`/study/documents`, `/study/connections`), всё остальное — как обычно. */
export function useInternalLink(): NonNullable<BlockContext["internalLink"]> {
  const router = useRouter();
  return useCallback(
    (url: string) => {
      const link = parseInternalLink(url);
      if (!link) return null;
      const href = internalHref(link);
      return {
        href,
        label: internalLabel(link),
        icon: internalIcon(link),
        onOpen: () => router.history.push(href),
      };
    },
    [router],
  );
}

const noop = () => {};

/** Контекст блока для сообщения из истории: ответов на вопросы там уже нет, действий — тоже. */
function historyContext(base: Pick<BlockContext, "nowMs" | "timeZone" | "internalLink">): BlockContext {
  return {
    jobId: "",
    readOnly: true,
    answers: {},
    nowMs: base.nowMs,
    timeZone: base.timeZone,
    onAnswer: noop,
    onApprove: noop,
    onReject: noop,
    confidence: textConfidence,
    internalLink: base.internalLink,
  };
}

interface TimelineViewProps {
  items: readonly TimelineItem[];
  /** Задание, поток которого идёт сейчас; даёт ответы на вопросы агента и действия. */
  job: JobSnapshot | null;
  nowMs: number;
  timeZone: string;
  manager?: JobManager;
}

/**
 * Лента сообщений: вопросы человека, ответы агента из истории и живой ответ (шаги, вопросы агента, текст).
 * Блоки рисует общий `AgentBlockView` — тот же, что на экране подключения вуза.
 */
export function TimelineView({ items, job, nowMs, timeZone, manager = jobManager }: TimelineViewProps) {
  const internalLink = useInternalLink();
  const history = useMemo(
    () => historyContext({ nowMs, timeZone, internalLink }),
    [nowMs, timeZone, internalLink],
  );
  return (
    <ol className="flex flex-col gap-4" aria-label="Сообщения" data-timeline="">
      {items.map((item) => (
        <li key={item.key}>
          {item.kind === "user" && (
            <ChatMessage
              role="user"
              markdown={item.text}
              at={item.at}
              timeZone={timeZone}
              status={item.status}
            />
          )}
          {item.kind === "agent" && (
            <div className="flex flex-col gap-3">
              {item.blocks.map((block) => (
                <AgentBlockView key={block.id} block={block} ctx={history} />
              ))}
            </div>
          )}
          {item.kind === "live" && (
            <LiveReply
              item={item}
              job={job}
              nowMs={nowMs}
              timeZone={timeZone}
              manager={manager}
              internalLink={internalLink}
            />
          )}
        </li>
      ))}
    </ol>
  );
}

function LiveReply({
  item,
  job,
  nowMs,
  timeZone,
  manager,
  internalLink,
}: {
  item: Extract<TimelineItem, { kind: "live" }>;
  job: JobSnapshot | null;
  nowMs: number;
  timeZone: string;
  manager: JobManager;
  internalLink: NonNullable<BlockContext["internalLink"]>;
}) {
  const [collapsed, setCollapsed] = useState(true);
  const jobId = item.jobId ?? "";
  const ctx: BlockContext = useMemo(
    () => ({
      jobId,
      answers: job?.answers ?? {},
      nowMs,
      timeZone,
      onAnswer: (blockId, values, shown) => void manager.answer(jobId, blockId, values, shown),
      onApprove: (blockId, proposalId, fields) => void manager.approve(jobId, blockId, proposalId, fields),
      onReject: (blockId, proposalId) => void manager.reject(jobId, blockId, proposalId),
      confidence: (markdown) => (item.unsure ? "unsure" : textConfidence(markdown)),
      internalLink,
    }),
    [jobId, job?.answers, nowMs, timeZone, manager, item.unsure, internalLink],
  );
  return (
    <div className="flex flex-col gap-3" data-live-reply="" aria-busy={item.running || undefined}>
      {/* Объявляется только итог: шаги и секунды скринридер не читает. */}
      <span role="status" aria-live="polite" className="sr-only">
        {item.running ? "" : item.failure ? "Не удалось получить ответ" : "Ответ получен"}
      </span>
      {item.steps.length > 0 && (
        <ThinkingStream
          steps={item.steps}
          nowMs={nowMs}
          finishedAt={item.finishedAt}
          collapsed={collapsed}
          onToggle={setCollapsed}
        />
      )}
      {item.blocks.map((block) => (
        <AgentBlockView key={block.id} block={block} ctx={ctx} />
      ))}
      {item.failure && <ChatMessage role="agent" confidence="unsure" markdown={item.failure} />}
    </div>
  );
}
