import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { returnPath } from "@/app/session";
import { chatHarness, wait } from "@/test/chat-harness";
import { renderApp } from "@/test/render-app";
import type { SharedThread } from "./api";

const harness = chatHarness();

const AT = "2026-10-27T11:00:00.000Z";
const sharedThread = (partial: Partial<SharedThread> = {}): SharedThread => ({
  shareId: "abcdefghijklmnopqrstuv",
  title: "Курсовая",
  ownerName: "Матвей",
  mine: false,
  threadId: null,
  sharedAt: AT,
  messages: [
    {
      id: "M1",
      role: "user",
      markdown: "Что нужно для курсовой?",
      quote: null,
      attachments: [],
      createdAt: AT,
    },
    {
      id: "M2",
      role: "agent",
      markdown: "**Заявление** и [чат](botai://threads/T9)",
      quote: null,
      attachments: [],
      createdAt: AT,
    },
    {
      id: "M3",
      role: "user",
      markdown: "",
      quote: null,
      attachments: [{ name: "шаблон.docx", mime: "application/msword", size: 2048, kind: "document" }],
      createdAt: AT,
    },
  ],
  ...partial,
});

describe("«Поделиться чатом»", () => {
  it("создаёт ссылку только для чтения, копирует её и закрывает доступ", async () => {
    const user = userEvent.setup();
    renderApp("/chat");
    await user.click(await screen.findByRole("button", { name: "Поделиться" }, wait));
    const sheet = await screen.findByRole("dialog", { name: "Поделиться чатом" });
    expect(await within(sheet).findByText("Чат пока никому не открыт.", undefined, wait)).toBeInTheDocument();

    await user.click(within(sheet).getByRole("button", { name: "Создать ссылку" }));
    const field = await within(sheet).findByRole("textbox", { name: "Ссылка на чат" }, wait);
    const link = (field as HTMLInputElement).value;
    expect(new URL(link).pathname).toMatch(/^\/shared\/share-/);
    await waitFor(async () => expect(await navigator.clipboard.readText()).toBe(link), wait);
    expect(within(sheet).getByRole("link", { name: "Открыть" })).toHaveAttribute("target", "_blank");

    await user.click(within(sheet).getByRole("button", { name: "Закрыть доступ" }));
    expect(await within(sheet).findByText("Чат пока никому не открыт.", undefined, wait)).toBeInTheDocument();
  });

  it("у нового чата кнопки нет: делиться ещё нечем", async () => {
    renderApp("/chat?thread=new");
    await screen.findByRole("button", { name: "Чаты" }, wait);
    expect(screen.queryByRole("button", { name: "Поделиться" })).toBeNull();
  });
});

describe("чат по ссылке", () => {
  it("показывает переписку только для чтения: без поля ввода, с копированием и названиями файлов", async () => {
    harness.server.use(http.get("*/api/v1/shared/chats/:shareId", () => HttpResponse.json(sharedThread())));
    const user = userEvent.setup();
    renderApp("/shared/abcdefghijklmnopqrstuv");
    expect(await screen.findByRole("heading", { level: 1, name: "Курсовая" }, wait)).toBeInTheDocument();
    expect(screen.getByText("Только чтение")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Сообщение агенту" })).toBeNull();
    const list = screen.getByRole("list", { name: "Сообщения" });
    expect(within(list).getByText("Что нужно для курсовой?")).toBeInTheDocument();
    expect(within(list).getByText("шаблон.docx")).toBeInTheDocument();
    // Внутренняя ссылка ответа ведёт в приложение; откроется, только если у зрителя есть доступ.
    expect(within(list).getByRole("link", { name: /чат/ })).toHaveAttribute("href", "/chat?thread=T9");

    await user.click(within(list).getByRole("button", { name: "Скопировать ответ" }));
    expect(await navigator.clipboard.readText()).toBe("**Заявление** и [чат](botai://threads/T9)");
    expect(within(list).getAllByRole("button", { name: "Скопировать сообщение" })).toHaveLength(1);
  });

  it("владельцу предлагает открыть сам чат", async () => {
    harness.server.use(
      http.get("*/api/v1/shared/chats/:shareId", () =>
        HttpResponse.json(sharedThread({ mine: true, threadId: "T1" })),
      ),
    );
    renderApp("/shared/abcdefghijklmnopqrstuv");
    const open = await screen.findByRole("link", { name: "Открыть чат" }, wait);
    expect(open).toHaveAttribute("href", "/chat?thread=T1");
  });

  it("закрытая ссылка говорит, что делать", async () => {
    harness.server.use(
      http.get("*/api/v1/shared/chats/:shareId", () =>
        HttpResponse.json(
          {
            type: "about:blank",
            title: "not_found",
            status: 404,
            detail: "Ссылка недействительна",
            code: "not_found",
          },
          { status: 404 },
        ),
      ),
    );
    renderApp("/shared/abcdefghijklmnopqrstuv");
    expect(await screen.findByText("Ссылка не открывается", undefined, wait)).toBeInTheDocument();
    expect(screen.getByText(/Попросите новую ссылку/)).toBeInTheDocument();
  });

  it("после входа возвращает только на ссылку чата", () => {
    expect(returnPath("/shared/abc")).toBe("/shared/abc");
    expect(returnPath("/chat?thread=T1")).toBeUndefined();
    expect(returnPath("https://evil.example/shared/abc")).toBeUndefined();
    expect(returnPath(undefined)).toBeUndefined();
  });
});
