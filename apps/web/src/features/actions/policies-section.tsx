import { Badge, Banner, Button, Card, IconButton, Select, SkeletonText, toast } from "@botai/design";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import {
  actionKeys,
  actionPoliciesQuery,
  deleteActionPolicy,
  effectivePoliciesQuery,
  upsertActionPolicy,
} from "@/api/actions";
import { humanMessage } from "@/api/errors";
import {
  ACTION_CLASSES,
  ACTION_MODES,
  type ActionClass,
  type ActionMode,
  describeActionMode,
  describeEscalation,
  describePolicyOrigin,
  findExplicitPolicy,
  type PolicyScope,
} from "./policy";

/**
 * Настройки автономии для одной области: глобально или для источника. Показывает эффективный режим
 * каждого класса так, как его посчитал сервер (`effective`), и позволяет задать свою строку политики.
 * «Источник > вуз > сайт > глобально» на клиенте не вычисляется — это правило сервера.
 */
export function PoliciesSection({
  scopeKind,
  scopeId,
  sourceId,
  spaceId,
  title = "Автономия",
  journalSourceId,
}: {
  scopeKind: Extract<PolicyScope, "global" | "space" | "source">;
  /** Идентификатор области для `source`/`space`; у глобальной области его нет. */
  scopeId?: string;
  /** Для `effective`: какой источник/вуз смотреть. */
  sourceId?: string;
  spaceId?: string;
  title?: string;
  /** Ссылка в журнал с фильтром по источнику (в карточке источника). */
  journalSourceId?: string;
}) {
  const queryClient = useQueryClient();
  const effective = useQuery(effectivePoliciesQuery({ sourceId, spaceId }));
  const policies = useQuery(actionPoliciesQuery());
  const byClass = new Map((effective.data ?? []).map((item) => [item.actionClass, item]));

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: actionKeys.policies });
    void queryClient.invalidateQueries({
      queryKey: actionKeys.effective(sourceId, spaceId, undefined),
    });
  };
  const save = useMutation({
    mutationFn: (input: { actionClass: ActionClass; mode: ActionMode }) =>
      upsertActionPolicy({
        scopeKind,
        scopeId: scopeKind === "global" ? undefined : scopeId,
        actionClass: input.actionClass,
        mode: input.mode,
      }),
    onSuccess: () => {
      invalidate();
      toast("Настройка автономии сохранена", { tone: "success" });
    },
    onError: (error) =>
      toast("Не удалось сохранить настройку", { tone: "error", description: humanMessage(error) }),
  });
  const reset = useMutation({
    mutationFn: (policyId: string) => deleteActionPolicy(policyId),
    onSuccess: () => {
      invalidate();
      toast("Настройка сброшена к значению по умолчанию", { tone: "success" });
    },
    onError: (error) =>
      toast("Не удалось сбросить настройку", { tone: "error", description: humanMessage(error) }),
  });

  const pendingClass = save.isPending ? (save.variables?.actionClass ?? null) : null;

  return (
    <section aria-labelledby={`policy-${scopeKind}-${scopeId ?? "global"}`} className="flex flex-col gap-2">
      <h3 id={`policy-${scopeKind}-${scopeId ?? "global"}`} className="type-subheading text-fg">
        {title}
      </h3>
      <p className="type-caption text-fg-muted">
        Чтение выполняется сразу, изменения и деньги по умолчанию спрашивают. Настройка источника важнее
        настройки вуза и общих правил; сервер сам решает, какая строка победила.
      </p>
      {effective.isError ? (
        <Banner tone="warning" icon="offline" live>
          Настройки не загрузились: {humanMessage(effective.error)} Обновление идёт в фоне.
        </Banner>
      ) : effective.isPending || policies.isPending ? (
        <SkeletonText lines={5} label="Загрузка настроек автономии" />
      ) : (
        <Card padding="sm">
          <ul aria-label="Правила автономии" className="flex flex-col divide-y divide-border">
            {ACTION_CLASSES.map((item) => {
              const effectiveClass = byClass.get(item.value);
              const explicit = findExplicitPolicy(
                policies.data ?? [],
                scopeKind,
                scopeId ?? null,
                item.value,
              );
              const origin = describePolicyOrigin(effectiveClass, explicit, policies.data ?? [], item.value);
              const mode = effectiveClass?.mode ?? "ask";
              const modeView = describeActionMode(mode);
              const escalation = describeEscalation(effectiveClass?.escalated);
              return (
                <li
                  key={item.value}
                  data-policy-class={item.value}
                  className="flex flex-wrap items-center gap-x-3 gap-y-2 py-3"
                >
                  <span className="flex min-w-0 flex-1 basis-52 flex-col gap-1">
                    <span className="type-ui text-fg">{item.label}</span>
                    <span className="type-caption text-fg-muted">{item.description}</span>
                    <span className="flex flex-wrap items-center gap-1.5 pt-1">
                      <Badge tone={explicit ? "accent" : "neutral"} data-policy-origin="">
                        {origin.label}
                      </Badge>
                      {escalation && <Badge tone="warning">режим поднят: {escalation}</Badge>}
                    </span>
                  </span>
                  <span className="flex items-center gap-1.5">
                    <Select
                      options={ACTION_MODES.map((option) => ({
                        value: option.value,
                        label: option.label,
                        icon: option.icon,
                      }))}
                      value={mode}
                      onValueChange={(value) =>
                        save.mutate({ actionClass: item.value, mode: value as ActionMode })
                      }
                      disabled={save.isPending}
                      aria-label={`${item.label}: режим`}
                      sheetTitle={`${item.label}: режим`}
                      className="min-w-40"
                    />
                    {explicit && (
                      <IconButton
                        icon="history"
                        label={`Вернуть по умолчанию: ${item.label}`}
                        onClick={() => reset.mutate(explicit.id)}
                        disabled={reset.isPending}
                      />
                    )}
                  </span>
                  <span className="flex min-w-0 basis-full flex-col gap-1 type-caption text-fg-subtle xs:basis-auto">
                    <span>{modeView.description}</span>
                    {pendingClass === item.value && <span>Сохраняем…</span>}
                  </span>
                </li>
              );
            })}
          </ul>
        </Card>
      )}
      <div className="flex flex-wrap items-center gap-2">
        {journalSourceId && (
          <Button variant="secondary" size="sm" leadingIcon="history" asChild data-touch-expanded="">
            <Link to="/actions" search={{ source: journalSourceId }}>
              Журнал действий
            </Link>
          </Button>
        )}
        {scopeKind === "global" && (
          <Button variant="secondary" size="sm" leadingIcon="history" asChild data-touch-expanded="">
            <Link to="/actions">Открыть журнал действий</Link>
          </Button>
        )}
        <span className="type-caption text-fg-subtle">Изменения сохраняются сразу.</span>
      </div>
    </section>
  );
}
