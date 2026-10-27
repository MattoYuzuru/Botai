import { type FormEvent, useId, useRef, useState } from "react";
import { cn } from "../../lib/cn";
import { Button } from "../button/button";
import { Field } from "../field/field";
import { Input } from "../input/input";
import { PasswordInput } from "../password-input/password-input";

export type SecretKind = "password" | "code";

export interface SecretFieldProps {
  /** Вопрос или подпись поля: «Пароль от личного кабинета». */
  label: string;
  kind?: SecretKind;
  /** Из блока агента: `current-password`, `one-time-code`, `off`. Нативная семантика — для менеджеров паролей. */
  autoComplete?: "current-password" | "one-time-code" | "off" | "new-password";
  /** Имя поля для менеджера паролей. */
  name?: string;
  placeholder?: string;
  hint?: string;
  /** Ошибка, пришедшая с сервера: «Неверный пароль». */
  error?: string;
  required?: boolean;
  maxLength?: number;
  submitLabel?: string;
  disabled?: boolean;
  /** Показать «Сохраним зашифрованным» (для пароля). */
  encryptedNote?: boolean;
  /** Получает значение один раз; после вызова поле очищается, в состоянии компонента значения нет. */
  onSubmit: (value: string) => unknown;
  className?: string;
}

/**
 * Поле пароля или кода. Значение живёт только в DOM-поле: React-состояние хранит лишь «поле не пусто»
 * и «показано ли». После отправки поле очищается и снова скрыто. Кегль 16 px (iOS не приближает страницу),
 * `autoComplete` — из блока, поэтому менеджер паролей работает. Значение не логируется и не попадает в события.
 */
export function SecretField({
  label,
  kind = "password",
  autoComplete = kind === "code" ? "one-time-code" : "current-password",
  name,
  placeholder,
  hint,
  error,
  required = true,
  maxLength = 500,
  submitLabel = "Ответить",
  disabled = false,
  encryptedNote = kind === "password",
  onSubmit,
  className,
}: SecretFieldProps) {
  const ref = useRef<HTMLInputElement>(null);
  const noteId = useId();
  const [filled, setFilled] = useState(false);
  const [revealed, setRevealed] = useState(false);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const input = ref.current;
    if (!input || (required && !input.value) || busy) return;
    const value = input.value;
    setBusy(true);
    // Поле очищается до того, как значение уйдёт дальше: на экране и в DOM его больше нет.
    input.value = "";
    setFilled(false);
    setRevealed(false);
    try {
      await onSubmit(value);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className={cn("flex flex-col gap-3", className)} autoComplete="on" noValidate>
      <Field label={label} hint={hint} error={error} disabled={disabled}>
        {kind === "password" ? (
          <PasswordInput
            ref={ref}
            name={name ?? "password"}
            autoComplete={autoComplete}
            maxLength={maxLength}
            placeholder={placeholder}
            aria-describedby={encryptedNote ? noteId : undefined}
            revealed={revealed}
            onRevealedChange={setRevealed}
            onChange={(event) => setFilled(event.target.value.length > 0)}
          />
        ) : (
          <Input
            ref={ref}
            name={name ?? "otp"}
            type="text"
            inputMode="numeric"
            autoComplete={autoComplete}
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            maxLength={maxLength}
            placeholder={placeholder}
            onChange={(event) => setFilled(event.target.value.length > 0)}
          />
        )}
      </Field>
      {encryptedNote && (
        <p id={noteId} className="type-caption text-fg-muted">
          Сохраним зашифрованным и используем только для входа в вуз.
        </p>
      )}
      <Button type="submit" variant="primary" loading={busy} disabled={disabled || (required && !filled)}>
        {submitLabel}
      </Button>
    </form>
  );
}
