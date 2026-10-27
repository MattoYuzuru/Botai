import { Button, Card, Icon, type IconName, Wordmark } from "@botai/design";
import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

interface Point {
  icon: IconName;
  title: string;
  body: ReactNode;
}

/** Три вопроса, на которые студент хочет получить честный ответ до первого подключения (FR-028). */
const POINTS: Point[] = [
  {
    icon: "connection",
    title: "Что такое BotAI",
    body: "Одно место для расписания, сроков и заданий из ваших вузов. Вуз подключает агент: он сам находит личный кабинет и показывает каждый свой шаг. Настраивать заранее ничего не нужно.",
  },
  {
    icon: "encrypted",
    title: "Что будет с паролем",
    body: "Пароль хранится на сервере BotAI в зашифрованном виде. Он нужен, чтобы заходить в кабинет за вас, когда вход устареет. Расшифровать его может только сервер в момент входа. Удалить пароль можно в любой момент.",
  },
  {
    icon: "source",
    title: "Как проходит подключение",
    body: "Логин и пароль вы вводите в полях BotAI, и этот ввод проходит через сервер BotAI. Если вуз просит код из SMS или проверку «я не робот», это делаете вы: обходить такие защиты мы не будем.",
  },
];

/** Онбординг после регистрации: что это, что с паролем, как подключается вуз, и первый шаг. */
export function WelcomePage() {
  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-col flex-col gap-8 px-safe-4 pt-safe-8 xs:px-safe-5">
      <header className="flex flex-col gap-3">
        <Wordmark size="lg" />
        <h1 className="type-display text-fg">Расписание и сроки из всех ваших вузов</h1>
        <p className="max-w-prose type-body text-fg-muted">
          Три минуты на то, чтобы понять, как это работает, и подключить первый вуз.
        </p>
      </header>

      <ul className="flex flex-col gap-3">
        {POINTS.map((point) => (
          <li key={point.title}>
            <Card className="flex flex-col gap-3">
              <div className="flex items-center gap-3">
                <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-accent-subtle text-accent-fg">
                  <Icon name={point.icon} size={24} />
                </span>
                <h2 className="type-subheading text-fg">{point.title}</h2>
              </div>
              <p className="type-body text-fg-muted">{point.body}</p>
            </Card>
          </li>
        ))}
      </ul>

      <div className="sticky bottom-0 mt-auto flex flex-col gap-2 bg-bg pt-3 pb-safe-4 xs:flex-row">
        <Button variant="primary" asChild className="xs:min-w-48">
          <Link to="/connect">Подключить вуз</Link>
        </Button>
        <Button variant="ghost" asChild>
          <Link to="/">Позже</Link>
        </Button>
      </div>
    </main>
  );
}
