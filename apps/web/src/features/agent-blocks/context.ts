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
}
