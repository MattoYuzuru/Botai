import { Button, Dialog, TaskCard, type TaskState } from "@botai/design";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
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
  const [cancelling, setCancelling] = useState(false);
  const here = ctx.threadId !== undefined && block.threadId === ctx.threadId;
  const state = STATES[ctx.taskState?.(block.jobId) ?? block.state] ?? "running";
  const active = state === "running" || state === "waiting_user" || state === "queued";
  const connect = block.kind === "connect";
  return (
    <>
      <TaskCard
        title={block.title}
        kind={block.kind}
        state={state}
        here={here}
        action={
          !here ? (
            <Button variant="secondary" size="sm" data-touch-expanded="" asChild>
              <Link to="/chat" search={{ thread: block.threadId }}>
                Открыть чат
              </Link>
            </Button>
          ) : active && ctx.cancelTask ? (
            <Button variant="ghost" size="sm" data-touch-expanded="" onClick={() => setCancelling(true)}>
              {connect ? "Отменить подключение" : "Отменить задачу"}
            </Button>
          ) : undefined
        }
      />
      <Dialog
        open={cancelling}
        onOpenChange={setCancelling}
        title={connect ? "Отменить подключение?" : "Отменить задачу?"}
        description="Агент остановится. Уже найденные данные сохранятся."
        confirmLabel={connect ? "Отменить подключение" : "Отменить задачу"}
        cancelLabel="Продолжить"
        onConfirm={() => ctx.cancelTask?.(block.jobId)}
      />
    </>
  );
}
