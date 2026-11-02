import { Button, EmptyState } from "@botai/design";
import { Link } from "@tanstack/react-router";
import { CONNECT_DRAFT } from "./drafts";

/**
 * Пустой чат: что здесь можно спросить и до трёх вопросов из настоящих данных. Подсказка отправляется сразу,
 * как обычное сообщение. Подключение нового вуза всегда доступно и только заполняет редактируемую заготовку.
 * Блок стоит по центру свободной области; место под сцену Вари входит в него, поэтому над полем ввода
 * нет пустой полосы (mascot.md §4.2).
 */
export function EmptyChat({
  suggestions,
  noSpaces,
  onPick,
  onConnect,
}: {
  suggestions: readonly string[];
  noSpaces: boolean;
  onPick: (text: string) => void;
  onConnect?: () => void;
}) {
  return (
    <div className="my-auto flex w-full flex-col gap-4 py-6" data-empty-chat="">
      <EmptyState
        icon="chat"
        title="Спросите про пары и сроки"
        description={
          noSpaces
            ? "Вуза пока нет — создадим его в этом чате. Напишите название вуза и пришлите ссылку на личный кабинет или скажите голосом: агент создаст вуз и подключит источники."
            : "Агент отвечает по вашим данным и показывает, откуда взял ответ."
        }
        action={
          <Button variant={noSpaces ? "primary" : "secondary"} asChild>
            {/* Заготовка ложится в поле этого же чата: экран не должен прыгать к началу ленты. */}
            <Link
              to="/chat"
              search={{ draft: CONNECT_DRAFT }}
              resetScroll={false}
              onClick={(event) => {
                if (
                  !onConnect ||
                  event.button !== 0 ||
                  event.metaKey ||
                  event.ctrlKey ||
                  event.shiftKey ||
                  event.altKey
                )
                  return;
                event.preventDefault();
                onConnect();
              }}
            >
              {noSpaces ? "Подключить вуз" : "Подключить новый вуз"}
            </Link>
          </Button>
        }
        size="sm"
      />
      {/* Данные приходят после первого рендера. Резерв трёх строк не даёт кнопке подключения уехать из-под тапа. */}
      {(!noSpaces || suggestions.length > 0) && (
        <div className={noSpaces ? undefined : "min-h-44"} data-empty-chat-suggestions="">
          {suggestions.length > 0 && (
            <ul className="mx-auto flex w-full max-w-reading flex-col gap-2" aria-label="Подсказки">
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
        </div>
      )}
      {/* Сцена Вари входит в блок: пол над полем ввода занят ею, а не пустотой (mascot.md §4.2). */}
      <div aria-hidden="true" data-stage-floor="" className="h-24 shrink-0 min-[390px]:h-30" />
    </div>
  );
}
