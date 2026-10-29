import {
  Badge,
  Button,
  Card,
  Checkbox,
  EmptyState,
  Field,
  IconButton,
  Input,
  RadioGroup,
  SecretField,
  Select,
  Skeleton,
  SkeletonText,
  SourceStatus,
  SourceStatusBar,
  Swatch,
  Switch,
  SyncIndicator,
  Textarea,
  toast,
} from "@botai/design";
import { useState } from "react";
import { MSK, NOW } from "./fixtures";
import { Demo, Grid, Section } from "./layout";

const SCALES = ["hse", "azure", "pine", "amber", "cranberry"] as const;
const SCALE_NAMES: Record<(typeof SCALES)[number], string> = {
  hse: "ВШЭ (акцент)",
  azure: "Лазурь (справка)",
  pine: "Хвоя (сдано)",
  amber: "Янтарь (внимание)",
  cranberry: "Клюква (ошибка)",
};
const STEPS = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950] as const;
const PAPER = [0, 50, 100, 150, 200, 300] as const;
const SLATE = [300, 400, 500, 600, 700, 750, 800, 850, 900, 925, 950] as const;

const SEMANTIC: { group: string; tokens: string[]; against?: string }[] = [
  {
    group: "Поверхности",
    tokens: ["bg", "bg-subtle", "surface", "surface-raised", "border", "border-strong", "control"],
  },
  {
    group: "Текст",
    tokens: ["fg", "fg-muted", "fg-subtle", "accent-fg", "info-fg", "success-fg", "warning-fg", "danger-fg"],
    against: "--color-surface",
  },
  {
    group: "Заливки",
    tokens: ["accent", "accent-hover", "accent-subtle", "danger", "danger-hover", "danger-subtle"],
  },
  {
    group: "Статусы",
    tokens: ["info", "success", "warning", "info-subtle", "success-subtle", "warning-subtle", "focus-ring"],
  },
];

export function PaletteSection() {
  return (
    <Section
      id="palette"
      title="Палитра"
      note="Шкалы построены в OKLCH, hex и контраст считаются из вычисленных токенов в текущей теме. Контраст — к фону приложения, у текста — к поверхности."
    >
      {SEMANTIC.map((group) => (
        <Demo key={group.group} label={group.group} wide>
          <div className="grid grid-cols-[repeat(auto-fill,minmax(7rem,1fr))] gap-3">
            {group.tokens.map((token) => (
              <Swatch key={token} token={`--color-${token}`} against={group.against} />
            ))}
          </div>
        </Demo>
      ))}
      {SCALES.map((scale) => (
        <Demo key={scale} label={SCALE_NAMES[scale]} wide>
          <div className="grid grid-cols-[repeat(auto-fill,minmax(5.5rem,1fr))] gap-3">
            {STEPS.map((step) => (
              <Swatch key={step} token={`--palette-${scale}-${step}`} label={String(step)} />
            ))}
          </div>
        </Demo>
      ))}
      <Demo label="Бумага и Грифель (нейтрали)" wide>
        <div className="grid grid-cols-[repeat(auto-fill,minmax(5.5rem,1fr))] gap-3">
          {PAPER.map((step) => (
            <Swatch key={`p${step}`} token={`--palette-paper-${step}`} label={`бумага ${step}`} />
          ))}
          {SLATE.map((step) => (
            <Swatch key={`s${step}`} token={`--palette-slate-${step}`} label={`грифель ${step}`} />
          ))}
        </div>
      </Demo>
    </Section>
  );
}

const ROLES = [
  ["type-display", "Первый запуск · 26/32 · 600", "Подключите вуз"],
  ["type-title", "Заголовок экрана · 20/26 · 600", "Расписание"],
  ["type-heading", "Заголовок шита, раздела · 17/24 · 600", "Сегодня, вторник"],
  ["type-subheading", "Заголовок карточки · 15/20 · 600", "Алгоритмы и структуры данных"],
  ["type-body", "Чат, длинный текст · 16/24 · 400", "Следующая пара — Алгоритмы в 11:10, ауд. 214."],
  ["type-ui", "Основной текст интерфейса · 15/20 · 400", "Обновлено 5 минут назад"],
  ["type-label", "Подписи полей и вкладок · 13/16 · 500", "Адрес личного кабинета"],
  ["type-caption", "Метаданные · 13/16 · 400", "ВШЭ · Данные от 24 октября"],
  ["type-mono", "Коды, аудитории · 14/20 · Geist Mono", "Ауд. 305 · 08:30 · 11:45"],
] as const;

export function TypographySection() {
  return (
    <Section
      id="typography"
      title="Типографика"
      note="Geist и Geist Mono с кириллицей, локально. Минимум 13 px, поле ввода и чат — 16 px, цифры табличные."
    >
      <Card className="flex flex-col gap-4">
        {ROLES.map(([role, spec, sample]) => (
          <div key={role} className="flex flex-col gap-1 border-b border-border pb-4 last:border-0 last:pb-0">
            <p className="type-caption text-fg-subtle">
              {role} · {spec}
            </p>
            <p className={role}>{sample}</p>
          </div>
        ))}
      </Card>
    </Section>
  );
}

export function ControlsSection() {
  const [checked, setChecked] = useState<boolean | "indeterminate">("indeterminate");
  const [choice, setChoice] = useState("b");
  const [city, setCity] = useState<string | undefined>("msk");
  const [calm, setCalm] = useState(false);
  const [text, setText] = useState("");
  return (
    <Section
      id="controls"
      title="Действия и ввод"
      note="Одна заливка accent на экран. Кнопка и поле — 44 px, поля 16 px. Кнопки «Повторить» и «Обновить» не существует."
    >
      <Demo label="Button — primary / secondary / ghost">
        <Button variant="primary">Подключить вуз</Button>
        <Button>Ответить</Button>
        <Button variant="ghost">Пропустить</Button>
      </Demo>
      <Demo label="Button — иконки, danger, disabled, loading, sm (36 px с зоной 44)">
        <Button variant="primary" leadingIcon="plus">
          Добавить источник
        </Button>
        <Button variant="secondary" trailingIcon="forward">
          Дальше
        </Button>
        <Button variant="primary" tone="danger">
          Отключить вуз
        </Button>
        <Button variant="secondary" tone="danger">
          Удалить пароль
        </Button>
        <Button disabled>Недоступно</Button>
        <Button variant="primary" loading>
          Подключаю
        </Button>
        <Button size="sm" data-touch-expanded="">
          Компактная
        </Button>
        <Button variant="ghost" size="sm" data-touch-expanded="">
          Ещё
        </Button>
      </Demo>
      <Demo label="IconButton — квадратная и круглая, подпись обязательна">
        <IconButton icon="more" label="Ещё" />
        <IconButton icon="close" label="Закрыть" variant="secondary" />
        <IconButton icon="send" label="Отправить" variant="primary" shape="round" />
        <IconButton icon="trash" label="Удалить" tone="danger" />
        <IconButton icon="copy" label="Скопировать" disabled />
      </Demo>

      <Grid>
        <Demo label="Field + Input — покой, подсказка" wide>
          <Field label="Адрес личного кабинета" hint="Откуда вы обычно заходите в кабинет вуза">
            <Input placeholder="https://" inputMode="url" />
          </Field>
        </Demo>
        <Demo label="Field + Input — ошибка (цвет и текст с иконкой)" wide>
          <Field label="Адрес личного кабинета" error="Нужна ссылка вида https://lk.example.ru">
            <Input defaultValue="lk.example" />
          </Field>
        </Demo>
        <Demo label="Input — иконка и необязательное поле" wide>
          <Field label="Поиск вуза" optional>
            <Input leadingIcon="search" placeholder="Название или сайт" />
          </Field>
        </Demo>
        <Demo label="Input — disabled и readOnly" wide>
          <div className="flex flex-col gap-3">
            <Field label="Вуз" disabled>
              <Input defaultValue="Высшая школа экономики" />
            </Field>
            <Field label="Почта">
              <Input defaultValue="student@example.ru" readOnly />
            </Field>
          </div>
        </Demo>
        <Demo label="Textarea — растёт между 1 и 6 строками" wide>
          <Field label="Что поменялось в расписании?">
            <Textarea
              value={text}
              onChange={(event) => setText(event.target.value)}
              minRows={2}
              placeholder="Опишите своими словами"
            />
          </Field>
        </Demo>
        <Demo label="Select — на телефоне раскрывается шитом" wide>
          <Field label="Часовой пояс пространства">
            <Select
              value={city}
              onValueChange={setCity}
              sheetTitle="Часовой пояс"
              options={[
                { value: "msk", label: "Москва (UTC+3)" },
                { value: "ekb", label: "Екатеринбург (UTC+5)" },
                { value: "nsk", label: "Новосибирск (UTC+7)" },
                { value: "vvo", label: "Владивосток (UTC+10)", disabled: true },
              ]}
            />
          </Field>
        </Demo>
      </Grid>

      <Grid>
        <Demo label="Checkbox — частично, выбрано, ошибка, disabled" wide>
          <div className="flex flex-col">
            <Checkbox
              label="Все предметы"
              description="Выбраны не все"
              checked={checked}
              onCheckedChange={setChecked}
            />
            <Checkbox label="Алгоритмы" defaultChecked />
            <Checkbox label="Принимаю условия" invalid />
            <Checkbox label="Недоступно" disabled />
          </div>
        </Demo>
        <Demo label="RadioGroup — внутри вопроса агента" wide>
          <RadioGroup
            aria-label="Какой это вуз"
            value={choice}
            onValueChange={setChoice}
            items={[
              { value: "a", label: "Высшая школа экономики", description: "Основная программа" },
              { value: "b", label: "Центральный университет" },
              { value: "c", label: "Другой вуз", disabled: true },
            ]}
          />
        </Demo>
        <Demo label="Switch — применяется сразу" wide>
          <div className="flex flex-col">
            <Switch
              label="Спокойный режим"
              description="Без декоративного движения"
              checked={calm}
              onCheckedChange={setCalm}
            />
            <Switch label="Уведомления" defaultChecked />
            <Switch label="Недоступно" disabled />
          </div>
        </Demo>
        <Demo label="SecretField — пароль и код, значение не хранится после отправки" wide>
          <div className="flex flex-col gap-6">
            <SecretField
              label="Пароль от личного кабинета"
              onSubmit={() => toast("Пароль отправлен", { tone: "success", description: "Поле очищено." })}
            />
            <SecretField label="Код из SMS" kind="code" submitLabel="Подтвердить" onSubmit={() => {}} />
          </div>
        </Demo>
      </Grid>
    </Section>
  );
}

export function StatusSection() {
  return (
    <Section
      id="status"
      title="Состояния и статус"
      note="Семь состояний данных: loading, empty, ok, stale, offline, reauth, repairing. Пустота не выглядит как «всё сдано»."
    >
      <Demo label="Badge — тона и варианты">
        <Badge>Нейтральный</Badge>
        <Badge tone="accent">ВШЭ</Badge>
        <Badge tone="info" icon="info">
          Источник
        </Badge>
        <Badge tone="success" icon="check">
          Сдано
        </Badge>
        <Badge tone="warning" icon="warning">
          Изменено
        </Badge>
        <Badge tone="danger" icon="error">
          Просрочено
        </Badge>
        <Badge tone="warning" variant="solid">
          Осталось 5 ч
        </Badge>
        <Badge tone="accent" variant="outline" dot>
          Подключено
        </Badge>
        <Badge tone="warning" icon="help">
          Не уверен
        </Badge>
      </Demo>
      <Grid>
        <Demo label="SourceStatus inline — шесть состояний" wide>
          <div className="flex flex-col gap-3">
            {(["ok", "syncing", "stale", "offline", "reauth", "repairing"] as const).map((state) => (
              <SourceStatus
                key={state}
                state={state}
                spaceId="hse"
                spaceTitle="ВШЭ"
                updatedAt={state === "ok" || state === "syncing" ? NOW - 5 * 60_000 : NOW - 3 * 86_400_000}
                nowMs={NOW}
                timeZone={MSK}
                onReauth={() => {}}
              />
            ))}
          </div>
        </Demo>
        <Demo label="SourceStatus banner — полосы" wide>
          <div className="flex flex-col gap-3">
            {(["offline", "reauth", "repairing", "stale"] as const).map((state) => (
              <SourceStatus
                key={state}
                variant="banner"
                state={state}
                spaceId="hse"
                spaceTitle="ВШЭ"
                updatedAt={NOW - 3 * 86_400_000}
                nowMs={NOW}
                timeZone={MSK}
                onReauth={() => {}}
              />
            ))}
          </div>
        </Demo>
      </Grid>
      <Demo
        label="SourceStatusBar — статусы вузов в шапке; пояснение раскрывается плавно по наведению, фокусу и касанию"
        wide
      >
        <SourceStatusBar
          nowMs={NOW}
          items={[
            { id: "hse", title: "НИУ ВШЭ", state: "ok", updatedAt: NOW - 20 * 60_000, timeZone: MSK },
            { id: "cu", title: "ЦУ", state: "reauth", updatedAt: NOW - 3 * 86_400_000, timeZone: MSK },
            { id: "su", title: "СУ", state: "syncing", updatedAt: NOW - 5 * 60_000, timeZone: MSK },
          ]}
        />
      </Demo>
      <Demo label="SyncIndicator — точка с мягкой пульсацией; обновление идёт само, кнопки нет">
        <span className="inline-flex items-center gap-2 type-caption text-fg-muted">
          <SyncIndicator state="syncing" /> обновляется
        </span>
        <span className="inline-flex items-center gap-2 type-caption text-fg-muted">
          <SyncIndicator state="repairing" /> Варя чинит
        </span>
      </Demo>
      <Grid>
        <Demo label="EmptyState — что здесь будет и одно действие" wide>
          <Card variant="sunken">
            <EmptyState
              icon="university"
              title="Вузов пока нет"
              description="Подключите вуз — расписание и сроки соберутся в одном месте."
              action={<Button variant="primary">Подключить вуз</Button>}
            />
          </Card>
        </Demo>
        <Demo label="Skeleton — форма будущего содержимого (loading)" wide>
          <Card className="flex flex-col gap-3" aria-busy="true">
            <Skeleton className="h-5 w-1/2" />
            <SkeletonText lines={3} />
          </Card>
        </Demo>
      </Grid>
      <Grid cols={3}>
        <Demo label="Card — outline" wide>
          <Card>
            <p className="type-subheading text-fg">Outline</p>
            <p className="type-caption text-fg-muted">Группа связанного содержимого</p>
          </Card>
        </Demo>
        <Demo label="Card — elevated" wide>
          <Card variant="elevated">
            <p className="type-subheading text-fg">Elevated</p>
            <p className="type-caption text-fg-muted">Над фоном, тень sm</p>
          </Card>
        </Demo>
        <Demo label="Card — sunken, interactive" wide>
          <Card variant="sunken" interactive asChild>
            <a href="#status">
              <p className="type-subheading text-fg">Sunken</p>
              <p className="type-caption text-fg-muted">Утопленный блок</p>
            </a>
          </Card>
        </Demo>
      </Grid>
    </Section>
  );
}
