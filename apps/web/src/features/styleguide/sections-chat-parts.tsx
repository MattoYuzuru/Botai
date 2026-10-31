import { ChatMessage, ContextMeter, ContextMeterBar, SourceCitation } from "@botai/design";
import { MSK, NOW } from "./fixtures";
import { Demo, Grid, Section } from "./layout";

const SEGMENTS = [
  { kind: "system", label: "Правила и навыки", tokens: 9_800 },
  { kind: "profile", label: "Профиль", tokens: 600 },
  { kind: "data", label: "Данные вуза", tokens: 12_400 },
  { kind: "documents", label: "Документы", tokens: 15_000 },
  { kind: "history", label: "Переписка", tokens: 3_450 },
  { kind: "free", label: "Свободно", tokens: 958_750 },
];

const SOURCES = [
  { title: "Расписание — личный кабинет", kind: "data", fetchedAt: NOW - 12 * 60_000 },
  { title: "Правила сдачи курсовых работ", kind: "document", fetchedAt: NOW - 30 * 3_600_000 },
];

/** Шкала контекста агента и источники ответа, включая внутренние ссылки в «Учёбу». Числа синтетические. */
export function ContextSection() {
  return (
    <Section
      id="context"
      title="Шкала контекста и источники"
      note="Одна плоская полоса по сегментам, как /context в Claude Code, и список источников ответа. Внутренние ссылки открываются в приложении, внешние — в новой вкладке."
    >
      <Grid>
        <Demo label="ContextMeter — полоса, легенда и источники" wide>
          <ContextMeter
            limit={1_000_000}
            used={41_250}
            segments={SEGMENTS}
            sources={SOURCES}
            nowMs={NOW}
            timeZone={MSK}
          />
        </Demo>
        <Demo label="Полоса в шапке и пустой контекст" wide>
          <div className="flex flex-col gap-6">
            <ContextMeterBar limit={1_000_000} segments={SEGMENTS} className="w-48" />
            <ContextMeterBar limit={200_000} segments={SEGMENTS.slice(0, 3)} className="w-48" />
            <ContextMeter limit={1_000_000} used={10_400} segments={SEGMENTS.slice(0, 2)} nowMs={NOW} />
          </div>
        </Demo>
        <Demo label="SourceCitation — внешняя, внутренняя и недоступная ссылки" wide>
          <div className="flex flex-col gap-2">
            <SourceCitation
              title="Расписание на сайте вуза"
              url="https://lk.fakeuni.test/timetable"
              fetchedAt={NOW - 12 * 60_000}
              nowMs={NOW}
              timeZone={MSK}
            />
            <SourceCitation
              title="Правила сдачи курсовых работ"
              url="botai://documents/doc-2"
              fetchedAt={NOW - 30 * 3_600_000}
              nowMs={NOW}
              timeZone={MSK}
              internal={{
                href: "/study",
                label: "Открыть документы в «Учёбе»",
                icon: "document",
              }}
            />
            <SourceCitation
              title="Расписание — личный кабинет"
              url="botai://connections/c-1"
              fetchedAt={NOW - 12 * 60_000}
              nowMs={NOW}
              timeZone={MSK}
              internal={{
                href: "/study",
                label: "Открыть подключение в «Учёбе»",
                icon: "connection",
              }}
            />
            <SourceCitation
              title="Источник без адреса"
              url="botai://sources/3"
              fetchedAt={NOW - 3 * 86_400_000}
              nowMs={NOW}
              timeZone={MSK}
            />
          </div>
        </Demo>
        <Demo label="ChatMessage — «Не уверен»: честный ответ без источника" wide>
          <ChatMessage
            role="agent"
            confidence="unsure"
            markdown="Не знаю: в подключённых документах нет ответа на этот вопрос. Загрузите документ или подключите страницу с правилами."
            at={NOW}
            timeZone={MSK}
          />
        </Demo>
      </Grid>
    </Section>
  );
}
