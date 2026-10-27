import { Button, EmptyState } from "@botai/design";
import { Link } from "@tanstack/react-router";

/**
 * Пустой чат: что здесь можно спросить и до трёх вопросов из настоящих данных. Подсказка отправляется сразу,
 * как обычное сообщение. Вуз не подключён — приглашение подключить, а не выдуманные вопросы.
 */
export function EmptyChat({
  suggestions,
  noSpaces,
  onPick,
}: {
  suggestions: readonly string[];
  noSpaces: boolean;
  onPick: (text: string) => void;
}) {
  return (
    <div className="mx-auto flex w-full max-w-col flex-col gap-4 py-6" data-empty-chat="">
      <EmptyState
        icon="chat"
        title="Спросите про пары и сроки"
        description={
          noSpaces
            ? "Подключите вуз — и агент ответит по вашему расписанию, срокам и документам."
            : "Агент отвечает по вашим данным и показывает, откуда взял ответ."
        }
        action={
          noSpaces ? (
            <Button variant="primary" asChild>
              <Link to="/connect">Подключить вуз</Link>
            </Button>
          ) : undefined
        }
        size="sm"
      />
      {suggestions.length > 0 && (
        <ul className="flex flex-col gap-2" aria-label="Подсказки">
          {suggestions.map((text) => (
            <li key={text}>
              <Button
                variant="secondary"
                fullWidth
                className="h-auto min-h-11 justify-start py-2.5 text-left whitespace-normal"
                onClick={() => onPick(text)}
              >
                {text}
              </Button>
            </li>
          ))}
        </ul>
      )}
      {/* Пол сцены Вари: полоса над полем ввода зарезервирована, раскладка не прыгает (mascot.md §4.2). */}
      <div aria-hidden="true" data-stage-floor="" className="h-24 shrink-0 min-[390px]:h-30" />
    </div>
  );
}
