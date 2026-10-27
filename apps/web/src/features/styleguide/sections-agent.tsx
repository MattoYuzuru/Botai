import { Card, ThinkingStream } from "@botai/design";
import { useMemo, useState } from "react";
import { AgentBlockView } from "@/features/agent-blocks/agent-block";
import type { BlockContext } from "@/features/agent-blocks/context";
import type { AnswerState } from "@/features/jobs/job-state";
import { RemoteScreen } from "@/features/remote-screen/remote-screen";
import { ProgressWall } from "@/widgets/progress-wall/progress-wall";
import { createDemoSocket } from "./demo-remote";
import { MSK, NOW } from "./fixtures";
import { DemoJobManager, SHOWCASE_BLOCKS, SHOWCASE_JOBS, SHOWCASE_LOGIN } from "./fixtures-agent";
import { Demo, Grid, Section } from "./layout";

/** Все блоки `AgentUiBlock` (ADR-0004): каждый рисуется только компонентами дизайн-системы. Ответы локальные. */
export function AgentBlocksSection() {
  const [answers, setAnswers] = useState<Record<string, AnswerState>>({});
  const ctx: BlockContext = useMemo(
    () => ({
      jobId: "showcase",
      answers,
      nowMs: NOW,
      timeZone: MSK,
      spaceTitle: "ВШЭ",
      onAnswer: (blockId, _values, shown) =>
        setAnswers((current) => ({ ...current, [blockId]: { status: "answered", shown, error: null } })),
      onApprove: (blockId) =>
        setAnswers((current) => ({
          ...current,
          [blockId]: { status: "answered", shown: null, error: null, outcome: "approved" },
        })),
      onReject: (blockId) =>
        setAnswers((current) => ({
          ...current,
          [blockId]: { status: "answered", shown: null, error: null, outcome: "rejected" },
        })),
    }),
    [answers],
  );
  const view = (block: (typeof SHOWCASE_BLOCKS)[keyof typeof SHOWCASE_BLOCKS]) => (
    <AgentBlockView block={block} ctx={ctx} />
  );
  return (
    <Section
      id="blocks"
      title="Блоки агента"
      note="Десять типов блоков плюс неизвестный. Клиент ничего не исполняет из блока: markdown без HTML, ссылки только http(s), секрет не остаётся в поле."
    >
      <Grid>
        <Demo label="text — markdown с источником" wide>
          {view(SHOWCASE_BLOCKS.text)}
        </Demo>
        <Demo label="thinking — ThinkingStream собирает шаги" wide>
          <ThinkingStream
            nowMs={NOW}
            steps={[
              { id: "1", label: "Ищу личный кабинет", state: "done", at: NOW - 24_000 },
              { id: "2", label: "Читаю расписание", state: "running", at: NOW - 6_000 },
            ]}
          />
        </Demo>
        <Demo label="choice single + «Другое»" wide>
          {view(SHOWCASE_BLOCKS.choiceSingle)}
        </Demo>
        <Demo label="choice multi, необязательный" wide>
          {view(SHOWCASE_BLOCKS.choiceMulti)}
        </Demo>
        <Demo label="input — логин" wide>
          {view(SHOWCASE_BLOCKS.inputText)}
        </Demo>
        <Demo label="input — ссылка, необязательная" wide>
          {view(SHOWCASE_BLOCKS.inputUrl)}
        </Demo>
        <Demo label="input secret — пароль (SecretField)" wide>
          {view(SHOWCASE_BLOCKS.inputSecret)}
        </Demo>
        <Demo label="input secret — код из SMS" wide>
          {view(SHOWCASE_BLOCKS.inputCode)}
        </Demo>
        <Demo label="confirm — подписи из блока" wide>
          {view(SHOWCASE_BLOCKS.confirm)}
        </Demo>
        <Demo label="summary" wide>
          {view(SHOWCASE_BLOCKS.summary)}
        </Demo>
        <Demo label="approval" wide>
          {view(SHOWCASE_BLOCKS.approval)}
        </Demo>
        <Demo label="wait" wide>
          {view(SHOWCASE_BLOCKS.wait)}
        </Demo>
        <Demo label="handoff" wide>
          {view(SHOWCASE_BLOCKS.handoff)}
        </Demo>
        <Demo label="неизвестный тип — текстом с пометкой" wide>
          {view(SHOWCASE_BLOCKS.unknown)}
        </Demo>
      </Grid>
      <Demo
        label="login — запасной экран входа (сервер подменён витриной: рисует страницу вуза и отвечает на ввод)"
        wide
      >
        <Card padding="none" className="max-w-col">
          <RemoteScreen block={SHOWCASE_LOGIN} nowMs={NOW} createSocket={createDemoSocket} />
        </Card>
      </Demo>
    </Section>
  );
}

const WALL_DEMOS = [
  ["question", "Идёт работа, вопрос ждёт ответа (логин)"],
  ["phone", "Ожидание подтверждения на телефоне"],
  ["reconnecting", "Связь с агентом восстанавливается"],
  ["done", "Итог: что подключено"],
  ["failed", "Сбой: хост вне списка разрешённых"],
] as const;

/** `ProgressWall` в пяти состояниях на готовых снимках заданий. */
export function ProgressWallSection() {
  const manager = useMemo(() => {
    const demo = new DemoJobManager();
    for (const job of Object.values(SHOWCASE_JOBS)) demo.seed(job);
    return demo;
  }, []);
  return (
    <Section
      id="progress-wall"
      title="ProgressWall"
      note="Экран долгой работы: стадия словами сервера, настоящее время, «сколько осталось — пока неизвестно», вопрос поверх экрана, итог, который нужно прочитать."
    >
      {WALL_DEMOS.map(([key, label]) => (
        <Demo key={key} label={label} wide>
          <Card className="mx-auto max-w-col" data-demo={key}>
            <ProgressWall
              jobId={SHOWCASE_JOBS[key].jobId}
              title="Подключаю ВШЭ"
              spaceTitle="Высшая школа экономики"
              timeZone={MSK}
              manager={manager}
              onMinimize={() => {}}
              onFinish={() => {}}
              onCancel={() => {}}
              onAskInChat={() => {}}
              headingLevel={3}
            />
          </Card>
        </Demo>
      ))}
    </Section>
  );
}
