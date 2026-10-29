import { cn } from "../../lib/cn";
import { Card } from "../card/card";
import { Icon, type IconName } from "../icon";

export type PlanStepState = "pending" | "active" | "done" | "skipped";

export interface PlanStep {
  id: string;
  title: string;
  state: PlanStepState;
}

export interface PlanCardProps {
  title: string;
  steps: readonly PlanStep[];
  className?: string;
}

const STATE: Record<PlanStepState, { icon: IconName; tone: string; text: string }> = {
  pending: { icon: "pending", tone: "text-fg-subtle", text: "ждёт" },
  active: { icon: "sync", tone: "text-accent-fg animate-pulse-soft", text: "в работе" },
  done: { icon: "ok", tone: "text-success-fg", text: "выполнено" },
  skipped: { icon: "minus", tone: "text-fg-subtle", text: "пропущено" },
};

/**
 * План ответа агента: шаги и их состояние. Счётчик «выполнено 2 из 4» считается по настоящим состояниям шагов,
 * без процентов. План обновляется на месте — тот же блок приходит снова с новыми состояниями.
 */
export function PlanCard({ title, steps, className }: PlanCardProps) {
  const done = steps.filter((step) => step.state === "done").length;
  return (
    <Card data-plan="" padding="md" className={cn("flex flex-col gap-3", className)}>
      <div className="flex flex-col gap-0.5">
        <p className="type-caption text-fg-muted" data-numeric="">
          {`План · выполнено ${done} из ${steps.length}`}
        </p>
        <p className="type-subheading text-fg">{title}</p>
      </div>
      <ol aria-label="Шаги плана" className="flex flex-col gap-2">
        {steps.map((step) => {
          const meta = STATE[step.state];
          return (
            <li key={step.id} data-state={step.state} className="flex items-start gap-2">
              <Icon name={meta.icon} size={20} className={cn("mt-0.5", meta.tone)} />
              <span
                className={cn(
                  "min-w-0 flex-1 type-ui [overflow-wrap:anywhere]",
                  step.state === "done" && "text-fg",
                  step.state === "active" && "font-medium text-fg",
                  step.state === "pending" && "text-fg-muted",
                  step.state === "skipped" && "text-fg-subtle",
                )}
              >
                {step.title}
                <span className="sr-only">{`: ${meta.text}`}</span>
              </span>
            </li>
          );
        })}
      </ol>
    </Card>
  );
}
