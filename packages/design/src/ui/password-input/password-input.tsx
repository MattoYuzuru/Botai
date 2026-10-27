import { useState } from "react";
import { IconButton } from "../icon-button/icon-button";
import { Input, type InputProps } from "../input/input";

export interface PasswordInputProps extends Omit<InputProps, "type" | "trailing"> {
  /** Управляемый показ пароля; без него компонент хранит флаг сам. */
  revealed?: boolean;
  onRevealedChange?: (revealed: boolean) => void;
}

/**
 * Поле пароля с кнопкой «Показать / Скрыть». Кегль 16 px, `autoComplete` задаёт вызывающий код
 * (`current-password` для входа, `new-password` для регистрации) — менеджеры паролей работают.
 */
export function PasswordInput({ revealed, onRevealedChange, disabled, ...props }: PasswordInputProps) {
  const [inner, setInner] = useState(false);
  const shown = revealed ?? inner;
  return (
    <Input
      {...props}
      type={shown ? "text" : "password"}
      autoCapitalize="off"
      autoCorrect="off"
      spellCheck={false}
      disabled={disabled}
      trailing={
        <IconButton
          icon={shown ? "hide" : "show"}
          label={shown ? "Скрыть пароль" : "Показать пароль"}
          aria-pressed={shown}
          size="sm"
          disabled={disabled}
          onClick={() => {
            onRevealedChange?.(!shown);
            setInner(!shown);
          }}
        />
      }
    />
  );
}
