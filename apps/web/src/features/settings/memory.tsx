import { Card, Dialog, formatDay, IconButton, Skeleton, toast, useNow } from "@botai/design";
import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, call } from "@/api/client";
import { humanMessage, isApiError } from "@/api/errors";
import type { MemoryItem, StatusRule } from "@/features/data/types";
import { DEADLINE_STATE_LABEL } from "@/features/tasks/deadlines";

export const memoryKeys = {
  memory: ["me", "memory"] as const,
  statusRules: ["status-rules"] as const,
};

export const memoryQuery = queryOptions({
  queryKey: memoryKeys.memory,
  queryFn: () => call(api.GET("/api/v1/me/memory")),
  staleTime: 30_000,
});

export const statusRulesQuery = queryOptions({
  queryKey: memoryKeys.statusRules,
  queryFn: () => call(api.GET("/api/v1/status-rules")),
  staleTime: 30_000,
});

type Pending = { kind: "memory"; item: MemoryItem } | { kind: "rule"; rule: StatusRule } | null;

/**
 * «Что помнит агент» (D-042, D-045) — в настройках «Учёбы». Правила для агента (указания студента) отдельно от
 * заметок (фактов о студенте), отдельно — правила статусов заданий («„Решение прикреплено“ → Сдано»). Создаются
 * они просьбой в чате; здесь их видно и можно удалить. Удаление необратимо — через `Dialog`.
 */
export function MemorySection() {
  const nowMs = useNow(60_000);
  const client = useQueryClient();
  const memory = useQuery(memoryQuery);
  const rules = useQuery(statusRulesQuery);
  const [pending, setPending] = useState<Pending>(null);

  const remove = useMutation({
    mutationFn: async (target: NonNullable<Pending>) => {
      if (target.kind === "memory") {
        await call(
          api.DELETE("/api/v1/me/memory/{note_id}", { params: { path: { note_id: target.item.id } } }),
        );
      } else {
        await call(
          api.DELETE("/api/v1/status-rules/{rule_id}", { params: { path: { rule_id: target.rule.id } } }),
        );
      }
    },
    onSuccess: async (_data, target) => {
      toast(
        target.kind === "rule"
          ? "Правило статуса удалено"
          : target.item.kind === "rule"
            ? "Правило удалено"
            : "Заметка удалена",
      );
      await client.invalidateQueries({
        queryKey: target.kind === "rule" ? memoryKeys.statusRules : memoryKeys.memory,
      });
      // Правило статуса меняет итоговое состояние заданий: перечитываем их сразу.
      if (target.kind === "rule") {
        await Promise.all(
          [["deadlines"], ["today"], ["updates"]].map((queryKey) => client.invalidateQueries({ queryKey })),
        );
      }
    },
    onError: (error) => toast("Не удалось удалить", { tone: "warning", description: humanMessage(error) }),
  });

  const items = memory.data ?? [];
  const agentRules = items.filter((item) => item.kind === "rule");
  const notes = items.filter((item) => item.kind !== "rule");
  const statusRules = rules.data ?? [];
  // Старый сервер без правил статусов (404) — блока правил просто нет.
  const rulesMissing = rules.isError && isApiError(rules.error) && rules.error.status === 404;
  const loading = memory.isPending || (rules.isPending && !rulesMissing);
  const empty = !loading && items.length === 0 && statusRules.length === 0;
  const when = (iso: string) => formatDay(Date.parse(iso), nowMs);

  return (
    <section aria-labelledby="memory-title" className="flex flex-col gap-3">
      <h2 id="memory-title" className="type-heading text-fg">
        Что помнит агент
      </h2>
      <p className="type-caption text-fg-muted">
        Правила и заметки появляются, когда вы просите агента в чате: «запомни: про оценки сначала смотри ЦУ»,
        «считай „Решение прикреплено“ сданным». Здесь их видно и можно удалить.
      </p>
      <Card padding="sm" className="flex flex-col gap-3">
        {loading ? (
          <div role="status" aria-busy="true" className="flex flex-col gap-2 p-2">
            <span className="sr-only">Загрузка памяти агента</span>
            <Skeleton className="h-11 rounded-md" />
            <Skeleton className="h-11 rounded-md" />
          </div>
        ) : memory.isError ? (
          <p className="px-3 py-3 type-ui text-fg-muted">Память агента не загрузилась. Она появится сама.</p>
        ) : empty ? (
          <p className="px-3 py-3 type-ui text-fg-muted">Агент пока ничего не запомнил.</p>
        ) : (
          <>
            <MemoryGroup
              title="Правила для агента"
              id="memory-rules"
              rows={agentRules.map((item) => ({
                id: item.id,
                text: item.note,
                meta: when(item.createdAt),
                label: `Удалить правило «${item.note}»`,
                onDelete: () => setPending({ kind: "memory", item }),
              }))}
            />
            <MemoryGroup
              title="Правила статусов"
              id="memory-status-rules"
              rows={statusRules.map((rule) => ({
                id: rule.id,
                text: `«${rule.label}» значит «${DEADLINE_STATE_LABEL[rule.state]}»`,
                meta: `${rule.sourceTitle ?? "Все источники"} · ${when(rule.createdAt)}`,
                label: `Удалить правило статуса «${rule.label}»`,
                onDelete: () => setPending({ kind: "rule", rule }),
              }))}
            />
            <MemoryGroup
              title="Заметки"
              id="memory-notes"
              rows={notes.map((item) => ({
                id: item.id,
                text: item.note,
                meta: when(item.createdAt),
                label: `Удалить заметку «${item.note}»`,
                onDelete: () => setPending({ kind: "memory", item }),
              }))}
            />
          </>
        )}
      </Card>
      <Dialog
        open={pending !== null}
        onOpenChange={(open) => !open && setPending(null)}
        title={
          pending?.kind === "rule"
            ? "Удалить правило статуса?"
            : pending?.item.kind === "rule"
              ? "Удалить правило?"
              : "Удалить заметку?"
        }
        description={
          pending?.kind === "rule"
            ? `Задания со статусом «${pending.rule.label}» снова получат состояние по словам источника.`
            : "Агент перестанет это учитывать. Вернуть можно, попросив его в чате ещё раз."
        }
        confirmLabel="Удалить"
        onConfirm={() => (pending ? remove.mutateAsync(pending).catch(() => undefined) : undefined)}
      />
    </section>
  );
}

function MemoryGroup({
  title,
  id,
  rows,
}: {
  title: string;
  id: string;
  rows: { id: string; text: string; meta: string; label: string; onDelete: () => void }[];
}) {
  if (rows.length === 0) return null;
  return (
    <section aria-labelledby={id} className="flex flex-col">
      <h3 id={id} className="px-3 pt-2 pb-1 type-label text-fg-muted">
        {title}
      </h3>
      <ul className="flex flex-col">
        {rows.map((row) => (
          <li key={row.id} className="flex min-h-14 items-center gap-3 rounded-md py-1 pl-3">
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <p className="break-words type-ui text-fg">{row.text}</p>
              <p className="type-caption text-fg-muted">{row.meta}</p>
            </div>
            <IconButton icon="trash" label={row.label} onClick={row.onDelete} />
          </li>
        ))}
      </ul>
    </section>
  );
}
