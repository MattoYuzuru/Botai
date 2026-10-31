import { describe, expect, it } from "vitest";
import { addSourceLinkV4, askUniversityLink, sourceChatLinks } from "./chat-links-v4";
import type { StudySource } from "./use-study-sources";

const source = (patch: Partial<StudySource> = {}): StudySource => ({
  id: "s1",
  title: "Smart LMS",
  kinds: ["lms", "grades"],
  status: "active",
  lastSyncAt: null,
  errorDetail: null,
  entryUrl: "https://my.fakeuni.test/learn",
  origin: "https://my.fakeuni.test",
  threadId: null,
  playbookId: null,
  trust: null,
  priority: null,
  mode: "v4",
  ...patch,
});

const draftOf = (link: { search: { draft?: string } } | null) => link?.search.draft ?? "";

describe("фразы чата из v4-«Учёбы»", () => {
  it("«Добавить источник» и «Спросить агента» ведут в тот же агентский тред", () => {
    expect(addSourceLinkV4("НИУ ВШЭ").search.draft).toContain("НИУ ВШЭ");
    expect(addSourceLinkV4("НИУ ВШЭ").to).toBe("/chat");
    expect(draftOf(askUniversityLink("НИУ ВШЭ"))).toMatch(/источник/i);
    expect(draftOf(sourceChatLinks(source(), "НИУ ВШЭ").ask)).toContain("Smart LMS");
  });

  it("«Починить» зависит от состояния: вход, новая ссылка, разбор ошибки", () => {
    const reauth = sourceChatLinks(source({ status: "needs_reauth" }), "НИУ ВШЭ").repair;
    expect(draftOf(reauth)).toMatch(/[Вв]ойди снова/);

    const link = sourceChatLinks(source({ status: "needs_link", kinds: ["calendar"] }), "НИУ ВШЭ").repair;
    expect(draftOf(link)).toMatch(/защищённое поле/);
    expect(draftOf(link)).not.toMatch(/https?:/);

    const broken = sourceChatLinks(
      source({ status: "error", errorDetail: "Сайт изменил разметку" }),
      "НИУ ВШЭ",
    ).repair;
    expect(draftOf(broken)).toMatch(/[Пп]очини/);
    expect(draftOf(broken)).toContain("Сайт изменил разметку");
  });

  it("у работающего и выключенного источника «Починить» нет", () => {
    expect(sourceChatLinks(source({ status: "active" }), "ВШЭ").repair).toBeNull();
    expect(sourceChatLinks(source({ status: "disabled" }), "ВШЭ").repair).toBeNull();
  });

  it("в тред задачи ведёт только известный threadId (v3-подключения)", () => {
    expect(sourceChatLinks(source(), "ВШЭ").openThread).toBeNull();
    const thread = sourceChatLinks(source({ threadId: "01THREAD" }), "ВШЭ").openThread;
    expect(thread?.search.thread).toBe("01THREAD");
  });
});
