import { Badge, Card, ChatMessage, SafeMarkdown } from "@botai/design";
import type { ReactNode } from "react";
import type { AnswerState } from "@/features/jobs/job-state";

/**
 * Рамка вопроса агента (styleguide §9 «Уточняющие вопросы»): один вопрос на сообщение — текст `type-body`,
 * затем элемент ввода и «Ответить». Пока ответа нет, поток ждёт, и это сказано словами («Жду ответа»),
 * а при `required` — и что без ответа работа не продолжится. Отправленный ответ остаётся в истории
 * сообщением человека.
 */
export function QuestionFrame({
  question,
  details,
  required,
  answer,
  editorOnly = false,
  children,
}: {
  question: string;
  /** Что подтверждается (markdown): стоит над вопросом, в том же сообщении, что вопрос и кнопки. */
  details?: string;
  required: boolean;
  answer: AnswerState | undefined;
  editorOnly?: boolean;
  children: ReactNode;
}) {
  if (answer?.status === "answered") {
    if (editorOnly)
      return (
        <p role="status" className="type-caption text-fg-muted">
          Ответ отправлен.
        </p>
      );
    return (
      <div className="flex flex-col gap-3" data-question="answered">
        <ChatMessage role="agent" markdown={details ? `${details}\n\n${question}` : question} />
        <ChatMessage role="user" markdown={answer.shown ?? "Введено"} />
      </div>
    );
  }
  return (
    <Card data-question="pending" className="flex flex-col gap-4" aria-label="Вопрос агента">
      {!editorOnly && (
        <div className="flex flex-col gap-2">
          <Badge tone="info" icon="waiting" className="self-start">
            Жду ответа
          </Badge>
          {details && <SafeMarkdown source={details} />}
          <p className={details ? "type-body font-medium text-fg" : "type-body text-fg"}>{question}</p>
          {required && <p className="type-caption text-fg-muted">Без ответа работа не продолжится.</p>}
        </div>
      )}
      {children}
      {answer?.status === "failed" && answer.error && (
        <p role="alert" className="type-caption text-danger-fg">
          {answer.error}
        </p>
      )}
    </Card>
  );
}
