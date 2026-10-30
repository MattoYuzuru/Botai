import {
  ApprovalCard,
  type ApprovalState,
  Badge,
  Button,
  Card,
  ChatMessage,
  Icon,
  SourceCitation,
} from "@botai/design";
import type { BlockContext } from "./context";
import { QuestionFrame } from "./question-frame";
import type { Approval, Confirm, Summary, Unknown } from "./types";

/** `confirm`: две кнопки с подписями из блока — без переименования («Подтвердить» / «Отмена» придумывает агент). */
export function ConfirmCard({ block, ctx }: { block: Confirm; ctx: BlockContext }) {
  const answer = ctx.answers[block.id];
  const busy = answer?.status === "sending";
  return (
    <QuestionFrame
      question={block.question}
      details={block.details}
      required
      answer={answer}
      editorOnly={ctx.editorOnly}
    >
      <div className="flex flex-col gap-2 xs:flex-row xs:flex-wrap">
        <Button
          variant="primary"
          loading={busy}
          onClick={() => ctx.onAnswer(block.id, true, block.confirmLabel)}
        >
          {block.confirmLabel}
        </Button>
        <Button
          variant="secondary"
          disabled={busy}
          onClick={() => ctx.onAnswer(block.id, false, block.cancelLabel)}
        >
          {block.cancelLabel}
        </Button>
      </div>
    </QuestionFrame>
  );
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

/** `approval`: единственный способ получить согласие на запись; подтверждение одноразовое и истекает. */
export function ApprovalBlock({ block, ctx }: { block: Approval; ctx: BlockContext }) {
  const answer = ctx.answers[block.id];
  const { proposal } = block;
  const state: ApprovalState =
    answer?.status === "expired"
      ? "expired"
      : answer?.status === "sending"
        ? "pending"
        : answer?.status === "answered"
          ? (answer.outcome ?? "approved")
          : "default";
  return (
    <div className="flex flex-col gap-2">
      <ApprovalCard
        proposal={{
          id: proposal.id,
          intent: proposal.intent,
          method: proposal.request.method,
          host: hostOf(proposal.request.url),
          fields: proposal.request.fields,
          reversible: proposal.reversible,
          expiresAt: Date.parse(proposal.expiresAt),
          spaceTitle: ctx.spaceTitle,
        }}
        state={state}
        nowMs={ctx.nowMs}
        timeZone={ctx.timeZone}
        onApprove={(fields) => ctx.onApprove(block.id, proposal.id, fields)}
        onReject={() => ctx.onReject(block.id, proposal.id)}
      />
      {answer?.status === "failed" && answer.error && (
        <p role="alert" className="type-caption text-danger-fg">
          {answer.error}
        </p>
      )}
    </div>
  );
}

/** `summary`: итог подключения или ответа — группы строк «подпись → значение» с источником и уверенностью. */
export function SummaryCard({ block, ctx }: { block: Summary; ctx: BlockContext }) {
  return (
    <Card className="flex flex-col gap-4" aria-labelledby={`summary-${block.id}`}>
      <h3 id={`summary-${block.id}`} className="type-heading text-fg">
        {block.title}
      </h3>
      {block.groups.map((group) => (
        <section key={group.title} className="flex flex-col gap-2">
          <h4 className="type-label text-fg-muted">{group.title}</h4>
          {group.items.length === 0 ? (
            <p className="type-ui text-fg-muted">Ничего не нашлось.</p>
          ) : (
            <dl className="flex flex-col divide-y divide-border">
              {group.items.map((item) => (
                <div
                  key={`${item.label}-${item.value}`}
                  className="flex flex-col gap-1 py-2 first:pt-0 last:pb-0"
                >
                  <dt className="type-caption text-fg-muted">{item.label}</dt>
                  <dd className="flex flex-wrap items-center gap-2 type-body text-fg">
                    <span className="min-w-0 break-words">{item.value}</span>
                    {item.confidence === "guess" && (
                      <Badge tone="warning" icon="help">
                        Предположение
                      </Badge>
                    )}
                  </dd>
                  {item.sourceUrl && (
                    <SourceCitation
                      title="Источник"
                      url={item.sourceUrl}
                      fetchedAt={Date.parse(block.at)}
                      nowMs={ctx.nowMs}
                      timeZone={ctx.timeZone}
                    />
                  )}
                </div>
              ))}
            </dl>
          )}
        </section>
      ))}
    </Card>
  );
}

/** Неизвестный тип блока — текстом с пометкой, а не молчанием и не разметкой (ADR-0004). */
export function UnknownBlockView({ block }: { block: Unknown }) {
  return (
    <div className="flex flex-col gap-2" data-unknown-block={block.rawType}>
      <Badge tone="warning" icon="warning" className="self-start">
        Блок неизвестного типа «{block.rawType}»
      </Badge>
      <ChatMessage
        role="agent"
        markdown={block.text ?? "Агент прислал блок, который это приложение пока не умеет показывать."}
      />
      <p className="flex items-center gap-1 type-caption text-fg-subtle">
        <Icon name="info" size={16} />
        Показан как текст. Обновите приложение, если такие блоки повторяются.
      </p>
    </div>
  );
}
