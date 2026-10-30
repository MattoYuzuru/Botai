import { Button, EmptyState } from "@botai/design";
import { Link } from "@tanstack/react-router";
import { connectUniversityLink } from "@/features/data/chat-links";

/**
 * Пустое состояние до первого вуза: что здесь будет и одно действие — чат с готовой просьбой «Давай подключим
 * вуз …». Это не «всё сдано».
 */
export function ConnectPrompt() {
  return (
    <EmptyState
      icon="university"
      title="Вузов пока нет"
      description="Подключите вуз — пары, сроки и ответы агента соберутся в одном месте."
      action={
        <div className="flex flex-col items-center gap-1">
          <Button variant="primary" asChild>
            <Link {...connectUniversityLink()}>Подключить вуз</Link>
          </Button>
          <Button variant="ghost" asChild>
            <Link to="/welcome">Как это работает</Link>
          </Button>
        </div>
      }
    />
  );
}
