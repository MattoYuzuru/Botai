import { Button, Field, Input, SecretField, type SecretKind } from "@botai/design";
import { type FormEvent, useState } from "react";
import type { Input as InputBlock } from "@/api/agent-ui.gen";
import { isAuthInput } from "@/features/jobs/job-state";
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
  const manual = isAuthInput(block) ? ctx.manualLogin : undefined;
  const busy = answer?.status === "sending" || manual?.busy === true;
  const secret = block.secret === true || block.inputKind === "password";

  if (secret) {
    const kind: SecretKind = block.inputKind === "code" ? "code" : "password";
    return (
      <QuestionFrame
        question={block.question}
        required={block.required}
        answer={answer}
        editorOnly={ctx.editorOnly}
      >
        {manual && <p className="type-caption text-fg-muted">Вход через поля BotAI</p>}
        <SecretField
          label="Ответ"
          kind={kind}
          autoComplete={secretAutocomplete(block.autocomplete, kind)}
          maxLength={block.maxLength}
          placeholder={block.placeholder}
          required={block.required}
          disabled={busy}
          encryptedNote={kind === "password"}
          onSubmit={(value) => {
            if (!busy) ctx.onAnswer(block.id, value, null);
          }}
        />
        {!block.required && (
          <Button variant="ghost" disabled={busy} onClick={() => ctx.onAnswer(block.id, "", "Пропущено")}>
            Пропустить
          </Button>
        )}
        <ManualLoginOption blockId={block.id} manual={manual} busy={busy} />
      </QuestionFrame>
    );
  }
  return <PlainInput block={block} ctx={ctx} busy={busy} />;
}

function PlainInput({ block, ctx, busy }: { block: InputBlock; ctx: BlockContext; busy: boolean }) {
  const answer = ctx.answers[block.id];
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const manual = isAuthInput(block) ? ctx.manualLogin : undefined;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    const found = validateInput(block.inputKind, value, block.required);
    setError(found);
    if (found) return;
    const text = value.trim();
    ctx.onAnswer(block.id, block.inputKind === "number" ? text.replace(",", ".") : text, text);
  };

  return (
    <QuestionFrame
      question={block.question}
      required={block.required}
      answer={answer}
      editorOnly={ctx.editorOnly}
    >
      {manual && <p className="type-caption text-fg-muted">Вход через поля BotAI</p>}
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
          <Button type="submit" variant="primary" loading={busy} disabled={busy} className="xs:min-w-40">
            Ответить
          </Button>
          {!block.required && (
            <Button variant="ghost" disabled={busy} onClick={() => ctx.onAnswer(block.id, "", "Пропущено")}>
              Пропустить
            </Button>
          )}
        </div>
      </form>
      <ManualLoginOption blockId={block.id} manual={manual} busy={busy} />
    </QuestionFrame>
  );
}

function ManualLoginOption({
  blockId,
  manual,
  busy,
}: {
  blockId: string;
  manual: BlockContext["manualLogin"];
  busy: boolean;
}) {
  if (!manual) return null;
  return (
    <div className="flex flex-col items-start gap-2">
      <Button type="button" variant="secondary" disabled={busy} onClick={() => manual.onOpen(blockId)}>
        Открыть браузер
      </Button>
      {manual.busy && (
        <p role="status" className="type-caption text-fg-muted">
          Открываю браузер…
        </p>
      )}
      {manual.error && (
        <p role="alert" className="type-caption text-danger-fg">
          {manual.error}
        </p>
      )}
    </div>
  );
}
