import {
  Badge,
  Button,
  Dialog,
  formatDay,
  formatTime,
  Icon,
  ListRowBody,
  listRowClass,
  Sheet,
  toast,
  useNow,
} from "@botai/design";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { type ActionDecision, type ActionJournalEntry, actionKeys, decideAction } from "@/api/actions";
import { humanMessage } from "@/api/errors";
import { DetailList, DetailRow } from "@/features/data/detail-row";
import {
  actionResultText,
  browserSteps,
  describeActionClass,
  describeActionMode,
  describeDecision,
  describeRequest,
  describeScopeKind,
  proposalFields,
  rollbackHint,
} from "./policy";

/**
 * Карточка записи журнала действий (docs/v4/19-actions.md §4): предложение с методом/адресом и
 * полями (значения уже замаскированы сервером), решение и режим, итог и ссылка на трассу хода.
 * Для `ask` здесь же решение: «Разрешить»/«Запретить». Оно только записывается в журнал —
 * запрос отправит ход агента, который его предложил (клиент ничего не шлёт на сайт).
 */
export function ActionSheet({
  action,
  spaceTitle,
  onClose,
}: {
  action: ActionJournalEntry | null;
  /** Название вуза, если известно из контекста (в журнале его нет). */
  spaceTitle?: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const nowMs = useNow(60_000);
  const [shown, setShown] = useState<ActionJournalEntry | null>(action);
  const [confirmDeny, setConfirmDeny] = useState(false);

  // Карточка открыта на конкретной записи: ответ сервера заменяет её, чужое действие не перетирает.
  const current = action && shown?.id === action.id ? shown : action;
  const decision = current ? describeDecision(current.decision) : null;
  const actionClass = current ? describeActionClass(current.actionClass) : null;
  const mode = current ? describeActionMode(current.mode) : null;
  const request = current ? describeRequest(current) : null;
  const fields = current ? proposalFields(current) : [];
  const steps = current ? browserSteps(current) : null;
  const result = current ? actionResultText(current) : null;
  const rollback = current ? rollbackHint(current) : null;

  const decide = useMutation({
    mutationFn: (value: ActionDecision) => {
      if (!current) throw new Error("нет действия");
      return decideAction(current.id, value);
    },
    onSuccess: (updated) => {
      setShown(updated);
      void queryClient.invalidateQueries({ queryKey: actionKeys.journalAll });
      toast(updated.decision === "approved" ? "Действие разрешено" : "Действие запрещено", {
        tone: "success",
        description:
          updated.decision === "approved"
            ? "Запись в журнале обновлена. Запрос отправит ход агента, который его предложил."
            : "Запись в журнале обновлена: действие не будет выполнено.",
      });
    },
    onError: (error) =>
      toast("Не удалось записать решение", { tone: "error", description: humanMessage(error) }),
  });

  return (
    <>
      <Sheet
        open={action !== null}
        onOpenChange={(open) => !open && onClose()}
        title={current?.intent ?? "Действие"}
        description={
          current
            ? `${actionClass?.label ?? "Действие"} · ${describeScopeKind(current.scopeKind)}`
            : undefined
        }
        size="half"
        footer={
          current && decision?.pending ? (
            <div className="flex flex-col gap-2">
              <p className="type-caption text-fg-subtle">
                Решение запишется в журнал, а действие выполнит ход агента.
              </p>
              <div className="flex gap-2">
                <Button
                  variant="primary"
                  fullWidth
                  loading={decide.isPending && decide.variables === "approve"}
                  onClick={() => decide.mutate("approve")}
                >
                  Разрешить
                </Button>
                <Button
                  variant="secondary"
                  fullWidth
                  loading={decide.isPending && decide.variables === "deny"}
                  onClick={() => setConfirmDeny(true)}
                >
                  Запретить
                </Button>
              </div>
            </div>
          ) : undefined
        }
      >
        {current && (
          <div className="flex flex-col gap-4 pb-2">
            <div className="flex flex-wrap gap-1.5">
              {decision && (
                <Badge tone={decision.tone} icon={decision.icon}>
                  {decision.label}
                </Badge>
              )}
              {actionClass && <Badge tone="neutral">{actionClass.label}</Badge>}
              {mode && <Badge tone="neutral">{mode.label}</Badge>}
            </div>

            <DetailList label="Действие">
              <DetailRow icon="chat" label="Что предлагает агент">
                {current.intent}
              </DetailRow>
              <DetailRow icon="settings" label="Класс и режим">
                {actionClass?.label ?? current.actionClass} · {mode?.label ?? current.mode}
              </DetailRow>
              <DetailRow icon="space" label="Область">
                {describeScopeKind(current.scopeKind)}
                {current.scopeKind !== "global" && current.scopeId
                  ? ` · ${current.scopeId}`
                  : spaceTitle
                    ? ` · ${spaceTitle}`
                    : ""}
              </DetailRow>
              <DetailRow icon="time" label="Предложено">
                {`${formatDay(Date.parse(current.createdAt), nowMs)}, ${formatTime(
                  Date.parse(current.createdAt),
                )}`}
              </DetailRow>
              {current.decidedAt && (
                <DetailRow icon="check" label="Решено">
                  {`${formatDay(Date.parse(current.decidedAt), nowMs)}, ${formatTime(
                    Date.parse(current.decidedAt),
                  )}`}
                </DetailRow>
              )}
              {current.executedAt && (
                <DetailRow icon="ok" label="Выполнено">
                  {`${formatDay(Date.parse(current.executedAt), nowMs)}, ${formatTime(
                    Date.parse(current.executedAt),
                  )}`}
                </DetailRow>
              )}
            </DetailList>

            {request && (request.method || request.url) && (
              <DetailList label="Запрос">
                <DetailRow icon="external" label="Метод и адрес">
                  <span className="break-all type-mono">
                    {[request.method, request.url].filter(Boolean).join(" ")}
                  </span>
                </DetailRow>
                {request.bodyKind && (
                  <DetailRow icon="document" label="Тело">
                    {request.bodyKind}
                    {request.bodyBytes !== null ? ` · ${request.bodyBytes} Б` : ""}
                  </DetailRow>
                )}
                {request.detail && (
                  <DetailRow icon="lock" label="Отпечаток тела">
                    <span className="break-all type-mono">{request.detail}</span>
                  </DetailRow>
                )}
              </DetailList>
            )}

            {fields.length > 0 && (
              <section aria-labelledby="action-fields-title" className="flex flex-col gap-2">
                <h3 id="action-fields-title" className="type-subheading text-fg">
                  Поля
                </h3>
                <ul aria-label="Поля предложения" className="flex flex-col divide-y divide-border">
                  {fields.map((field) => (
                    <li key={field.name} className={listRowClass}>
                      <ListRowBody
                        title={field.name}
                        meta={<span className="break-all type-mono">{field.value}</span>}
                      />
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {steps !== null && steps > 0 && (
              <p className="flex items-center gap-2 type-ui text-fg-muted">
                <Icon name="site" size={20} />
                {`Браузерных шагов: ${steps}.`}
              </p>
            )}

            {result && (
              <DetailList label="Итог">
                <DetailRow icon="ok" label="Что получилось">
                  {result}
                </DetailRow>
              </DetailList>
            )}
            {rollback && <p className="type-caption text-fg-subtle">Откат: {rollback}</p>}
            {current.runId && (
              <Button variant="secondary" leadingIcon="history" asChild fullWidth>
                <Link to="/agent/runs/$runId" params={{ runId: current.runId }} onClick={onClose}>
                  Открыть трассу хода
                </Link>
              </Button>
            )}
            {current.idempotencyKey && (
              <p className="break-all type-caption text-fg-subtle">Ключ повтора: {current.idempotencyKey}</p>
            )}
          </div>
        )}
      </Sheet>
      <Dialog
        open={confirmDeny}
        onOpenChange={setConfirmDeny}
        title="Запретить действие?"
        description="Агент получит отказ и не будет выполнять это действие. Запись останется в журнале."
        confirmLabel="Запретить"
        cancelLabel="Оставить вопрос"
        tone="default"
        onConfirm={async () => {
          await decide.mutateAsync("deny").catch(() => undefined);
        }}
      />
    </>
  );
}
