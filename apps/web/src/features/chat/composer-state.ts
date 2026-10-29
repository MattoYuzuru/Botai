import type { PendingRef, ThreadRuntime } from "@/api/v3";
import { isTerminal } from "@/features/jobs/job-state";
import type { ThreadJob } from "./timeline";

export interface ComposerState {
  disabled: boolean;
  placeholder: string;
  /** Агенту нужен человек: вопрос в блоке или в диалоге. */
  waitingForHuman: boolean;
  /** Идёт ход агента: следующее сообщение дождётся ответа. */
  replying: boolean;
  /** Сообщение отвечает на вопрос задачи, которую человек видит: сервер проверит, что вопрос ещё открыт. */
  targetJobId?: string;
}

export interface ComposerStateInput {
  /** `null` — тред ещё не создан: первое сообщение создаст его. */
  threadId: string | null;
  runtime: ThreadRuntime | undefined;
  runtimeLoading: boolean;
  runtimeFailed: boolean;
  jobs: readonly ThreadJob[];
  /** Вопрос свежий, а задание неизвестно: ответ, скорее всего, в пути. */
  awaiting: boolean;
}

/** Вопрос, на который нельзя ответить текстом в поле ввода: секрет, нативный блок, согласие или браузер. */
export const isProtected = (item: PendingRef): boolean => item.channel !== "dialogue" || item.secret;

export function composerState(input: ComposerStateInput): ComposerState {
  const { threadId, runtime, jobs, awaiting } = input;
  const base = "Спросите про пары и сроки";
  if (threadId === null) {
    return { disabled: false, placeholder: base, waitingForHuman: false, replying: false };
  }
  if (input.runtimeLoading || input.runtimeFailed || !runtime) {
    return { disabled: true, placeholder: base, waitingForHuman: false, replying: false };
  }
  const protectedRequest = runtime.pending.find(isProtected);
  if (protectedRequest) {
    return {
      disabled: true,
      placeholder:
        protectedRequest.channel === "manual" ? "Завершите вход в браузере выше" : "Ответьте на вопрос выше",
      waitingForHuman: true,
      replying: false,
    };
  }
  const replying = awaiting || jobs.some((job) => job.kind === "chat" && !isTerminal(job.snapshot.state));
  if (replying) {
    return { disabled: true, placeholder: "Агент отвечает…", waitingForHuman: false, replying: true };
  }
  const dialogue = runtime.pending.find((item) => item.channel === "dialogue" && !item.secret);
  if (dialogue) {
    return {
      disabled: false,
      placeholder: "Ответьте агенту или уточните задачу",
      waitingForHuman: true,
      replying: false,
      targetJobId: dialogue.jobId,
    };
  }
  const tasks = jobs.some((job) => job.kind !== "chat" && !isTerminal(job.snapshot.state));
  return {
    disabled: false,
    placeholder: tasks ? "Напишите агенту: он ответит и продолжит работу" : base,
    waitingForHuman: false,
    replying: false,
  };
}
