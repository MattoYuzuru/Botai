import { Button, Checkbox, Field, Input, RadioGroup, RadioGroupItem } from "@botai/design";
import { useState } from "react";
import type { Choice } from "@/api/agent-ui.gen";
import type { BlockContext } from "./context";
import { QuestionFrame } from "./question-frame";

const OTHER = "\u0000other";

/**
 * `choice`: одиночный выбор — `RadioGroup`, множественный — `Checkbox`; вся строка — зона нажатия.
 * `allowOther` добавляет «Другое» с полем. У необязательного вопроса есть «Не знаю».
 */
export function ChoiceCard({ block, ctx }: { block: Choice; ctx: BlockContext }) {
  const answer = ctx.answers[block.id];
  const busy = answer?.status === "sending";
  const [single, setSingle] = useState<string>("");
  const [multi, setMulti] = useState<string[]>([]);
  const [other, setOther] = useState("");
  const labelOf = (value: string) => block.options.find((option) => option.value === value)?.label ?? value;

  const otherChosen = block.mode === "single" ? single === OTHER : multi.includes(OTHER);
  const missingOther = otherChosen && !other.trim();
  const canSend = !busy && !missingOther && (block.mode === "single" ? single !== "" : multi.length > 0);

  const send = () => {
    if (block.mode === "single") {
      const text = single === OTHER ? other.trim() : single;
      ctx.onAnswer(block.id, text, single === OTHER ? text : labelOf(single));
    } else {
      const values = multi.map((value) => (value === OTHER ? other.trim() : value));
      const shown = multi.map((value) => (value === OTHER ? other.trim() : labelOf(value))).join(", ");
      ctx.onAnswer(block.id, values, shown);
    }
  };

  const toggle = (value: string, checked: boolean) =>
    setMulti((current) => (checked ? [...current, value] : current.filter((item) => item !== value)));

  return (
    <QuestionFrame question={block.question} required={block.required} answer={answer}>
      {block.mode === "single" ? (
        <RadioGroup aria-label={block.question} value={single} onValueChange={setSingle} disabled={busy}>
          {block.options.map((option) => (
            <RadioGroupItem
              key={option.value}
              value={option.value}
              label={option.label}
              description={option.hint}
            />
          ))}
          {block.allowOther && <RadioGroupItem value={OTHER} label="Другое" />}
        </RadioGroup>
      ) : (
        <fieldset aria-label={block.question} className="m-0 flex min-w-0 flex-col gap-1 border-0 p-0">
          {block.options.map((option) => (
            <Checkbox
              key={option.value}
              label={option.label}
              description={option.hint}
              checked={multi.includes(option.value)}
              onCheckedChange={(checked) => toggle(option.value, checked === true)}
              disabled={busy}
            />
          ))}
          {block.allowOther && (
            <Checkbox
              label="Другое"
              checked={multi.includes(OTHER)}
              onCheckedChange={(checked) => toggle(OTHER, checked === true)}
              disabled={busy}
            />
          )}
        </fieldset>
      )}
      {otherChosen && (
        <Field label="Своё значение">
          <Input
            value={other}
            onChange={(event) => setOther(event.target.value)}
            maxLength={120}
            disabled={busy}
          />
        </Field>
      )}
      <div className="flex flex-col gap-2 xs:flex-row xs:flex-wrap">
        <Button variant="primary" loading={busy} disabled={!canSend} onClick={send} className="xs:min-w-40">
          Ответить
        </Button>
        {!block.required && (
          <Button variant="ghost" disabled={busy} onClick={() => ctx.onAnswer(block.id, null, "Не знаю")}>
            Не знаю
          </Button>
        )}
      </div>
    </QuestionFrame>
  );
}
