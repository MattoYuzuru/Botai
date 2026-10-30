import {
  Badge,
  Button,
  Card,
  ChatComposer,
  ChatMessage,
  EmptyState,
  MASCOT_SITUATIONS,
  MASCOT_SKINS,
  Mascot,
  MascotAnimationPolicy,
  type MascotContext,
  MascotDock,
  type MascotIntent,
  MascotStage,
  ProgressRail,
  resolveMascot,
  SegmentedControl,
  Select,
  Switch,
  SyncIndicator,
  toast,
  useMascotQuiet,
} from "@botai/design";
import { useCallback, useMemo, useState } from "react";
import { WallMascot } from "@/features/connect/wall-mascot";
import {
  BASE,
  CHECK_FIXTURES,
  deadline,
  type MascotFixture,
  MSK,
  msk,
  NOW,
  PRIORITY_FIXTURES,
  snapshot,
  URGENT,
} from "@/features/mascot/fixtures";
import { ProgressWall } from "@/widgets/progress-wall/progress-wall";
import { DemoJobManager, SHOWCASE_JOBS } from "./fixtures-agent";
import { Demo, Grid, Section } from "./layout";
import { MASCOT_LABELS, MascotPlayground } from "./mascot-playground";

const onIntent = (intent: MascotIntent) => toast(`Действие: ${intent.label}`, { description: intent.kind });

/**
 * Тихие режимы системы и настроек накладываются на фикстуру: «спокойный маскот» из настроек витрины действует
 * на все карточки, даже если фикстура сама задаёт сезонность.
 */
function useFixtureContext(extra: Partial<MascotContext>): MascotContext {
  const quiet = useMascotQuiet();
  return useMemo(() => {
    const calm = quiet.preferences.motion === "calm" || extra.preferences?.motion === "calm";
    return {
      ...BASE,
      reducedMotion: quiet.reducedMotion,
      lite: quiet.lite,
      visible: quiet.visible,
      ...extra,
      preferences: {
        seasonal: extra.preferences?.seasonal ?? quiet.preferences.seasonal,
        motion: calm ? ("calm" as const) : ("system" as const),
      },
    };
  }, [quiet.reducedMotion, quiet.lite, quiet.visible, quiet.preferences, extra]);
}

function Resolved({ context }: { context: MascotContext }) {
  const presentation = resolveMascot(context);
  return (
    <p className="type-caption text-fg-subtle" data-testid="resolved">
      {presentation.situation} · {presentation.motion}
      {presentation.skin ? ` · ${presentation.skin}` : ""}
      {presentation.visible ? "" : " · без картинки"}
    </p>
  );
}

function FixtureCard({ fixture }: { fixture: MascotFixture }) {
  const context = useFixtureContext(fixture.context);
  const presentation = useMemo(() => resolveMascot(context), [context]);
  return (
    <Card className="flex flex-col gap-3" data-fixture={fixture.id}>
      <div className="flex flex-col gap-1">
        <h3 className="type-subheading text-fg">{fixture.title}</h3>
        <p className="type-caption text-fg-muted">{fixture.expect}</p>
      </div>
      <MascotStage presentation={presentation} onIntent={onIntent} seed={1} />
      <Resolved context={context} />
    </Card>
  );
}

/** Сдано один раз: реакция играется и подтверждается, после перезагрузки (подтверждения) не повторяется. */
function DoneOnceDemo() {
  const [acknowledged, setAcknowledged] = useState<string[]>([]);
  const [round, setRound] = useState(1);
  const event = useMemo(
    () => ({
      id: `hw-${round}`,
      spaceId: "hse",
      kind: "deadline-done" as const,
      at: NOW - 5_000,
      title: "Домашнее задание 3",
    }),
    [round],
  );
  const context = useFixtureContext(
    useMemo(
      () => ({ snapshot: URGENT, recentEvent: event, acknowledgedEventIds: acknowledged }),
      [event, acknowledged],
    ),
  );
  const presentation = resolveMascot(context);
  const acknowledge = useCallback((id: string) => setAcknowledged((current) => [...current, id]), []);
  return (
    <Card className="flex flex-col gap-3" data-fixture="done-once">
      <div className="flex flex-col gap-1">
        <h3 className="type-subheading text-fg">done-once</h3>
        <p className="type-caption text-fg-muted">
          Одна реакция ≤ 1.5 с; подтверждённое событие не повторяется (на возврате вкладки и после
          перезагрузки).
        </p>
      </div>
      <MascotStage presentation={presentation} onIntent={onIntent} onReactionEnd={acknowledge} />
      <Resolved context={context} />
      <div>
        <Button variant="secondary" onClick={() => setRound((value) => value + 1)}>
          Сдать ещё работу
        </Button>
      </div>
    </Card>
  );
}

/** ВШЭ, затем ЦУ: снимок и событие прежнего пространства отбрасываются сразу, до нового снимка — unknown. */
function SpaceSwitchDemo() {
  const [space, setSpace] = useState<"hse" | "cu">("hse");
  const context = useFixtureContext(
    useMemo(
      () => ({
        spaceId: space,
        spaceTitle: space === "hse" ? "ВШЭ" : "ЦУ",
        availability: space === "hse" ? ("ready" as const) : ("unknown" as const),
        // Снимок и событие остаются от ВШЭ: чужое пространство они не должны оживить.
        snapshot: URGENT,
        recentEvent: { id: "hw2", spaceId: "hse", kind: "deadline-done" as const, at: NOW - 5_000 },
      }),
      [space],
    ),
  );
  const presentation = resolveMascot(context);
  return (
    <Card className="flex flex-col gap-3" data-fixture="space-switch">
      <div className="flex flex-col gap-1">
        <h3 className="type-subheading text-fg">space-switch</h3>
        <p className="type-caption text-fg-muted">
          Прежние реплика, бейдж и реакция убраны сразу; состояние unknown до нового снимка.
        </p>
      </div>
      <SegmentedControl
        label="Активное пространство"
        value={space}
        onValueChange={setSpace}
        options={[
          { value: "hse", label: "Первый вуз" },
          { value: "cu", label: "Второй вуз" },
        ]}
      />
      <MascotStage presentation={presentation} onIntent={onIntent} />
      <Resolved context={context} />
    </Card>
  );
}

const CALENDAR = [
  { value: "dec-19", label: "19 дек 23:59", nowMs: msk(12, 19, 23, 59) },
  { value: "dec-20", label: "20 дек 00:00", nowMs: msk(12, 20, 0, 0) },
  { value: "jan-10", label: "10 янв 23:59", nowMs: msk(1, 10, 23, 59, 2027) },
  { value: "jan-11", label: "11 янв 00:00", nowMs: msk(1, 11, 0, 0, 2027) },
  { value: "sep-1", label: "1 сентября", nowMs: msk(9, 1, 9, 0) },
] as const;

/** Любой образ просматривается на фикстуре с заданными `nowMs` и часовым поясом — ждать месяца не нужно. */
function CalendarDemo() {
  const [moment, setMoment] = useState<(typeof CALENDAR)[number]["value"]>("dec-19");
  const [zone, setZone] = useState<"msk" | "krsk">("msk");
  const nowMs = CALENDAR.find((item) => item.value === moment)?.nowMs ?? NOW;
  const context = useFixtureContext(
    useMemo(
      () => ({
        nowMs,
        timeZone: zone === "msk" ? MSK : "Asia/Krasnoyarsk",
        // Работа агента не занимает слот головы, а срок остаётся срочным по соседству.
        activity: "thinking" as const,
        snapshot: snapshot([deadline("hw3", "Домашнее задание 3", nowMs + 5 * 3_600_000)]),
      }),
      [nowMs, zone],
    ),
  );
  const presentation = resolveMascot(context);
  return (
    <Card className="flex flex-col gap-3" data-fixture="calendar">
      <div className="flex flex-col gap-1">
        <h3 className="type-subheading text-fg">Календарь образов</h3>
        <p className="type-caption text-fg-muted">
          Окно считается в часовом поясе пространства: с 19 декабря 23:59 на 20 декабря 00:00 в Москве; с
          поясом Красноярска та же минута уже в новом окне. С 11 января колпак уходит, срочность не
          сбрасывается.
        </p>
      </div>
      <Select
        aria-label="Момент"
        value={moment}
        onValueChange={(value) => setMoment(value as typeof moment)}
        options={CALENDAR.map(({ value, label }) => ({ value, label }))}
        sheetTitle="Момент"
      />
      <SegmentedControl
        label="Часовой пояс пространства"
        value={zone}
        onValueChange={setZone}
        options={[
          { value: "msk", label: "Москва" },
          { value: "krsk", label: "Красноярск" },
        ]}
      />
      <MascotStage presentation={presentation} onIntent={onIntent} />
      <Resolved context={context} />
    </Card>
  );
}

/** Экран чата в рамке нужной ширины: сцена пустого чата, прыжок на насест и открытая клавиатура. */
function DockFrame({ width, label, pad }: { width: string; label: string; pad: string }) {
  const quiet = useMascotQuiet();
  const [sent, setSent] = useState(false);
  const [keyboard, setKeyboard] = useState(false);
  const [value, setValue] = useState("");
  const staticMotion =
    quiet.reducedMotion || quiet.preferences.motion === "calm" || quiet.lite || !quiet.visible || keyboard;
  const context = useFixtureContext(
    useMemo(
      () => ({ keyboardOpen: keyboard, activity: sent ? ("thinking" as const) : ("idle" as const) }),
      [keyboard, sent],
    ),
  );
  const presentation = resolveMascot(context);
  return (
    <div className={`flex w-full flex-col gap-3 ${width}`} data-frame={label}>
      <p className="type-label text-fg-muted">{label}</p>
      <div className="flex flex-col rounded-xl bg-bg ring-1 ring-border">
        <div className={`flex min-h-80 flex-col justify-end gap-4 pt-4 pb-6 ${pad}`}>
          {sent ? (
            <>
              <ChatMessage role="user" markdown="Какая следующая пара?" at={NOW - 60_000} timeZone={MSK} />
              <ChatMessage
                role="agent"
                status="streaming"
                markdown="Ищу в расписании ВШЭ, читаю сегодняшний день"
                at={NOW}
                timeZone={MSK}
              />
            </>
          ) : (
            <div className="flex flex-col gap-2">
              <EmptyState
                icon="chat"
                title="Спросите про пары и сроки"
                description="Подсказки строятся из настоящих данных"
                size="sm"
              />
              {/* Пол сцены: полоса над полем ввода зарезервирована, раскладка не прыгает. */}
              <div aria-hidden="true" className="h-30" />
            </div>
          )}
        </div>
        <div className={`flex flex-col pb-3 ${pad}`}>
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
              motion={staticMotion ? "static" : "animated"}
              skin={presentation.skin}
              seed={7}
            />
          </div>
        </div>
      </div>
      <div className="flex flex-wrap gap-3">
        <Button
          variant="secondary"
          onClick={() => setSent((value) => !value)}
          data-testid={`dock-toggle-${label}`}
        >
          {sent ? "Очистить чат" : "Первое сообщение"}
        </Button>
        <Switch label="Клавиатура открыта" checked={keyboard} onCheckedChange={setKeyboard} />
      </div>
    </div>
  );
}

/** Сводка по всем ситуациям renderer: статичные позы и сезонные образы. */
function PosesDemo() {
  return (
    <Demo label="Все ситуации renderer — статичные позы" wide>
      <ul className="grid grid-cols-2 gap-3 xs:grid-cols-4 md:grid-cols-6" aria-label="Ситуации Вари">
        {MASCOT_SITUATIONS.map((situation) => (
          <li
            key={situation}
            className="flex flex-col items-center gap-1 rounded-lg bg-bg-subtle p-3"
            data-pose={situation}
          >
            <Mascot situation={situation} motion="static" size={80} />
            <span className="text-center type-caption text-fg-muted">{MASCOT_LABELS[situation]}</span>
          </li>
        ))}
        <li className="flex flex-col items-center gap-1 rounded-lg bg-bg-subtle p-3" data-pose="pj">
          <Mascot situation="night" theme="pj" motion="static" size={80} />
          <span className="type-caption text-fg-muted">night · pj</span>
        </li>
        {MASCOT_SKINS.map((skin) => (
          <li
            key={skin}
            className="flex flex-col items-center gap-1 rounded-lg bg-bg-subtle p-3"
            data-pose={`skin-${skin}`}
          >
            <Mascot situation="idle" skin={skin} motion="static" size={80} />
            <span className="type-caption text-fg-muted">{skin}</span>
          </li>
        ))}
      </ul>
    </Demo>
  );
}

/** Варя в рельсе ProgressRail и в точке SyncIndicator: «бег на месте» по стадиям, без процентов. */
function RunnerDemo() {
  const quiet = useMascotQuiet();
  const motion =
    quiet.reducedMotion || quiet.preferences.motion === "calm" || quiet.lite || !quiet.visible
      ? "static"
      : "animated";
  return (
    <>
      <Demo label="progress-wall без оценки остатка — Варя бежит по стадиям; процента и срока нет" wide>
        <div className="flex max-w-col flex-col gap-6 px-5">
          <ProgressRail
            stage={2}
            stageCount={5}
            elapsedMs={72_000}
            etaKnown={false}
            calm={motion === "static"}
            runner={<Mascot situation="sync" flip motion={motion} size={40} />}
          />
          <ProgressRail
            stage={0}
            stageCount={null}
            elapsedMs={8_000}
            etaKnown={false}
            calm={motion === "static"}
            runner={<Mascot situation="connect" flip motion={motion} size={40} />}
          />
        </div>
      </Demo>
      <Demo label="progress-wall с вопросом — работа на паузе, «Жду ответа», вопрос доступен" wide>
        <Card className="flex max-w-col items-center gap-3">
          <Mascot situation="tip" motion="static" size={56} />
          <div className="flex flex-col gap-1">
            <Badge tone="warning">Нужен ответ</Badge>
            <p className="type-ui text-fg">Жду ответа: какой это вуз?</p>
          </div>
        </Card>
      </Demo>
      <Demo label="SyncIndicator — точка или Варя в шапке, aria-live только на итог">
        <SyncIndicator state="syncing" />
        <SyncIndicator
          state="syncing"
          size="mascot"
          mascot={
            <Mascot
              situation="sync"
              flip
              motion={motion}
              size="fill"
              detail="compact"
              className="size-full"
            />
          }
        />
      </Demo>
    </>
  );
}

const WALL_STATES = [
  ["reconnecting", "Идёт работа: бежит по рельсу на месте, стадия и время — текстом"],
  ["question", "Агент ждёт ответа: поза ожидания, «Жду ответа», вопрос на экране"],
  ["done", "Итог: Варя рядом с названием, «Готово» закрывает экран"],
] as const;

/** `ProgressWall` с Варей в трёх состояниях на готовых снимках заданий. */
function WallDemos() {
  const manager = useMemo(() => {
    const demo = new DemoJobManager();
    for (const [key] of WALL_STATES) demo.seed(SHOWCASE_JOBS[key]);
    return demo;
  }, []);
  return (
    <>
      {WALL_STATES.map(([key, label]) => (
        <Demo key={key} label={`ProgressWall — ${label}`} wide>
          <Card className="mx-auto max-w-col" data-demo={`wall-${key}`}>
            <ProgressWall
              jobId={SHOWCASE_JOBS[key].jobId}
              title="Подключаю ВШЭ"
              spaceTitle="Высшая школа экономики"
              timeZone={MSK}
              manager={manager}
              onMinimize={() => {}}
              onFinish={() => {}}
              headingLevel={3}
              renderMascot={(state, slot) => <WallMascot state={state} slot={slot} />}
            />
          </Card>
        </Demo>
      ))}
    </>
  );
}

/**
 * Варя (docs/design/mascot.md): состояния §2, фикстуры §8, календарь образов §7, сцена и насест чата §4.
 * Данные синтетические, время зафиксировано; это не доказательство живого входа в вуз.
 */
export function MascotSection() {
  return (
    // Витрина показывает много Варь сразу: правило «одна анимированная на экран» здесь снято.
    <MascotAnimationPolicy exclusive={false}>
      <Section
        id="mascot"
        title="Варя"
        note="Векторная Варя: один персонаж от иконки до сцены чата. Состояние приходит из приложения; каждый смысл остаётся текстом, а движение можно отключить."
      >
        <MascotPlayground />
        <div className="flex flex-col gap-6" data-part="poses">
          <PosesDemo />
          <RunnerDemo />
        </div>
        <div className="flex flex-col gap-6" data-part="wall">
          <WallDemos />
        </div>
        <div data-part="priority">
          <Demo label="Таблица приоритета §2 — по фикстуре на строку" wide>
            <Grid cols={3}>
              {PRIORITY_FIXTURES.map((fixture) => (
                <FixtureCard key={fixture.id} fixture={fixture} />
              ))}
            </Grid>
          </Demo>
        </div>
        <div data-part="checks">
          <Demo label="Фикстуры проверок §8" wide>
            <Grid cols={3}>
              {CHECK_FIXTURES.map((fixture) => (
                <FixtureCard key={fixture.id} fixture={fixture} />
              ))}
              <DoneOnceDemo />
              <SpaceSwitchDemo />
              <CalendarDemo />
            </Grid>
          </Demo>
        </div>
        <div data-part="dock">
          <Demo
            label="Чат: сцена пустого чата и прыжок на насест, открытая клавиатура, насест на 320 / 390 / 430 px"
            wide
          >
            <div className="flex flex-wrap items-start gap-6">
              <DockFrame width="max-w-[20rem]" label="320" pad="px-4" />
              <DockFrame width="max-w-[24.375rem]" label="390" pad="px-4" />
              <DockFrame width="max-w-[26.875rem]" label="430" pad="px-5" />
            </div>
          </Demo>
        </div>
      </Section>
    </MascotAnimationPolicy>
  );
}
