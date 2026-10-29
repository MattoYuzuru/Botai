import {
  AttachmentViewer,
  Button,
  ChatComposer,
  ChatDropZone,
  ChatMessage,
  CitationGroups,
  type ComposerAttachment,
  PlanCard,
  QuickReplies,
  TaskCard,
  toast,
  UnreadDot,
  type ViewerAttachment,
} from "@botai/design";
import { useState } from "react";
import { MSK, NOW } from "./fixtures";
import { Demo, Grid, Section } from "./layout";

// Картинка без цветов в разметке экрана: кодированный SVG, чтобы вьюеру было что показать.
const PICTURE =
  "data:image/svg+xml;utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 4 3'%3E%3Crect width='4' height='3' fill='gray'/%3E%3C/svg%3E";

const ITEMS: ViewerAttachment[] = [
  { id: "img", name: "Схема корпуса.png", mime: "image/png", size: 184_320, kind: "image", url: PICTURE },
  {
    id: "doc",
    name: "Положение о сессии.docx",
    mime: "application/msword",
    size: 52_430,
    kind: "other",
    url: "/files/doc",
  },
];

const READY: ComposerAttachment[] = [
  {
    id: "a",
    name: "Положение о сессии.pdf",
    mime: "application/pdf",
    size: 1_258_291,
    kind: "document",
    status: "ready",
  },
  {
    id: "b",
    name: "Голосовое сообщение 27.10 14-05.webm",
    mime: "audio/webm",
    size: 38_400,
    kind: "audio",
    status: "ready",
  },
];
const UPLOADING: ComposerAttachment[] = [
  {
    id: "c",
    name: "Конспект.pdf",
    mime: "application/pdf",
    size: 6_291_456,
    kind: "document",
    status: "uploading",
    progress: 0.4,
  },
  {
    id: "d",
    name: "Расписание.xlsx",
    mime: "application/vnd.ms-excel",
    size: 40_960,
    kind: "other",
    status: "uploading",
  },
];
const FAILED: ComposerAttachment[] = [
  {
    id: "e",
    name: "Видео лекции.mp4",
    mime: "video/mp4",
    size: 31_457_280,
    kind: "other",
    status: "failed",
    error: "Файл больше 20 МБ",
  },
  READY[0] as ComposerAttachment,
];

function ComposerDemo({ initial }: { initial: ComposerAttachment[] }) {
  const [value, setValue] = useState("");
  const [items, setItems] = useState(initial);
  return (
    <ChatComposer
      value={value}
      onValueChange={setValue}
      onSend={() => {
        toast("Сообщение отправлено", { tone: "success" });
        setValue("");
        setItems([]);
      }}
      attachments={items}
      onAddFiles={(files) => toast(`Прикреплено файлов: ${files.length}`)}
      onRemoveAttachment={(id) => setItems((rows) => rows.filter((row) => row.id !== id))}
      onOpenAttachment={() => toast("Откроется вьюер")}
    />
  );
}

/** Вложения и голос, быстрые ответы, план, карточка задачи и сгруппированные источники ответа. Данные синтетические. */
export function AttachmentsSection() {
  const [openId, setOpenId] = useState<string | null>(null);
  return (
    <Section
      id="attachments"
      title="Вложения и ответ агента"
      note="Поле ввода с вложениями и записью голоса, вьюер, план, быстрые ответы, карточка задачи и источники по названию. «+» открывает выбор файла, на телефоне — меню; микрофон виден, если браузер умеет записывать звук."
    >
      <Grid>
        <Demo label="ChatComposer — готовые вложения (отправка без текста разрешена)" wide>
          <ComposerDemo initial={READY} />
        </Demo>
        <Demo label="ChatComposer — идёт загрузка: процент известен и неизвестен" wide>
          <ComposerDemo initial={UPLOADING} />
        </Demo>
        <Demo label="ChatComposer — сбой загрузки: причина словами, «Убрать» остаётся" wide>
          <ComposerDemo initial={FAILED} />
        </Demo>
        <Demo label="ChatDropZone и AttachmentViewer — файлы над областью и просмотр" wide>
          <ChatDropZone onFiles={(files) => toast(`Отпущено файлов: ${files.length}`)} className="flex-none">
            <div className="flex flex-col gap-3 rounded-lg border border-dashed border-border-strong p-4">
              <p className="type-ui text-fg-muted">Перетащите сюда файл с компьютера — появится подсказка.</p>
              <div className="flex flex-wrap gap-3">
                <Button variant="secondary" onClick={() => setOpenId("img")}>
                  Открыть изображение
                </Button>
                <Button variant="secondary" onClick={() => setOpenId("doc")}>
                  Открыть файл без предпросмотра
                </Button>
              </div>
            </div>
          </ChatDropZone>
          <AttachmentViewer items={ITEMS} openId={openId} onOpenChange={setOpenId} />
        </Demo>
      </Grid>
      <Grid>
        <Demo label="ChatMessage — вложения у сообщения человека и ответ агента без карточки" wide>
          <div className="flex flex-col gap-4">
            <ChatMessage
              role="user"
              markdown="Вот схема и положение — что сдавать первым?"
              attachments={ITEMS}
              onOpenAttachment={setOpenId}
              at={NOW - 120_000}
              timeZone={MSK}
            />
            <ChatMessage
              role="agent"
              markdown="Первым сдавайте **курсовую**: срок раньше, чем у остальных работ."
              at={NOW}
              timeZone={MSK}
              nowMs={NOW}
              citationGroups={[
                {
                  id: "doc",
                  label: "Положение о сессии",
                  fetchedAt: NOW - 3 * 3_600_000,
                  items: [{ id: "1", title: "Положение о сессии", url: "https://lk.fakeuni.test/rules" }],
                },
              ]}
            />
          </div>
        </Demo>
        <Demo label="PlanCard — шаги и их состояние" wide>
          <PlanCard
            title="Подготовить сводку по сессии"
            steps={[
              { id: "1", title: "Собрать расписание экзаменов", state: "done" },
              { id: "2", title: "Найти сроки курсовых", state: "done" },
              { id: "3", title: "Сверить с положением о сессии", state: "active" },
              { id: "4", title: "Составить таблицу", state: "pending" },
              { id: "5", title: "Проверить онлайн-ресурсы", state: "skipped" },
            ]}
          />
        </Demo>
        <Demo label="QuickReplies — кнопки отправляют текст как сообщение" wide>
          <QuickReplies
            options={["Да, это НИУ ВШЭ", "Нет, другой вуз", "Показать расписание"]}
            onPick={(text) => toast(`Отправлено: ${text}`)}
          />
        </Demo>
        <Demo label="TaskCard — в этом чате и в другом" wide>
          <div className="flex flex-col gap-3">
            <TaskCard title="Подключение Smart LMS" kind="connect" state="running" />
            <TaskCard
              title="Починка расписания ВШЭ"
              kind="repair"
              state="waiting_user"
              here={false}
              action={
                <Button variant="secondary" size="sm" onClick={() => toast("Откроется чат задачи")}>
                  Открыть чат
                </Button>
              }
            />
            <TaskCard title="Индексация «Положение о сессии»" kind="ingest" state="done" />
          </div>
        </Demo>
        <Demo label="CitationGroups — одна строка на источник, а не на страницу" wide>
          <CitationGroups
            nowMs={NOW}
            timeZone={MSK}
            groups={[
              {
                id: "lk",
                label: "Личный кабинет ВШЭ",
                spaceTitle: "ВШЭ",
                fetchedAt: NOW - 12 * 60_000,
                items: [
                  { id: "1", title: "Расписание", url: "https://lk.fakeuni.test/timetable" },
                  { id: "2", title: "Аудитории", url: "https://lk.fakeuni.test/rooms" },
                  { id: "3", title: "Преподаватели", url: null },
                ],
              },
              {
                id: "doc",
                label: "Положение о сессии",
                fetchedAt: NOW - 30 * 3_600_000,
                stale: true,
                items: [
                  {
                    id: "d",
                    title: "Положение о сессии",
                    url: "botai://documents/doc-2",
                    internal: {
                      href: "/study/documents",
                      label: "Открыть документ в «Учёбе»",
                      icon: "document",
                    },
                  },
                ],
              },
            ]}
          />
        </Demo>
        <Demo label="UnreadDot — точка непрочитанного в списке чатов" wide>
          <ul className="flex flex-col gap-1">
            {["Подключение ЦУ", "Что сдавать на неделе?"].map((title, index) => (
              <li key={title} className="flex min-h-11 items-center gap-3 rounded-md px-3">
                <span className="grid w-2 place-items-center">{index === 0 && <UnreadDot />}</span>
                <span className="type-ui text-fg">{title}</span>
              </li>
            ))}
          </ul>
        </Demo>
      </Grid>
    </Section>
  );
}
