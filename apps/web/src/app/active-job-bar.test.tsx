import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, it } from "vitest";
import type { ThreadOut } from "@/api/v3";
import { useActiveJob } from "@/features/jobs/active-job";
import { chatHarness, wait } from "@/test/chat-harness";
import { renderApp } from "@/test/render-app";

const harness = chatHarness();
afterEach(() => useActiveJob.getState().clear());

const AT = "2026-10-27T11:00:00.000Z";
const thread = (id: string, activeJobId?: string): ThreadOut => ({
  id,
  spaceId: null,
  title: "Документ",
  createdAt: AT,
  lastMessageAt: AT,
  unread: false,
  attention: activeJobId ? "running" : null,
  activeTask: activeJobId
    ? { jobId: activeJobId, kind: "ingest", connectionId: null, state: "running", title: "Индексация" }
    : null,
  preview: null,
});

describe("плашка работы на других экранах", () => {
  it("тред известен из записи: «Открыть» ведёт в него", async () => {
    useActiveJob.getState().set({ jobId: "J1", title: "Подключаю ВШЭ", threadId: "T1" });
    renderApp("/");
    expect(await screen.findByRole("link", { name: "Открыть" }, wait)).toHaveAttribute(
      "href",
      "/chat?thread=T1",
    );
  });

  it("треда в записи нет (trackJob без треда): он находится среди активных задач списка чатов", async () => {
    harness.server.use(http.get("*/api/v1/chat/threads", () => HttpResponse.json([thread("T9", "J2")])));
    useActiveJob.getState().set({ jobId: "J2", title: "Документ: правила" });
    renderApp("/");
    expect(await screen.findByRole("link", { name: "Открыть" }, wait)).toHaveAttribute(
      "href",
      "/chat?thread=T9",
    );
  });

  it("тред неизвестен вовсе: плашка остаётся и ведёт к списку чатов, а не в «последний тред» наугад", async () => {
    harness.server.use(
      http.get("*/api/v1/chat/threads", () => HttpResponse.json([thread("T1")])),
      http.get("*/api/v1/jobs/:jobId", () =>
        HttpResponse.json({ id: "J3", kind: "ingest", state: "running", stage: null, createdAt: AT }),
      ),
    );
    useActiveJob.getState().set({ jobId: "J3", title: "Документ: правила" });
    const { router } = renderApp("/");
    const link = await screen.findByRole("link", { name: "К чатам" }, wait);
    expect(link).toHaveAttribute("href", "/chat?threads=1");
    expect(screen.queryByRole("link", { name: "Открыть" })).toBeNull();
    await userEvent.setup().click(link);
    // Список открыт поверх нового чата, а не поверх последнего.
    expect(await screen.findByRole("dialog", { name: "Чаты" }, wait)).toBeInTheDocument();
    await waitFor(() => expect(router.state.location.search).toEqual({ thread: "new" }));
  });

  it("JobOut несёт thread id — плашка использует его", async () => {
    harness.server.use(
      http.get("*/api/v1/chat/threads", () => HttpResponse.json([thread("T1")])),
      http.get("*/api/v1/jobs/:jobId", () =>
        HttpResponse.json({
          id: "J4",
          kind: "ingest",
          state: "running",
          stage: null,
          createdAt: AT,
          threadId: "T7",
        }),
      ),
    );
    useActiveJob.getState().set({ jobId: "J4", title: "Документ: правила" });
    renderApp("/");
    expect(await screen.findByRole("link", { name: "Открыть" }, wait)).toHaveAttribute(
      "href",
      "/chat?thread=T7",
    );
  });
});
