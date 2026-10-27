import { EmptyState } from "@botai/design";
import { createFileRoute, useRouter } from "@tanstack/react-router";
import { Screen } from "@/app/screen";
import { requireSession } from "@/app/session";

/** Заглушка до среза «Подключение вуза»: маршрут нужен онбордингу и переключателю вузов. */
export const Route = createFileRoute("/connect")({
  beforeLoad: ({ context }) => requireSession(context.queryClient),
  component: ConnectPlaceholder,
});

function ConnectPlaceholder() {
  const router = useRouter();
  return (
    <Screen title="Подключение вуза" leading="back" onBack={() => router.history.back()}>
      <EmptyState
        icon="connection"
        title="Подключение появится скоро"
        description="Выбор вуза и вход в личный кабинет готовятся."
      />
    </Screen>
  );
}
