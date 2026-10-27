import { Button, Field, Input, SecretField, type SecretKind } from "@botai/design";
import { type FormEvent, useState } from "react";
import type { Input as InputBlock } from "@/api/agent-ui.gen";
import type { BlockContext } from "./context";
import { QuestionFrame } from "./question-frame";

const HTML_TYPE: Record<InputBlock["inputKind"], string> = {
  text: "text",
  url: "url",
  number: "text",
  date: "date",
  email: "email",
  password: "password",
  code: "text",
};

const INPUT_MODE: Partial<Record<InputBlock["inputKind"], "url" | "decimal" | "email" | "numeric">> = {
  url: "url",
  number: "decimal",
  email: "email",
  code: "numeric",
};

/** Проверка значения до отправки: только то, что можно объяснить человеком. */
export function validateInput(
  kind: InputBlock["inputKind"],
  value: string,
  required: boolean,
): string | null {
  const text = value.trim();
  if (!text) return required ? "Нужно заполнить это поле" : null;
  if (kind === "url") {
    try {
      const url = new URL(text);
      if (url.protocol !== "https:" && url.protocol !== "http:") return "Нужна ссылка вида https://…";
    } catch {
      return "Нужна ссылка вида https://…";
    }
  }
  if (kind === "number" && !/^-?\d+([.,]\d+)?$/.test(text)) return "Нужно число";
  if (kind === "email" && !/^\S+@\S+\.\S+$/.test(text)) return "Нужна почта вида name@example.ru";
  return null;
}

/** Автозаполнение для секретного поля: только значения, которые умеет `SecretField`. */
function secretAutocomplete(value: InputBlock["autocomplete"], kind: SecretKind) {
  if (value === "current-password" || value === "one-time-code" || value === "off") return value;
  return kind === "code" ? "one-time-code" : "current-password";
}

/**
 * `input`: обычное поле — `Field` + `Input` (кегль 16 px, `inputMode` по виду значения); секретное
 * (`secret: true` или пароль) — `SecretField`: значение живёт только в поле, после отправки поле очищено,
 * в истории остаётся «введено». Секрет уходит напрямую в API, не через кэш запросов.
 */
export function InputCard({ block, ctx }: { block: InputBlock; ctx: BlockContext }) {
  const answer = ctx.answers[block.id];
  const busy = answer?.status === "sending";
  const secret = block.secret === true || block.inputKind === "password";

  if (secret) {
    const kind: SecretKind = block.inputKind === "code" ? "code" : "password";
    return (
      <QuestionFrame question={block.question} required={block.required} answer={answer}>
        <SecretField
          label="Ответ"
          kind={kind}
          autoComplete={secretAutocomplete(block.autocomplete, kind)}
          maxLength={block.maxLength}
          placeholder={block.placeholder}
          required={block.required}
          disabled={busy}
          encryptedNote={kind === "password"}
          onSubmit={(value) => ctx.onAnswer(block.id, value, null)}
        />
        {!block.required && (
          <Button variant="ghost" disabled={busy} onClick={() => ctx.onAnswer(block.id, "", "Пропущено")}>
            Пропустить
          </Button>
        )}
      </QuestionFrame>
    );
  }
  return <PlainInput block={block} ctx={ctx} busy={busy} />;
}

function PlainInput({ block, ctx, busy }: { block: InputBlock; ctx: BlockContext; busy: boolean }) {
  const answer = ctx.answers[block.id];
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const found = validateInput(block.inputKind, value, block.required);
    setError(found);
    if (found) return;
    const text = value.trim();
    ctx.onAnswer(block.id, block.inputKind === "number" ? text.replace(",", ".") : text, text);
  };

  return (
    <QuestionFrame question={block.question} required={block.required} answer={answer}>
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        <Field label="Ответ" error={error ?? undefined}>
          <Input
            type={HTML_TYPE[block.inputKind]}
            inputMode={INPUT_MODE[block.inputKind]}
            autoComplete={block.autocomplete ?? "off"}
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            maxLength={block.maxLength}
            placeholder={block.placeholder}
            value={value}
            disabled={busy}
            onChange={(event) => setValue(event.target.value)}
          />
        </Field>
        <div className="flex flex-col gap-2 xs:flex-row xs:flex-wrap">
          <Button type="submit" variant="primary" loading={busy} className="xs:min-w-40">
            Ответить
          </Button>
          {!block.required && (
            <Button variant="ghost" disabled={busy} onClick={() => ctx.onAnswer(block.id, "", "Пропущено")}>
              Пропустить
            </Button>
          )}
        </div>
      </form>
    </QuestionFrame>
  );
}
