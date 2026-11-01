import { Button, EmptyState } from "@botai/design";
import { Link } from "@tanstack/react-router";
import { connectUniversityLink } from "@/features/data/chat-links";

/**
 * Пустое состояние до первого вуза: что здесь будет и одно действие — чат с готовой просьбой подключить вуз.
 * Отдельной формы «создать вуз» нет осознанно (D-004, FR-059): вуз создаёт агент в чате по названию, поэтому
 * экран честно называет этот путь, включая голосовой ввод, который уже есть в поле ввода. Это не «всё сдано».
 */
export function ConnectPrompt() {
  return (
    <EmptyState
      icon="university"
      title="Вузов пока нет"
      description="Подключение идёт в чате: назовите вуз и пришлите ссылку на личный кабинет — агент создаст вуз и подключит источники. Можно и голосом: распознавание речи уже есть в поле ввода."
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
