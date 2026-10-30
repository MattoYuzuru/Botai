import {
  ApprovalCard,
  Button,
  Card,
  ChatComposer,
  ChatMessage,
  DeadlineItem,
  Dialog,
  HandoffCard,
  ProgressRail,
  ScheduleDay,
  ScheduleWeek,
  Sheet,
  SourceCitation,
  TabBar,
  ThinkingStream,
  toast,
  UniversitySpaceSwitcher,
  urgencyOf,
  WaitCard,
  Wordmark,
} from "@botai/design";
import { useState } from "react";
import {
  DEADLINES,
  MAIN_TABS,
  MSK,
  NOW,
  PROPOSAL,
  SAMPLE_MARKDOWN,
  SCHEDULE,
  SPACES,
  THINKING,
  WEEK,
} from "./fixtures";
import { Demo, Grid, Section } from "./layout";

export function StudySection() {
  const [selected, setSelected] = useState(NOW);
  const [space, setSpace] = useState("all");
  return (
    <Section
      id="study"
      title="Учебные данные"
      note="У каждой строки видны вуз и время получения. Срочность — текстом и меткой; без срока — «Срок не указан», без придуманного отсчёта."
    >
      <Demo label="UniversitySpaceSwitcher — кнопка в шапке открывает шит со статусами" wide>
        <UniversitySpaceSwitcher
          spaces={SPACES}
          activeId={space}
          onChange={setSpace}
          allowAll
          nowMs={NOW}
          timeZone={MSK}
        />
      </Demo>
      <Grid>
        <Demo label="DeadlineItem — calm, soon, urgent, overdue, unknown, сдано" wide>
          <Card padding="sm" className="flex flex-col">
            {DEADLINES.map((deadline) => (
              <DeadlineItem
                key={deadline.id}
                title={deadline.title}
                courseTitle={deadline.course}
                dueAt={deadline.dueAt}
                urgency={urgencyOf(deadline.dueAt, NOW)}
                spaceId={deadline.spaceId}
                spaceTitle={deadline.spaceTitle}
                nowMs={NOW}
                timeZone={MSK}
                showSpace
                onOpen={() => toast("Откроется карточка задания")}
              />
            ))}
            <DeadlineItem
              title="Эссе по литературе"
              dueAt={NOW - 86_400_000}
              urgency="overdue"
              done
              spaceId="hse"
              spaceTitle="ВШЭ"
              nowMs={NOW}
              timeZone={MSK}
              showSpace
            />
          </Card>
        </Demo>
        <Demo label="ScheduleWeek + ScheduleDay (пара идёт сейчас, изменение, пересечение)" wide>
          <div className="flex flex-col gap-4">
            <ScheduleWeek days={WEEK} selected={selected} onSelect={setSelected} nowMs={NOW} timeZone={MSK} />
            <ScheduleDay
              date={selected}
              items={selected === NOW ? SCHEDULE : []}
              state={selected === NOW ? "ok" : "empty"}
              nowMs={NOW}
              timeZone={MSK}
              showSpace
              freshness="Обновлено 5 минут назад"
            />
          </div>
        </Demo>
        <Demo label="ScheduleDay — loading" wide>
          <ScheduleDay date={NOW} items={[]} state="loading" nowMs={NOW} timeZone={MSK} />
        </Demo>
        <Demo label="ScheduleDay — stale: данные остаются, подпись свежести видна" wide>
          <ScheduleDay
            date={NOW}
            items={SCHEDULE.slice(0, 1)}
            state="stale"
            nowMs={NOW}
            timeZone={MSK}
            freshness="Данные от 24 октября"
          />
        </Demo>
      </Grid>
      <Grid>
        <Demo label="SourceCitation — рабочий и недоступный источник" wide>
          <div className="flex flex-col gap-2">
            <SourceCitation
              title="Расписание — ЛК вуза"
              url="https://lk.fakeuni.test/timetable"
              fetchedAt={NOW - 3600_000}
              nowMs={NOW}
              timeZone={MSK}
              spaceTitle="ВШЭ"
              snippet="Алгоритмы, 9:30, ауд. 214"
            />
            <SourceCitation
              title="Старый портал"
              url={null}
              fetchedAt={NOW - 3 * 86_400_000}
              nowMs={NOW}
              timeZone={MSK}
              spaceTitle="ЦУ"
              stale
            />
          </div>
        </Demo>
      </Grid>
    </Section>
  );
}

export function ChatSection() {
  const [value, setValue] = useState("");
  const [collapsed, setCollapsed] = useState(false);
  return (
    <Section
      id="chat"
      title="Чат и агент"
      note="Блоки агента рисуются только этими компонентами. Ответ без источника не имитирует источник, неуверенный ответ помечен."
    >
      <Grid>
        <Demo label="ChatMessage — ответ с источником и вопрос пользователя" wide>
          <div className="flex flex-col gap-4">
            <ChatMessage role="user" markdown="Какая следующая пара?" at={NOW - 60_000} timeZone={MSK} />
            <ChatMessage
              role="agent"
              markdown={SAMPLE_MARKDOWN}
              at={NOW}
              timeZone={MSK}
              nowMs={NOW}
              citationGroups={[
                {
                  id: "lk",
                  label: "Личный кабинет ВШЭ",
                  spaceTitle: "ВШЭ",
                  fetchedAt: NOW - 3600_000,
                  items: [{ id: "1", title: "Расписание", url: "https://lk.fakeuni.test/timetable" }],
                },
              ]}
            />
          </div>
        </Demo>
        <Demo label="ChatMessage — не уверен, ответ приходит, ждёт сети" wide>
          <div className="flex flex-col gap-4">
            <ChatMessage
              role="agent"
              confidence="unsure"
              markdown="Не нашёл в подключённых источниках расписание на пятницу. Подключите ещё один источник или уточните, какой вуз имеется в виду."
            />
            <ChatMessage role="agent" status="streaming" markdown="Ищу в ЛК ВШЭ, читаю список" />
            <ChatMessage role="user" status="queued" markdown="Когда экзамен по алгоритмам?" />
          </div>
        </Demo>
      </Grid>
      <Demo label="ChatComposer — поле 16 px, авто-рост до 6 строк; место для Вари — на кромке справа" wide>
        <div className="flex flex-col pt-10">
          <ChatComposer
            value={value}
            onValueChange={setValue}
            onSend={() => {
              toast("Сообщение отправлено", { tone: "success" });
              setValue("");
            }}
            mascotSlot={
              <span className="block size-9 rounded-md border border-dashed border-border-strong" />
            }
          />
        </div>
      </Demo>
      <Grid>
        <Demo label="ThinkingStream — развёрнут, шаги, ошибка, ждёт" wide>
          <ThinkingStream steps={THINKING} nowMs={NOW} collapsed={collapsed} onToggle={setCollapsed} />
        </Demo>
        <Demo label="ProgressRail — стадии без процентов, время настоящее, остаток неизвестен" wide>
          <div className="flex flex-col gap-6">
            <ProgressRail stage={2} stageCount={5} elapsedMs={72_000} etaKnown={false} />
            <ProgressRail stage={0} stageCount={null} elapsedMs={8_000} etaKnown={false} />
            <ProgressRail
              stage={3}
              stageCount={4}
              elapsedMs={132_000}
              etaKnown
              etaMs={50_000}
              tone="success"
            />
          </div>
        </Demo>
      </Grid>
      <Grid>
        <Demo label="WaitCard — ожидание на другом устройстве, без кнопок" wide>
          <WaitCard
            message="Подтвердите вход в приложении на телефоне"
            expiresAt={NOW + 150_000}
            nowMs={NOW}
          />
        </Demo>
        <Demo label="HandoffCard — агент не может отправить форму сам" wide>
          <HandoffCard
            title="Заявка на справку"
            url="https://fakeuni.test/requests/new"
            text="Прошу выдать справку об обучении для предоставления по месту требования."
          />
        </Demo>
        <Demo label="ApprovalCard — метод, хост, поля, срок, одноразовое подтверждение" wide>
          <ApprovalCard
            proposal={PROPOSAL}
            nowMs={NOW}
            timeZone={MSK}
            onApprove={() => toast("Запрос подтверждён")}
            onReject={() => toast("Ничего не отправлено")}
          />
        </Demo>
        <Demo label="ApprovalCard — истекло" wide>
          <ApprovalCard
            proposal={PROPOSAL}
            state="expired"
            nowMs={NOW}
            timeZone={MSK}
            onApprove={() => {}}
            onReject={() => {}}
          />
        </Demo>
      </Grid>
    </Section>
  );
}

export function NavigationSection() {
  const [active, setActive] = useState("schedule");
  const [sheet, setSheet] = useState(false);
  const [dialog, setDialog] = useState(false);
  return (
    <Section
      id="navigation"
      title="Навигация и слои"
      note="Ровно пять вкладок. До 1024 px — нижняя панель, от 1024 px — боковая 240 px (см. каркас приложения)."
    >
      <Demo label="TabBar — нижняя панель (предпросмотр в потоке страницы)" wide>
        <div className="max-w-panel overflow-hidden rounded-xl border border-border">
          <TabBar
            inline
            items={MAIN_TABS}
            activeId={active}
            onSelect={setActive}
            renderLink={(item, props) => <a href={`#${item.id}`} {...props} />}
          />
        </div>
      </Demo>
      <Demo label="Wordmark — «Bot» fg, «AI» accent-fg">
        <Wordmark size="sm" />
        <Wordmark size="md" />
        <Wordmark size="lg" />
      </Demo>
      <Demo label="Sheet, Dialog, Toast">
        <Button onClick={() => setSheet(true)}>Открыть шит</Button>
        <Button tone="danger" onClick={() => setDialog(true)}>
          Отключить вуз
        </Button>
        <Button
          onClick={() =>
            toast("Вуз подключён", { tone: "success", description: "Расписание обновится само." })
          }
        >
          Уведомление
        </Button>
        <Button
          onClick={() =>
            toast("Не удалось открыть ЛК ВШЭ", {
              tone: "warning",
              description: "Отправим, когда вуз ответит.",
              action: { label: "Отменить", onClick: () => {} },
            })
          }
        >
          Уведомление с действием
        </Button>
      </Demo>
      <Sheet
        open={sheet}
        onOpenChange={setSheet}
        title="Подключение вуза"
        description="Потяните вниз, нажмите «Закрыть» или Esc."
        footer={
          <Button variant="primary" fullWidth onClick={() => setSheet(false)}>
            Готово
          </Button>
        }
      >
        <p className="type-body text-fg">
          На телефоне шит выезжает снизу, на ноутбуке становится правой панелью 420 px.
        </p>
      </Sheet>
      <Dialog
        open={dialog}
        onOpenChange={setDialog}
        title="Отключить ЛК ВШЭ?"
        description="Расписание и сроки останутся в снимке, но обновляться перестанут. Сохранённый пароль будет удалён."
        confirmLabel="Отключить"
        onConfirm={() => toast("ЛК ВШЭ отключён")}
      />
    </Section>
  );
}
