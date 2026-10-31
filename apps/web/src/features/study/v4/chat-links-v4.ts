import { addSourceDraft, type ChatLink, chatDraftLink, chatThreadLink } from "@/features/data/chat-links";
import type { StudySource } from "./use-study-sources";

/**
 * Фразы для чата из нового экрана «Учёбы» (03-sources-and-ux.md §5.2–§5.4): «Добавить источник» и действия
 * карточки открывают тот же агентский тред с готовой просьбой — отдельных мастеров и форм нет. Значения
 * секретов в текст не попадают: агент сам откроет защищённое поле.
 */
export interface SourceChatLinks {
  /** «Спросить агента» об источнике. */
  ask: ChatLink;
  /** Действие по состоянию: вход, новая ссылка, продолжение подключения или починка. */
  repair: ChatLink | null;
  /** Короткая подпись действия `repair` для кнопки. */
  repairLabel: string | null;
  /** Перейти в тред последней задачи источника, если он известен (v3-подключения). */
  openThread: ChatLink | null;
}

const calendarLike = (source: StudySource): boolean =>
  source.kinds.includes("calendar") || source.kinds.includes("mail");

/**
 * Действия карточки источника. У календаря и почты «новая ссылка/пароль» вводится защищённым полем,
 * поэтому просьба не просит прислать ссылку в чат.
 */
export function sourceChatLinks(source: StudySource, university: string): SourceChatLinks {
  const name = `«${source.title}» (${university})`;
  const ask = chatDraftLink(
    source.errorDetail
      ? `Что с источником ${name}? Он пишет: «${source.errorDetail}». Объясни и почини, если сможешь.`
      : `Расскажи про источник ${name}: что он читает, когда обновлялся и всё ли с ним в порядке?`,
  );
  if (source.status === "needs_reauth") {
    return {
      ask,
      repair: chatDraftLink(
        calendarLike(source)
          ? `Открой защищённое поле для новой ссылки источника ${name}`
          : `Войди снова в источник ${name}`,
      ),
      repairLabel: calendarLike(source) ? "Новая ссылка" : "Войти снова",
      openThread: source.threadId ? chatThreadLink(source.threadId) : null,
    };
  }
  if (source.status === "needs_link") {
    return {
      ask,
      repair: chatDraftLink(`Нужна новая ссылка для источника ${name}: открой защищённое поле`),
      repairLabel: "Новая ссылка",
      openThread: source.threadId ? chatThreadLink(source.threadId) : null,
    };
  }
  if (source.status === "waiting_user") {
    return {
      ask,
      repair: chatDraftLink(`Продолжим подключение источника ${name}`),
      repairLabel: "Продолжить",
      openThread: source.threadId ? chatThreadLink(source.threadId) : null,
    };
  }
  if (source.status === "draft" || source.status === "probing" || source.status === "learning") {
    return {
      ask,
      repair: chatDraftLink(`Проверь, как идёт подключение источника ${name}, и подскажи, что нужно от меня`),
      repairLabel: "Спросить о подключении",
      openThread: source.threadId ? chatThreadLink(source.threadId) : null,
    };
  }
  // Источник здоров, обновляется сам или выключен владельцем: просьбы «починить» нет.
  const healthy =
    source.status === "active" ||
    source.status === "syncing" ||
    source.status === "disabled" ||
    source.status === "archived";
  if (healthy) {
    return {
      ask,
      repair: null,
      repairLabel: null,
      openThread: source.threadId ? chatThreadLink(source.threadId) : null,
    };
  }
  return {
    ask,
    repair: chatDraftLink(
      source.errorDetail
        ? `Почини источник ${name}: он не обновляется — «${source.errorDetail}». Проверь, что изменилось, и обнови playbook.`
        : `Почини источник ${name}: он не обновляется. Проверь, что изменилось, и обнови playbook.`,
    ),
    repairLabel: "Починить",
    openThread: source.threadId ? chatThreadLink(source.threadId) : null,
  };
}

/** «Добавить источник» вуза: тот же тред агента с готовой фразой; что именно — допишет человек. */
export const addSourceLinkV4 = (university: string): ChatLink => chatDraftLink(addSourceDraft(university));

/** «Спросить агента» про вуз целиком: что обновляется, что требует внимания. */
export const askUniversityLink = (university: string): ChatLink =>
  chatDraftLink(
    `Расскажи, что происходит с источниками вуза «${university}»: всё ли обновляется и что требует внимания?`,
  );
