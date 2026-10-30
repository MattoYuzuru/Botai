import { describe, expect, it } from "vitest";
import { connectionFixture } from "@/test/data-fixtures";
import {
  addCalendarDraft,
  addCalendarLink,
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
    expect(CONNECT_UNIVERSITY_DRAFT.endsWith(" ")).toBe(true);
    expect(addSourceDraft("НИУ ВШЭ")).toContain("«НИУ ВШЭ»");
    expect(addSourceDraft("НИУ ВШЭ").endsWith(": ")).toBe(true);
  });

  it("«Календарь (iCal)» — чат с готовой просьбой подключить календарь вуза; ссылки в просьбе нет", () => {
    const draft = addCalendarDraft("НИУ ВШЭ");
    expect(draft).toContain("«НИУ ВШЭ»");
    expect(draft).toMatch(/календар/i);
    expect(draft).not.toMatch(/https?:|webcal:|\.ics/);
    expect(addCalendarLink("НИУ ВШЭ")).toEqual({ to: "/chat", search: { draft } });
  });

  it("календарь с умершей ссылкой просит новую ссылку, а не вход", () => {
    const calendar = connectionFixture({
      kind: "calendar",
      adapter: "ical",
      title: "Календарь",
      status: "needs_reauth",
    });
    const draft = connectionChatLink(calendar, "НИУ ВШЭ").search.draft;
    expect(draft).toContain("«Календарь»");
    expect(draft).toContain("НИУ ВШЭ");
    expect(draft).toMatch(/ссылк/i);
    expect(draft).not.toMatch(/Войди/);
  });

  it("плашка источника: есть тред задачи — в него, нет — готовая просьба по состоянию", () => {
    const withThread = connectionFixture({ status: "needs_reauth", threadId: "01JTHREAD" });
    expect(connectionChatLink(withThread, "НИУ ВШЭ")).toEqual(chatThreadLink("01JTHREAD"));
    const draft = (status: Parameters<typeof connectionFixture>[0] = {}) =>
      connectionChatLink(connectionFixture({ title: "Smart LMS", ...status }), "НИУ ВШЭ").search.draft;
    for (const status of ["needs_reauth", "waiting_user", "error"] as const) {
      expect(draft({ status })).toContain("«Smart LMS»");
      expect(draft({ status })).toContain("НИУ ВШЭ");
    }
    expect(draft({ status: "needs_reauth" })).not.toBe(draft({ status: "waiting_user" }));
  });
});
