import { useState } from "react";
import { cn } from "../../lib/cn";
import { DEFAULT_TIME_ZONE, formatDay, formatTime } from "../../lib/format";
import { Badge } from "../badge/badge";
import { Banner } from "../banner/banner";
import { Button } from "../button/button";
import { Card } from "../card/card";
import { Field } from "../field/field";
import { Input } from "../input/input";

export interface ProposalField {
  name: string;
  label: string;
  value: string;
  editable: boolean;
}

export interface ApprovalProposal {
  id: string;
  /** Человеческими словами: что произойдёт. */
  intent: string;
  method: "POST" | "PUT" | "PATCH";
  /** Хост, на который уйдёт запрос. */
  host: string;
  fields: readonly ProposalField[];
  reversible: boolean;
  /** Когда предложение истекает, мс. */
  expiresAt: number;
  /** Название вуза, на который действует запрос. */
  spaceTitle?: string;
}

export type ApprovalState = "default" | "pending" | "approved" | "rejected" | "expired";

export interface ApprovalCardProps {
  proposal: ApprovalProposal;
  state?: ApprovalState;
  nowMs: number;
  timeZone?: string;
  /** Получает итоговые значения полей: человек мог исправить редактируемые. */
  onApprove: (fields: Record<string, string>) => void;
  onReject: () => void;
  className?: string;
}

/**
 * Единственный способ получить согласие на запись. Показывает поимённо метод, хост, поля и вуз;
 * подтверждение одноразовое и истекает (срок виден), автоподтверждения по таймеру нет.
 * «Подтвердить» — primary, «Не делать» — secondary. Изменённые поля подтверждаются заново.
 */
export function ApprovalCard({
  proposal,
  state = "default",
  nowMs,
  timeZone = DEFAULT_TIME_ZONE,
  onApprove,
  onReject,
  className,
}: ApprovalCardProps) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(proposal.fields.map((field) => [field.name, field.value])),
  );
  const expired = state === "expired" || (state === "default" && nowMs >= proposal.expiresAt);
  const settled = state === "approved" || state === "rejected" || expired;
  const expiresText = `${formatDay(proposal.expiresAt, nowMs, timeZone)}, ${formatTime(proposal.expiresAt, timeZone)}`;

  return (
    <Card data-approval={expired ? "expired" : state} className={cn("flex flex-col gap-4", className)}>
      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="type-subheading text-fg">Подтвердите действие</h3>
          {proposal.spaceTitle && <Badge tone="neutral">{proposal.spaceTitle}</Badge>}
        </div>
        <p className="type-body text-fg">{proposal.intent}</p>
      </div>

      <dl className="flex flex-col gap-2 rounded-md bg-bg-subtle p-3 type-caption">
        <div className="flex items-baseline gap-2">
          <dt className="w-16 shrink-0 text-fg-muted">Запрос</dt>
          <dd className="min-w-0 type-mono text-fg">
            <span className="font-medium">{proposal.method}</span> {proposal.host}
          </dd>
        </div>
        <div className="flex items-baseline gap-2">
          <dt className="w-16 shrink-0 text-fg-muted">Действует</dt>
          <dd className="text-fg">до {expiresText}</dd>
        </div>
      </dl>

      {proposal.fields.length > 0 && (
        <div className="flex flex-col gap-3">
          {proposal.fields.map((field) =>
            field.editable && !settled ? (
              <Field key={field.name} label={field.label}>
                <Input
                  name={field.name}
                  value={values[field.name] ?? ""}
                  onChange={(event) =>
                    setValues((current) => ({ ...current, [field.name]: event.target.value }))
                  }
                  disabled={state === "pending"}
                />
              </Field>
            ) : (
              <div key={field.name} className="flex flex-col gap-0.5">
                <span className="type-label text-fg-muted">{field.label}</span>
                <span className="whitespace-pre-wrap break-words type-body text-fg">
                  {values[field.name] ?? field.value}
                </span>
              </div>
            ),
          )}
        </div>
      )}

      <div>
        {proposal.reversible ? (
          <Badge tone="info" icon="info">
            Можно отменить
          </Badge>
        ) : (
          <Badge tone="warning" icon="warning">
            Нельзя отменить
          </Badge>
        )}
      </div>

      {state === "approved" && (
        <Banner tone="success" icon="ok" live>
          Подтверждено. Отправляю запрос в вуз.
        </Banner>
      )}
      {state === "rejected" && (
        <Banner tone="neutral" icon="close" live>
          Не делаю. Ничего не отправлено.
        </Banner>
      )}
      {expired && (
        <Banner tone="warning" icon="time" live>
          Время подтверждения истекло. Попросите агента предложить действие снова.
        </Banner>
      )}

      {!settled && (
        <div className="flex flex-col-reverse gap-2 xs:flex-row xs:flex-wrap xs:justify-end">
          <Button variant="secondary" onClick={onReject} disabled={state === "pending"}>
            Не делать
          </Button>
          <Button variant="primary" loading={state === "pending"} onClick={() => onApprove(values)}>
            Подтвердить
          </Button>
        </div>
      )}
    </Card>
  );
}
