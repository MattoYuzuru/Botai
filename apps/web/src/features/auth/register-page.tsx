import { Banner, Button, Field, Input, PasswordInput } from "@botai/design";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { api, call } from "@/api/client";
import { humanMessage, isApiError, isNetworkError } from "@/api/errors";
import { queryKeys } from "@/api/queries";
import { AuthLayout } from "./auth-layout";

const MIN_PASSWORD = 8;

interface Values {
  displayName: string;
  email: string;
  password: string;
}

type FieldErrors = Partial<Record<keyof Values, string>>;

/** Проверка на клиенте: только то, что можно сказать человеку до запроса. Остальное проверяет сервер. */
export function validateRegistration(values: Values): FieldErrors {
  const errors: FieldErrors = {};
  if (!values.displayName.trim()) errors.displayName = "Как к вам обращаться?";
  if (!/^\S+@\S+\.\S+$/.test(values.email.trim()))
    errors.email = "Введите почту целиком, например name@example.ru";
  if (values.password.length < MIN_PASSWORD) errors.password = `Пароль — не меньше ${MIN_PASSWORD} знаков`;
  return errors;
}

/** Открытая регистрация: имя, почта и пароль BotAI, без приглашения или одобрения. */
export function RegisterPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [errors, setErrors] = useState<FieldErrors>({});
  const [failure, setFailure] = useState<unknown>(null);

  const register = useMutation({
    mutationFn: (body: Values) => call(api.POST("/api/v1/auth/register", { body })),
    onSuccess: async (me) => {
      queryClient.setQueryData(queryKeys.me, me);
      await navigate({ to: "/welcome" });
    },
    onError: (error) => {
      // Ответ сервера привязываем к полю, к которому он относится.
      if (isApiError(error) && error.status === 409) return setErrors({ email: humanMessage(error) });
      setFailure(error);
    },
  });

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const values: Values = {
      displayName: String(form.get("displayName") ?? "").trim(),
      email: String(form.get("email") ?? "").trim(),
      password: String(form.get("password") ?? ""),
    };
    const found = validateRegistration(values);
    setErrors(found);
    setFailure(null);
    if (Object.keys(found).length === 0) register.mutate(values);
  };

  return (
    <AuthLayout
      title="Регистрация"
      lead="Создайте аккаунт, чтобы пользоваться BotAI. Приглашение не нужно."
      footer={
        <>
          Уже есть аккаунт?{" "}
          <Link
            to="/login"
            className="inline-flex min-h-11 items-center text-accent-fg underline underline-offset-2"
          >
            Войти
          </Link>
        </>
      }
    >
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        {failure !== null && (
          <Banner
            tone={isNetworkError(failure) ? "info" : "danger"}
            icon={isNetworkError(failure) ? "offline" : "error"}
            live
          >
            {humanMessage(failure)}
          </Banner>
        )}
        <Field label="Как к вам обращаться" error={errors.displayName}>
          <Input name="displayName" autoComplete="name" />
        </Field>
        <Field label="Почта" error={errors.email}>
          <Input name="email" type="email" inputMode="email" autoComplete="username" autoCapitalize="off" />
        </Field>
        <Field
          label="Пароль"
          hint={`Не меньше ${MIN_PASSWORD} знаков. Это пароль от BotAI, не от вуза.`}
          error={errors.password}
        >
          <PasswordInput name="password" autoComplete="new-password" />
        </Field>
        <Button type="submit" variant="primary" loading={register.isPending}>
          Создать аккаунт
        </Button>
      </form>
    </AuthLayout>
  );
}
