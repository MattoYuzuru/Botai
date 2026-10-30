import {
  Badge,
  Button,
  ChatComposer,
  MASCOT_SITUATIONS,
  MASCOT_SKINS,
  Mascot,
  MascotBubble,
  MascotDock,
  type MascotHandle,
  type MascotSituationId,
  type MascotSkinId,
  Select,
  Switch,
  useMascotQuiet,
} from "@botai/design";
import { useRef, useState } from "react";

export const MASCOT_LABELS: Record<MascotSituationId, string> = {
  connect: "Знакомится с вузом",
  tip: "Подсказывает",
  deadline: "Срок близко",
  skips: "Обращает внимание",
  night: "Пора отдохнуть",
  class: "Скоро занятие",
  done: "Радуется за вас",
  sync: "Собирает обновления",
  think: "Ищет ответ",
  overdue: "Срок прошёл",
  offline: "Нет сети",
  reauth: "Нужен вход",
  error: "Не получилось",
  idle: "Просто рядом",
  perch: "Слушает",
  wave: "Приветствует",
};

/** Живое превью тех же компонентов, которые работают в продукте. Данные только демонстрационные. */
export function MascotPlayground() {
  const [situation, setSituation] = useState<MascotSituationId>("idle");
  const [skin, setSkin] = useState<MascotSkinId | null>(null);
  const [paused, setPaused] = useState(false);
  const [sent, setSent] = useState(false);
  const [value, setValue] = useState("");
  const handle = useRef<MascotHandle>(null);
  const quiet = useMascotQuiet();
  const still =
    paused || quiet.reducedMotion || quiet.preferences.motion === "calm" || quiet.lite || !quiet.visible;
  const motion = still ? "static" : "animated";

  return (
    <div className="flex flex-col gap-6" data-mascot-playground="">
      <div className="grid overflow-hidden rounded-xl border border-border bg-surface md:grid-cols-2">
        <div className="flex flex-col items-center justify-center gap-4 bg-accent-subtle p-6">
          <Badge tone="neutral">Варя · учебный напарник</Badge>
          <Mascot
            ref={handle}
            situation={situation}
            skin={skin}
            motion={motion}
            size="fill"
            className="size-48"
          />
          <p className="type-subheading text-fg" aria-live="polite">
            {MASCOT_LABELS[situation]}
          </p>
          <div className="flex flex-wrap justify-center gap-2">
            <Button variant="secondary" disabled={still} onClick={() => handle.current?.act("wave")}>
              Помахать
            </Button>
            <Button variant="ghost" disabled={still} onClick={() => handle.current?.act("nod")}>
              Кивнуть
            </Button>
          </div>
        </div>
        <div className="flex min-w-0 flex-col justify-center gap-6 p-6">
          <div className="flex flex-col gap-2">
            <h3 className="type-title text-fg">Варя рядом. Всё под крылом.</h3>
            <p className="type-ui text-fg-muted">
              Знакомится с вузом, ищет ответы и замечает важное. Во время ввода замирает и даёт
              сосредоточиться.
            </p>
          </div>
          <div className="flex flex-col gap-2">
            <p className="type-label text-fg-muted">Состояние</p>
            <Select
              aria-label="Состояние Вари"
              value={situation}
              onValueChange={(value) => {
                const next = MASCOT_SITUATIONS.find((id) => id === value);
                if (next) setSituation(next);
              }}
              options={MASCOT_SITUATIONS.map((id) => ({ value: id, label: MASCOT_LABELS[id] }))}
            />
          </div>
          <div className="flex flex-col gap-2">
            <p className="type-label text-fg-muted">Образ</p>
            <Select
              aria-label="Образ Вари"
              value={skin ?? "base"}
              onValueChange={(value) => setSkin(MASCOT_SKINS.find((id) => id === value) ?? null)}
              options={[
                { value: "base", label: "На каждый день" },
                { value: "new-year", label: "Новогодний колпак" },
                { value: "mug", label: "Сессия с тёплой кружкой" },
                { value: "leaf", label: "Первый учебный день" },
              ]}
            />
          </div>
          <Switch
            label="Без движения"
            description="Тот же характер, статичная поза"
            checked={paused}
            onCheckedChange={setPaused}
          />
        </div>
      </div>
      <div className="grid gap-6 md:grid-cols-2">
        <div className="flex min-w-0 flex-col gap-3 rounded-xl border border-border bg-surface p-4">
          <div className="flex items-center justify-between gap-3">
            <p className="type-subheading text-fg">С вами в чате</p>
            <Badge tone="neutral">Демо</Badge>
          </div>
          <div className="flex min-h-64 flex-col justify-between gap-6 pb-4">
            <p className="max-w-reading type-ui text-fg-muted">
              {sent
                ? "Варя пересела на кромку поля. Здесь она слушает и отражает работу агента."
                : "В пустом чате Варя знакомится с вами. Отправьте любое сообщение, чтобы увидеть прыжок на насест."}
            </p>
            <div className="relative">
              <ChatComposer
                value={value}
                onValueChange={setValue}
                onSend={() => {
                  setSent(true);
                  setValue("");
                }}
              />
              <MascotDock
                mode={sent ? "perch" : "floor"}
                situation={sent ? "think" : "idle"}
                skin={skin}
                motion={value || still ? "static" : "animated"}
                seed={7}
                bubble={
                  !value && (
                    <MascotBubble
                      text={sent ? "Ищу в подключённых источниках" : "С чего начнём?"}
                      tail={sent ? "right" : "center"}
                    />
                  )
                }
              />
            </div>
          </div>
          <Button
            variant="ghost"
            onClick={() => {
              setSent((current) => !current);
              setValue("");
            }}
          >
            {sent ? "Вернуть на сцену" : "Показать насест"}
          </Button>
        </div>
        <div className="flex flex-col justify-between gap-6 rounded-xl bg-bg-subtle p-6">
          <div className="flex flex-col gap-2">
            <h3 className="type-subheading text-fg">Один персонаж, разный масштаб</h3>
            <p className="type-ui text-fg-muted">
              На сцене — жесты и детали. В строке статуса — силуэт и взгляд. Смысл всегда остаётся в тексте.
            </p>
          </div>
          <section className="flex flex-wrap items-end gap-6" aria-label="Масштабы Вари">
            {[20, 36, 56, 96].map((size) => (
              <div key={size} className="flex flex-col items-center gap-2">
                <Mascot situation={situation} skin={skin} size={size} motion="static" />
                <span className="type-caption text-fg-muted">{size} px</span>
              </div>
            ))}
          </section>
          <p className="type-caption text-fg-subtle">
            Сезонные образы в приложении включаются по календарю и отключаются в настройках.
          </p>
        </div>
      </div>
    </div>
  );
}
