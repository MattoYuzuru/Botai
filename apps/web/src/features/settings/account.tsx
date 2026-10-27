import { Button, Card, toast } from "@botai/design";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { api, call } from "@/api/client";
import { humanMessage } from "@/api/errors";
import { meQuery } from "@/api/queries";
import { AppearanceSettings } from "./appearance";

/** «Профиль и настройки» — внизу экрана «Учёба» (styleguide §7.1). */
export function AccountSettings() {
  const me = useQuery(meQuery);
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const logout = useMutation({
    mutationFn: () => call(api.POST("/api/v1/auth/logout")),
    onSuccess: async () => {
      // Прежние данные не должны пережить выход: следующий вход начинается с чистого кэша.
      queryClient.clear();
      await navigate({ to: "/login" });
    },
    onError: (error) => toast("Не удалось выйти", { tone: "warning", description: humanMessage(error) }),
  });

  return (
    <section aria-labelledby="account-title" className="flex max-w-col flex-col gap-3">
      <h2 id="account-title" className="type-heading text-fg">
        Профиль и настройки
      </h2>
      <Card padding="sm" className="flex flex-col gap-4">
        <div className="flex flex-col gap-1 px-3 pt-3">
          <p className="type-subheading text-fg">{me.data?.user.displayName}</p>
          <p className="type-caption text-fg-muted">{me.data?.user.email}</p>
        </div>
        <AppearanceSettings />
        <div className="px-3 pb-3">
          <Button
            variant="secondary"
            leadingIcon="logout"
            loading={logout.isPending}
            onClick={() => logout.mutate()}
          >
            Выйти из BotAI
          </Button>
        </div>
      </Card>
    </section>
  );
}
