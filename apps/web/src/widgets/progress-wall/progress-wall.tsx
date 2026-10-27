import {
  Badge,
  Banner,
  Button,
  Dialog,
  IconButton,
  ProgressRail,
  SyncIndicator,
  ThinkingStream,
  useNow,
} from "@botai/design";
import { type ReactNode, useEffect, useState } from "react";
import { ApiError, humanMessage } from "@/api/errors";
import { AgentBlockView } from "@/features/agent-blocks/agent-block";
import type { BlockContext } from "@/features/agent-blocks/context";
import { jobManager as defaultManager, type JobManager, useJob } from "@/features/jobs/job-manager";
import type { JobProblem } from "@/features/jobs/job-state";
import {
  activeAttention,
  isTerminal,
  needsHuman,
  pendingQuestions,
  thinkingSteps,
} from "@/features/jobs/job-state";

export interface ProgressWallProps {
  jobId: string;
  /** «Подключаю ВШЭ». */
  title: string;
  spaceTitle?: string;
  timeZone?: string;
  /** Свернуть: работа продолжается в фоне, её состояние видно в шапке экранов. */
  onMinimize: () => void;
  /** «Готово» после итога. */
  onFinish: () => void;
  /** Отменить задание (после подтверждения). */
  onCancel?: () => void;
  /** Открыть чат с вопросом по сбою. Без обработчика кнопки нет: виджет не знает про маршруты. */
  onAskInChat?: () => void;
  /** Уровень заголовка с названием: на своём экране 1, в витрине ниже по иерархии. Заголовок стадии — на уровень глубже. */
  headingLevel?: 1 | 3;
  /** Менеджер потоков; по умолчанию общий. Витрина и тесты подставляют свой. */
  manager?: JobManager;
  /**
   * Варя на экране (mascot.md §3). `rail` — бежит по рельсу, пока работа идёт или ждёт человека; `header` —
   * после итога стоит рядом с названием. Без функции экран выглядит как раньше: картинка ничего не несёт,
   * стадия, время и вопросы остаются текстом.
   */
  renderMascot?: (state: WallMascotState, slot: "rail" | "header") => ReactNode;
}

export type WallMascotState = "running" | "waiting" | "done" | "failed";

/** Что показать человеку при сбое задания — по коду ошибки, без технических слов (styleguide §9). */
export function describeProblem(problem: JobProblem): { text: string; chat: boolean } {
  switch (problem.code) {
    case "egress_blocked": {
      const host = typeof problem.extra.host === "string" ? problem.extra.host : "";
      return {
        text: `Вуз обращается к сайту${host ? ` ${host}` : ""}, которого нет в списке разрешённых для этого подключения. Я туда не пойду, пока вы не подтвердите это в чате.`,
        chat: true,
      };
    }
    case "source_auth_required":
      return {
        text: "Вуз не принял вход. Подключение можно начать заново и ввести данные ещё раз.",
        chat: false,
      };
    case "quota_exceeded":
      return {
        text: "На сегодня лимит ответов агента исчерпан. Он обновится завтра, подключение продолжится тогда.",
        chat: false,
      };
    case "injection_blocked":
      return {
        text: `Я отклонил инструкцию из страницы вуза: ${typeof problem.extra.rejected === "string" ? problem.extra.rejected : "текст со страницы не считается командой"}.`,
        chat: true,
      };
    case "login_failed":
    case "source_bot_protection":
      return {
        text:
          problem.detail || "Вход потребовал проверки, которую делает человек. Попробуйте снова чуть позже.",
        chat: true,
      };
    case "not_found":
      return { text: problem.detail || "Задание не найдено. Возможно, оно уже завершилось.", chat: false };
    case "not_implemented":
      return {
        text: "Эта часть серверной работы пока не готова. Подключение вуза появится позже.",
        chat: false,
      };
    default:
      return { text: problem.detail || humanMessage(new ApiError(500, problem.code, "")), chat: true };
  }
}

/**
 * Экран долгой работы агента (mascot.md §3): стадия словами сервера, настоящее прошедшее время, честное «сколько
 * осталось — пока неизвестно», развёрнутый поток рассуждения, вопросы поверх экрана и итог, который нужно прочитать.
 * Ни процентов, ни придуманных оценок. Закрывается только по итогу или по действию человека.
 */
export function ProgressWall({
  jobId,
  title,
  spaceTitle,
  timeZone = "Europe/Moscow",
  onMinimize,
  onFinish,
  onCancel,
  onAskInChat,
  headingLevel = 1,
  manager = defaultManager,
  renderMascot,
}: ProgressWallProps) {
  const job = useJob(jobId, manager);
  const nowMs = useNow(1000);
  const Title = headingLevel === 1 ? "h1" : "h3";
  const StageTitle = headingLevel === 1 ? "h2" : "h4";
  const [cancelOpen, setCancelOpen] = useState(false);
  const [streamOpen, setStreamOpen] = useState(true);
  const [frozenAt, setFrozenAt] = useState<number | null>(null);

  useEffect(() => {
    manager.start(jobId);
  }, [jobId, manager]);

  const terminal = job ? isTerminal(job.state) : false;
  // Время работы останавливается в момент итога: «Идёт 1 мин 12 с» после завершения не растёт.
  useEffect(() => {
    if (terminal && frozenAt === null) setFrozenAt(Date.now());
  }, [terminal, frozenAt]);

  if (!job) return null;

  const steps = thinkingSteps(job);
  const doneSteps = steps.filter((step) => step.state === "done").length;
  const pending = pendingQuestions(job);
  const startedAt = job.startedAt ?? steps[0]?.at ?? nowMs;
  const elapsed = Math.max(0, (frozenAt ?? nowMs) - startedAt);
  const stage =
    job.stage ??
    steps.findLast((step) => step.state === "running" || step.state === "waiting")?.label ??
    "Готовлюсь";
  const summary = job.blocks.find((block) => block.type === "summary");

  const ctx: BlockContext = {
    jobId,
    answers: job.answers,
    nowMs,
    timeZone,
    spaceTitle,
    onAnswer: (blockId, values, shown) => void manager.answer(jobId, blockId, values, shown),
    onApprove: (blockId, proposalId, fields) => void manager.approve(jobId, blockId, proposalId, fields),
    onReject: (blockId, proposalId) => void manager.reject(jobId, blockId, proposalId),
  };

  const pendingIds = new Set(pending.map((block) => block.id));
  // Поверх экрана — то, что ждёт человека: вопросы, ожидание подтверждения, запасной экран входа.
  const attentionIds = new Set(activeAttention(job).map((block) => block.id));
  const elevated = job.blocks.filter((block) => pendingIds.has(block.id) || attentionIds.has(block.id));
  const elevatedIds = new Set(elevated.map((block) => block.id));
  // В истории только вопросы и ответы: закрытое ожидание и пройденный экран входа уже ничего не просят.
  const history = job.blocks.filter(
    (block) =>
      block.type !== "thinking" &&
      block.type !== "summary" &&
      block.type !== "wait" &&
      block.type !== "login" &&
      !elevatedIds.has(block.id),
  );
  const attention = needsHuman(job);
  const problem = job.problem ? describeProblem(job.problem) : null;
  const mascotState: WallMascotState = terminal
    ? job.state === "done"
      ? "done"
      : "failed"
    : attention
      ? "waiting"
      : "running";

  return (
    <div className="mx-auto flex w-full max-w-col flex-col gap-6" data-progress-wall={job.state}>
      <header className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <Title className="type-title text-fg">{title}</Title>
          {spaceTitle && <p className="type-caption text-fg-muted">{spaceTitle}</p>}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {terminal && renderMascot?.(mascotState, "header")}
          {attention && <Badge tone="warning">Нужен ответ</Badge>}
          {!terminal && (
            <IconButton icon="chevron-down" label="Свернуть" variant="secondary" onClick={onMinimize} />
          )}
        </div>
      </header>

      {!terminal && (
        <section aria-label="Ход работы" className="flex flex-col gap-3">
          <ProgressRail
            stage={doneSteps}
            stageCount={null}
            elapsedMs={elapsed}
            etaKnown={false}
            runner={renderMascot?.(mascotState, "rail")}
          />
          <StageTitle className="type-heading text-fg">{stage}</StageTitle>
          {job.link === "reconnecting" && (
            <p className="flex items-center gap-2 type-caption text-fg-muted">
              <SyncIndicator state="syncing" /> Связь с агентом восстанавливается, данные не потеряны.
            </p>
          )}
        </section>
      )}

      {/* Итоги и вопросы объявляются один раз: не секунды и не стадии. */}
      <p role="status" aria-live="polite" className="sr-only">
        {attention
          ? "Агенту нужен ваш ответ"
          : terminal
            ? job.state === "done"
              ? "Подключение завершено"
              : "Подключение не завершено"
            : ""}
      </p>

      {elevated.length > 0 && (
        <section aria-label="Нужно ваше участие" className="flex flex-col gap-3">
          {elevated.map((block) => (
            <AgentBlockView key={block.id} block={block} ctx={ctx} />
          ))}
        </section>
      )}

      {steps.length > 0 && (
        <ThinkingStream
          steps={steps}
          nowMs={nowMs}
          finishedAt={terminal ? (frozenAt ?? undefined) : undefined}
          collapsed={!streamOpen}
          onToggle={(collapsed) => setStreamOpen(!collapsed)}
        />
      )}

      {history.length > 0 && (
        <section aria-label="История" className="flex flex-col gap-3">
          {history.map((block) => (
            <AgentBlockView key={block.id} block={block} ctx={ctx} />
          ))}
        </section>
      )}

      {job.state === "done" && summary && <AgentBlockView block={summary} ctx={ctx} />}

      {problem && (
        <Banner
          tone="danger"
          icon="error"
          live
          action={
            problem.chat && onAskInChat ? (
              <Button variant="secondary" size="sm" data-touch-expanded="" onClick={onAskInChat}>
                Спросить в чате
              </Button>
            ) : undefined
          }
        >
          <p className="type-ui">Подключение не завершено.</p>
          <p className="type-ui">{problem.text}</p>
        </Banner>
      )}

      {job.state === "cancelled" && (
        <Banner tone="neutral" icon="close" live>
          Подключение отменено. То, что успело сохраниться, останется.
        </Banner>
      )}

      <div className="flex flex-col gap-2 pb-safe-4 xs:flex-row xs:items-center">
        {terminal ? (
          <Button variant="primary" onClick={onFinish} className="xs:min-w-40">
            Готово
          </Button>
        ) : (
          onCancel && (
            <Button variant="ghost" tone="danger" onClick={() => setCancelOpen(true)}>
              Отменить подключение
            </Button>
          )
        )}
      </div>

      <Dialog
        open={cancelOpen}
        onOpenChange={setCancelOpen}
        title="Отменить подключение?"
        description="Агент остановится. Вход в вуз придётся начать заново, а уже найденное сохранится."
        confirmLabel="Отменить подключение"
        cancelLabel="Продолжить"
        onConfirm={() => onCancel?.()}
      />
    </div>
  );
}
