import { describe, expect, it } from "vitest";
import { connectionFixture } from "@/test/data-fixtures";
import {
  addSourceDraft,
  CONNECT_UNIVERSITY_DRAFT,
  chatDraftLink,
  chatThreadLink,
  connectionChatLink,
  connectUniversityLink,
} from "./chat-links";

describe("переходы в чат (agent-hub-v3 §3.9)", () => {
  it("тред — /chat?thread=<id>, новый чат с черновиком — /chat?draft=<текст>", () => {
    expect(chatThreadLink("01JT")).toEqual({ to: "/chat", search: { thread: "01JT" } });
    expect(chatDraftLink("Привет")).toEqual({ to: "/chat", search: { draft: "Привет" } });
  });

  it("«Подключить ещё вуз» оставляет фразу открытой; «Добавить источник» называет вуз", () => {
    expect(connectUniversityLink().search.draft).toBe(CONNECT_UNIVERSITY_DRAFT);
    expect(CONNECT_UNIVERSITY_DRAFT).toBe("Давай подключим вуз ");
    expect(addSourceDraft("НИУ ВШЭ")).toBe("Давай добавим для НИУ ВШЭ ещё один источник: ");
  });

  it("плашка источника: есть тред задачи — в него, нет — готовая просьба по состоянию", () => {
    const withThread = connectionFixture({ status: "needs_reauth", threadId: "01JTHREAD" });
    expect(connectionChatLink(withThread, "НИУ ВШЭ")).toEqual(chatThreadLink("01JTHREAD"));
    const draft = (status: Parameters<typeof connectionFixture>[0] = {}) =>
      connectionChatLink(connectionFixture({ title: "Smart LMS", ...status }), "НИУ ВШЭ").search.draft;
    expect(draft({ status: "needs_reauth" })).toBe("Войди снова в «Smart LMS» (НИУ ВШЭ)");
    expect(draft({ status: "waiting_user" })).toBe("Продолжим подключение «Smart LMS» (НИУ ВШЭ)");
    expect(draft({ status: "error" })).toBe("Почему не обновляется «Smart LMS» (НИУ ВШЭ)? Посмотри и почини");
  });
});
