import { Button, TaskCard, type TaskState } from "@botai/design";
import { Link } from "@tanstack/react-router";
import type { BlockContext } from "./context";
import type { TaskBlock } from "./types";

const STATES: Record<string, TaskState> = {
  queued: "queued",
  running: "running",
  waiting_user: "waiting_user",
  done: "done",
  failed: "failed",
  cancelled: "cancelled",
};

/**
 * `task`: карточка фоновой задачи (§3.3). Задача этого треда показывает состояние, задача из другого треда
 * ведёт туда ссылкой. Состояние берётся из серверного состояния треда, если оно известно: блок хранит
 * только то, что было при его создании.
 */
export function TaskBlockView({ block, ctx }: { block: TaskBlock; ctx: BlockContext }) {
  const here = ctx.threadId !== undefined && block.threadId === ctx.threadId;
  const state = STATES[ctx.taskState?.(block.jobId) ?? block.state] ?? "running";
  return (
    <TaskCard
      title={block.title}
      kind={block.kind}
      state={state}
      here={here}
      action={
        here ? undefined : (
          <Button variant="secondary" size="sm" data-touch-expanded="" asChild>
            <Link to="/chat" search={{ thread: block.threadId }}>
              Открыть чат
            </Link>
          </Button>
        )
      }
    />
  );
}
