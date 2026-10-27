import { RadioGroup, SecretField, toast } from "@botai/design";
import { useState } from "react";
import { humanMessage } from "@/api/errors";
import type { Connection } from "@/api/types";
import type { SecretKind } from "@/features/data/types";
import { putSecret, SECRET_KINDS } from "./connection-actions";

const ICAL = /^(https?|webcal):\/\//i;

/**
 * Ссылка на календарь, ключ доступа или ключ сервиса. Значение вводится в секретное поле, уходит один раз и
 * больше не показывается: после сохранения экран говорит только «сохранено». Ошибка — у поля, словами сервера.
 */
export function SecretForm({ connection, onSaved }: { connection: Connection; onSaved: () => void }) {
  const [kind, setKind] = useState<SecretKind>("ical_url");
  const [error, setError] = useState<string | undefined>();
  const current = SECRET_KINDS.find((row) => row.value === kind) ?? SECRET_KINDS[0];

  const save = async (value: string) => {
    setError(undefined);
    const trimmed = value.trim();
    if (kind === "ical_url" && !ICAL.test(trimmed)) {
      setError("Нужна ссылка на календарь: https:// или webcal://. Вставьте её ещё раз.");
      return;
    }
    try {
      await putSecret(connection.id, kind, trimmed);
    } catch (cause) {
      setError(humanMessage(cause));
      return;
    }
    toast(current?.saved ?? "Сохранено", {
      tone: "success",
      description: "BotAI не показывает сохранённое значение. Обновление подхватит его само.",
    });
    onSaved();
  };

  return (
    <div className="flex flex-col gap-3" data-secret-form="">
      <RadioGroup
        aria-label="Что добавить"
        value={kind}
        onValueChange={(value) => {
          setKind(value as SecretKind);
          setError(undefined);
        }}
        items={SECRET_KINDS.map((row) => ({
          value: row.value,
          label: row.label,
          description: row.description,
        }))}
      />
      <SecretField
        key={kind}
        label={current?.field ?? "Значение"}
        hint={current?.hint}
        kind="password"
        name="secret"
        autoComplete="off"
        encryptedNote={false}
        maxLength={2000}
        submitLabel="Сохранить"
        error={error}
        onSubmit={save}
      />
      <p className="type-caption text-fg-muted">
        BotAI хранит значение зашифрованным и больше не показывает его: ни здесь, ни в чате.
      </p>
    </div>
  );
}
