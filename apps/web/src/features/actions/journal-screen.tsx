import { Badge, Banner, EmptyState, formatAgo, ListRow, Select, SkeletonText, useNow } from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useRouter } from "@tanstack/react-router";
import { useState } from "react";
import { type ActionJournalEntry, actionsJournalQuery, type JournalFilters } from "@/api/actions";
import { humanMessage } from "@/api/errors";
import { sourcesQuery } from "@/api/sources";
import { Screen } from "@/app/screen";
import { ActionSheet } from "./action-sheet";
import { ACTION_CLASSES, actionScopeText, describeActionClass, describeDecision } from "./policy";

const DECISION_FILTERS = [
  { value: "asked", label: "Ждут решения" },
  { value: "approved", label: "Разрешены" },
  { value: "executed", label: "Выполнены" },
  { value: "failed", label: "Не выполнены" },
  { value: "denied", label: "Запрещены" },
  { value: "never", label: "Запрещены политикой" },
  { value: "auto", label: "Автоматические" },
] as const;

/**
 * Журнал действий агента (docs/v4/19-actions.md §4): что агент предложил и сделал по политике
 * владельца, с фильтрами и решением по `ask`. Данные — только с сервера: журнал не хранит тел
 * запросов, значения уже замаскированы.
 */
export function ActionsJournalScreen({ source }: { source?: string }) {
  const router = useRouter();
  const navigate = useNavigate();
  const nowMs = useNow(30_000);
  const [decision, setDecision] = useState<string | undefined>(undefined);
  const [actionClass, setActionClass] = useState<string | undefined>(undefined);
  const [opened, setOpened] = useState<ActionJournalEntry | null>(null);
  const filters: JournalFilters = {
    sourceId: source,
    decision,
    actionClass,
    limit: 100,
  };
  const journal = useQuery(actionsJournalQuery(filters));
  const sources = useQuery(sourcesQuery());
  const back = () => {
    if (router.history.canGoBack()) router.history.back();
    else void navigate({ to: "/study" });
  };
  const pendingCount = (journal.data ?? []).filter((item) => item.decision === "asked").length;

  return (
    <Screen
      title="Действия агента"
      subtitle="Что агент предлагал и делал по вашей политике"
      leading="back"
      onBack={back}
    >
      <div
        data-screen="actions"
        data-state={
          journal.isPending
            ? "loading"
            : journal.isError
              ? "error"
              : (journal.data ?? []).length === 0
                ? "empty"
                : "ok"
        }
        className="flex flex-col gap-4"
      >
        <div className="flex flex-wrap items-center gap-2">
          <Select
            options={[
              { value: "", label: "Все решения" },
              ...DECISION_FILTERS.map((item) => ({ value: item.value, label: item.label })),
            ]}
            value={decision ?? ""}
            onValueChange={(value) => setDecision(value || undefined)}
            aria-label="Решение"
            sheetTitle="Решение"
            className="min-w-40"
          />
          <Select
            options={[
              { value: "", label: "Все классы" },
              ...ACTION_CLASSES.map((item) => ({ value: item.value, label: item.label })),
            ]}
            value={actionClass ?? ""}
            onValueChange={(value) => setActionClass(value || undefined)}
            aria-label="Класс действия"
            sheetTitle="Класс действия"
            className="min-w-40"
          />
          <Select
            options={[
              { value: "", label: "Все источники" },
              ...(sources.data ?? []).map((item) => ({ value: item.id, label: item.title })),
            ]}
            value={source ?? ""}
            onValueChange={(value) =>
              void navigate({ to: "/actions", search: value ? { source: value } : {} })
            }
            aria-label="Источник"
            sheetTitle="Источник"
            className="min-w-40"
          />
          {pendingCount > 0 && <Badge tone="warning">Ждут решения: {pendingCount}</Badge>}
        </div>

        {journal.isError ? (
          <Banner tone="warning" icon="offline" live>
            Журнал не загрузился: {humanMessage(journal.error)} Обновление идёт в фоне.
          </Banner>
        ) : journal.isPending ? (
          <SkeletonText lines={5} label="Загрузка журнала действий" />
        ) : (journal.data ?? []).length === 0 ? (
          <EmptyState
            icon="history"
            title="Действий пока нет"
            description="Здесь появятся записи, когда агент предложит или выполнит действие."
            size="sm"
          />
        ) : (
          <section aria-labelledby="actions-journal-title" className="flex flex-col gap-2">
            <div className="flex items-center justify-between gap-2">
              <h2 id="actions-journal-title" className="type-subheading text-fg">
                Журнал
              </h2>
              <span className="type-caption text-fg-muted">{journal.data?.length}</span>
            </div>
            <ul aria-label="Журнал действий" className="flex flex-col divide-y divide-border">
              {(journal.data ?? []).map((action) => {
                const decisionView = describeDecision(action.decision);
                const classView = describeActionClass(action.actionClass);
                const created = Date.parse(action.createdAt);
                return (
                  <li key={action.id} data-action-id={action.id} data-action-decision={action.decision}>
                    <ListRow
                      icon={classView.icon}
                      title={action.intent}
                      onOpen={() => setOpened(action)}
                      meta={
                        <>
                          <Badge tone={decisionView.tone} icon={decisionView.icon}>
                            {decisionView.label}
                          </Badge>
                          <span>{classView.label}</span>
                          <span>{actionScopeText(action)}</span>
                          <time dateTime={action.createdAt}>
                            {Number.isFinite(created) ? formatAgo(created, nowMs) : ""}
                          </time>
                        </>
                      }
                      aria-label={`${action.intent}: ${decisionView.label}`}
                    />
                  </li>
                );
              })}
            </ul>
          </section>
        )}
        <p className="type-caption text-fg-subtle">
          Политику автономии можно изменить в карточке источника и в настройках на экране «Учёба».
        </p>
      </div>
      <ActionSheet action={opened} onClose={() => setOpened(null)} />
    </Screen>
  );
}
