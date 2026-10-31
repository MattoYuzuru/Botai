import type { IconName } from "@botai/design";
import type { ActionJournalEntry, ActionPolicy, EffectivePolicy } from "@/api/actions";

/**
 * Словарь автономии и журнала действий (docs/v4/19-actions.md §1): классы, режимы, области и
 * решения. Цвет — не единственный сигнал: у каждого значения есть слово и иконка. Правило
 * «источник > вуз > сайт > глобально» не повторяется на клиенте — его применяет сервер в `effective`.
 */
export type ActionClass = "read" | "write_soft" | "write_destructive" | "money" | "auth";
export type ActionMode = "auto" | "ask" | "never";
export type PolicyScope = "global" | "site" | "space" | "source";

export interface ActionClassView {
  value: ActionClass;
  label: string;
  description: string;
  icon: IconName;
}

export const ACTION_CLASSES: readonly ActionClassView[] = [
  {
    value: "read",
    label: "Чтение",
    description: "Загрузка страниц и данных: без изменений на сайте.",
    icon: "search",
  },
  {
    value: "write_soft",
    label: "Изменения",
    description: "Отправка форм и мягкие изменения, которые можно обратить.",
    icon: "edit",
  },
  {
    value: "write_destructive",
    label: "Необратимые изменения",
    description: "Удаление, отзыв и окончательная отправка.",
    icon: "warning",
  },
  {
    value: "money",
    label: "Деньги",
    description: "Оплаты, взносы и другие денежные действия.",
    icon: "grade",
  },
  {
    value: "auth",
    label: "Вход и доступ",
    description: "Вход, коды и смена доступа к аккаунту.",
    icon: "lock",
  },
];

const BY_CLASS = new Map(ACTION_CLASSES.map((item) => [item.value, item]));

export function describeActionClass(raw: string): ActionClassView {
  return (
    BY_CLASS.get(raw as ActionClass) ?? {
      value: "write_soft",
      label: "Действие",
      description: "Класс, которого интерфейс ещё не знает.",
      icon: "connection",
    }
  );
}

export interface ActionModeView {
  value: ActionMode;
  label: string;
  description: string;
  icon: IconName;
}

export const ACTION_MODES: readonly ActionModeView[] = [
  {
    value: "auto",
    label: "Автоматически",
    description: "Выполняется сразу, запись в журнале остаётся.",
    icon: "ok",
  },
  {
    value: "ask",
    label: "Спрашивать",
    description: "Сначала покажет метод, адрес и поля — выполнение после «Разрешить».",
    icon: "help",
  },
  {
    value: "never",
    label: "Никогда",
    description: "Не выполняется: в журнале отказ, агент получает причину.",
    icon: "close",
  },
];

const MODE_BY_VALUE = new Map(ACTION_MODES.map((item) => [item.value, item]));

export function describeActionMode(raw: string): ActionModeView {
  return (
    MODE_BY_VALUE.get(raw as ActionMode) ?? {
      value: "ask",
      label: "Неизвестный режим",
      description: "Сервер вернул режим, которого интерфейс ещё не знает.",
      icon: "info",
    }
  );
}

/** Дефолты из docs/v4/19-actions.md §1 — только для подписи «по умолчанию», не для решения. */
export const DEFAULT_MODES: Record<ActionClass, ActionMode> = {
  read: "auto",
  write_soft: "ask",
  write_destructive: "ask",
  money: "ask",
  auth: "ask",
};

const SCOPE_LABEL: Record<PolicyScope, string> = {
  global: "все источники",
  site: "сайт",
  space: "вуз",
  source: "источник",
};

export function describeScopeKind(raw: string): string {
  return SCOPE_LABEL[raw as PolicyScope] ?? "область";
}

export interface DecisionView {
  label: string;
  tone: "success" | "info" | "warning" | "danger" | "neutral";
  icon: IconName;
  /** Ждёт решения владельца: в журнале и в карточке показываем кнопки. */
  pending: boolean;
}

const DECISION: Record<string, DecisionView> = {
  auto: { label: "Выполнено автоматически", tone: "success", icon: "ok", pending: false },
  asked: { label: "Ждёт решения", tone: "warning", icon: "waiting", pending: true },
  approved: { label: "Разрешено", tone: "info", icon: "check", pending: false },
  executing: { label: "Выполняется", tone: "info", icon: "sync", pending: false },
  executed: { label: "Выполнено", tone: "success", icon: "ok", pending: false },
  failed: { label: "Не выполнено", tone: "danger", icon: "error", pending: false },
  denied: { label: "Запрещено владельцем", tone: "neutral", icon: "close", pending: false },
  never: { label: "Запрещено политикой", tone: "neutral", icon: "paused", pending: false },
  expired: { label: "Время вышло", tone: "neutral", icon: "time", pending: false },
};

export function describeDecision(raw: string): DecisionView {
  return (
    DECISION[raw] ?? {
      label: "Неизвестное решение",
      tone: "neutral",
      icon: "info",
      pending: false,
    }
  );
}

export interface PolicyOrigin {
  /** Подпись, откуда взялся режим: своя настройка, унаследовано или дефолт. */
  label: string;
  /** Строка политики, которую можно удалить (только своя, в этой области). */
  explicitId: string | null;
}

/** Явная строка политики этой области для класса; ищется по точному совпадению. */
export function findExplicitPolicy(
  policies: readonly ActionPolicy[],
  scopeKind: PolicyScope,
  scopeId: string | null | undefined,
  actionClass: string,
): ActionPolicy | null {
  return (
    policies.find(
      (policy) =>
        policy.scopeKind === scopeKind &&
        policy.actionClass === actionClass &&
        (scopeKind === "global" ? policy.scopeId === null : policy.scopeId === (scopeId ?? null)),
    ) ?? null
  );
}

/**
 * Откуда взялся эффективный режим. Клиент не вычисляет победителя — он читает `effective.scopeKind`
 * и сравнивает с локальной областью; без явной строки победа более широкой области видна словами.
 */
export function describePolicyOrigin(
  effective: EffectivePolicy | undefined,
  explicit: ActionPolicy | null,
  policies: readonly ActionPolicy[],
  actionClass: string,
): PolicyOrigin {
  if (explicit) return { label: "своя настройка", explicitId: explicit.id };
  if (!effective) return { label: "по умолчанию", explicitId: null };
  const fromGlobal = policies.some(
    (policy) => policy.scopeKind === "global" && policy.actionClass === actionClass,
  );
  if (effective.scopeKind === "global") {
    return { label: fromGlobal ? "из общих настроек" : "по умолчанию", explicitId: null };
  }
  return { label: `унаследовано: ${describeScopeKind(effective.scopeKind)}`, explicitId: null };
}

/** Причина повышения режима лимитом — словами; неизвестную не изобретаем. */
export function describeEscalation(raw: string | null | undefined): string | null {
  if (!raw) return null;
  if (raw === "max_per_hour") return "достигнут предел действий в час";
  if (raw === "max_per_day") return "достигнут предел действий в день";
  return "ограничение владельца";
}

export interface ProposalField {
  name: string;
  value: string;
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Поля предложения, как их показал бы агент: сервер уже замаскировал секреты. */
export function proposalFields(action: Pick<ActionJournalEntry, "proposal">): ProposalField[] {
  const proposal = record(action.proposal) ?? {};
  const fields = proposal.fields;
  if (!Array.isArray(fields)) return [];
  return fields.flatMap((item): ProposalField[] => {
    const row = record(item);
    const name = row ? text(row.name) : null;
    if (!row || !name) return [];
    const value = row.value;
    return [
      {
        name,
        value:
          typeof value === "string"
            ? value
            : typeof value === "number" || typeof value === "boolean" || value === null
              ? String(value)
              : JSON.stringify(value),
      },
    ];
  });
}

export interface ActionRequestView {
  method: string | null;
  url: string | null;
  detail: string | null;
  bodyKind: string | null;
  bodyBytes: number | null;
}

/** Выполненный запрос без значений: метод, адрес, вид тела и размер — как в журнале сервера. */
export function describeRequest(action: Pick<ActionJournalEntry, "request" | "proposal">): ActionRequestView {
  const request = record(action.request) ?? {};
  const proposal = record(action.proposal) ?? {};
  return {
    method: text(request.method) ?? text(proposal.method),
    url: text(request.url) ?? text(proposal.url),
    detail: text(request.detail) ?? text(request.body_sha256) ?? null,
    bodyKind: text(request.body_kind) ?? text(proposal.body_kind),
    bodyBytes: number(request.body_bytes),
  };
}

/** Итог действия из журнала — только настоящие поля сервера. */
export function actionResultText(action: Pick<ActionJournalEntry, "result">): string | null {
  const result = record(action.result);
  if (!result) return null;
  const status = number(result.status);
  const summary = text(result.summary) ?? text(result.detail) ?? text(result.error);
  const parts: string[] = [];
  if (status !== null) parts.push(`статус ${status}`);
  if (summary) parts.push(summary);
  if (parts.length === 0) {
    try {
      return JSON.stringify(result, null, 2);
    } catch {
      return null;
    }
  }
  return parts.join(" · ");
}

/** Подсказка отката из playbook, если сервер её дал. */
export function rollbackHint(action: Pick<ActionJournalEntry, "rollback">): string | null {
  const rollback = record(action.rollback);
  if (!rollback) return null;
  return text(rollback.hint) ?? text(rollback.detail) ?? text(rollback.summary) ?? null;
}

/** Сколько шагов у браузерного действия — для подписи, без выдуманных подробностей. */
export function browserSteps(action: Pick<ActionJournalEntry, "proposal">): number | null {
  const proposal = record(action.proposal) ?? {};
  return Array.isArray(proposal.steps) ? proposal.steps.length : null;
}
