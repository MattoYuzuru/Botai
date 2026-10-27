import type { IconName } from "@botai/design";
import type { AnswerValues } from "@/features/jobs/job-manager";
import type { AnswerState } from "@/features/jobs/job-state";

/** Всё, что нужно блоку, чтобы нарисоваться и ответить. Блок сам ничего не исполняет. */
export interface BlockContext {
  jobId: string;
  answers: Record<string, AnswerState>;
  nowMs: number;
  timeZone: string;
  spaceTitle?: string;
  /** Ответ человека: значения для сервера и текст для истории (`null` — секрет, показываем «введено»). */
  onAnswer: (blockId: string, values: AnswerValues | null, shown: string | null) => void;
  /** Одобрение предложенного действия (ApprovalCard): метод, хост и поля уже подтверждены человеком. */
  onApprove: (blockId: string, proposalId: string, fields: Record<string, string>) => void;
  onReject: (blockId: string, proposalId: string) => void;
  /**
   * Чат: честная пометка «Не уверен» для текстового блока. Без функции блок рисуется как обычный ответ
   * (подключение вуза не оценивает уверенность).
   */
  confidence?: (markdown: string) => "ok" | "unsure";
  /**
   * Чат: внутренняя ссылка источника (`botai://documents/<id>`) открывается в приложении. Возвращает `null`,
   * если адрес не внутренний; тогда источник рисуется как обычный (http(s) — в новой вкладке).
   */
  internalLink?: (
    url: string,
  ) => { href: string; label: string; icon?: IconName; onOpen?: () => void } | null;
}
