import { Banner, Button, Field, Input, PasswordInput } from "@botai/design";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { api, call } from "@/api/client";
import { humanMessage, isApiError, isNetworkError } from "@/api/errors";
import { queryKeys } from "@/api/queries";
import { AuthLayout } from "./auth-layout";

/** Вход в BotAI. Ошибка — что не так и что делать; сеть недоступна — сказано словами, кнопки повтора нет. */
export function LoginPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [error, setError] = useState<unknown>(null);

  const login = useMutation({
    mutationFn: (body: { email: string; password: string }) => call(api.POST("/api/v1/auth/login", { body })),
    onSuccess: async (me) => {
      queryClient.setQueryData(queryKeys.me, me);
      await queryClient.invalidateQueries({ queryKey: queryKeys.spaces });
      await navigate({ to: "/" });
    },
    onError: setError,
  });

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setError(null);
    login.mutate({
      email: String(form.get("email") ?? "").trim(),
      password: String(form.get("password") ?? ""),
    });
  };

  const credentialsWrong = isApiError(error) && error.status === 401;
  const offline = isNetworkError(error);

  return (
    <AuthLayout
      title="Вход"
      lead="Войдите в BotAI, чтобы увидеть расписание и сроки из ваших вузов."
      footer={
        <>
          Есть приглашение?{" "}
          <Link
            to="/register"
            className="inline-flex min-h-11 items-center text-accent-fg underline underline-offset-2"
          >
            Создать аккаунт
          </Link>
        </>
      }
    >
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        {error !== null && !credentialsWrong && (
          <Banner tone={offline ? "info" : "danger"} icon={offline ? "offline" : "error"} live>
            {humanMessage(error)}
          </Banner>
        )}
        <Field label="Почта">
          <Input
            name="email"
            type="email"
            inputMode="email"
            autoComplete="username"
            autoCapitalize="off"
            autoFocus
            required
          />
        </Field>
        <Field label="Пароль" error={credentialsWrong ? humanMessage(error) : undefined}>
          <PasswordInput name="password" autoComplete="current-password" required />
        </Field>
        <Button type="submit" variant="primary" loading={login.isPending}>
          Войти
        </Button>
      </form>
    </AuthLayout>
  );
}
