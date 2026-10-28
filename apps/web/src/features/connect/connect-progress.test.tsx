import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { api, call } from "@/api/client";
import { type ThreadRuntime, threadRuntimeKey, threadRuntimeQuery } from "@/features/chat/connection-api";
import { outbox } from "@/features/chat/runtime";
import { useActiveJob } from "@/features/jobs/active-job";
import { parseBlock } from "@/features/jobs/blocks";
import { jobManager } from "@/features/jobs/job-manager";
import { renderApp } from "@/test/render-app";
import { createTestServer } from "@/test/server";

vi.mock("@/features/remote-screen/remote-screen", () => ({ RemoteScreen: () => <div>Ручной вход</div> }));
let current = createTestServer("empty");
const WAIT = { timeout: 5000 };
const AT = "2026-10-28T10:00:00Z";
beforeAll(() => current.server.listen({ onUnhandledRequest: "error" }));
afterAll(() => current.server.close());
beforeEach(() => {
  jobManager.stopAll();
  useActiveJob.setState({ job: null });
  window.sessionStorage.clear();
  window.localStorage.clear();
  current.server.close();
  current = createTestServer("empty");
  current.server.listen({ onUnhandledRequest: "error" });
});
afterEach(() => {
  current.server.resetHandlers();
  act(() => jobManager.stopAll());
});

async function legacyJob() {
  const space = await call(api.POST("/api/v1/spaces", { body: { title: "Учебный стенд" } }));
  return call(
    api.POST("/api/v1/spaces/{space_id}/connections", {
      params: { path: { space_id: space.id } },
      body: { title: "Кабинет", kind: "portal", url: "https://lk.fakeuni.test/" },
    }),
  );
}

describe("legacy и восстановление разговора", () => {
  it("legacy job URL получает actual connectionId и открывает canonical chat без нового задания", async () => {
    const started = await legacyJob();
    const { router } = renderApp(`/connect/${started.jobId}`);
    expect(await screen.findByRole("button", { name: "Открыть браузер" }, WAIT)).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/chat");
    expect(useActiveJob.getState().job).toMatchObject({
      jobId: started.jobId,
      connectionId: started.connection.id,
      threadId: router.state.location.search.thread,
    });
    expect(current.db.sims.size).toBe(1);
  });

  it("очистка клиентских job refs не возвращает исторические поля: runtime восстанавливает текущий вопрос", async () => {
    const started = await legacyJob();
    const first = renderApp(`/connect/${started.jobId}`);
    await userEvent.type(await screen.findByLabelText("Ответ", {}, WAIT), "anna");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    await screen.findByText("Вход в lk.fakeuni.test. Пароль", undefined, WAIT);
    await userEvent.type(screen.getByLabelText("Ответ"), "fixture-secret");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    await screen.findByRole("checkbox", { name: /Алгоритмы/ }, WAIT);
    const thread = first.router.state.location.search.thread;
    first.unmount();
    act(() => {
      jobManager.stopAll();
      useActiveJob.setState({ job: null });
    });
    window.sessionStorage.clear();
    renderApp(`/chat?thread=${thread}`);
    await screen.findByRole("checkbox", { name: /Алгоритмы/ }, WAIT);
    expect(screen.queryByLabelText("Ответ")).toBeNull();
    expect(screen.queryByRole("button", { name: "Открыть браузер" })).toBeNull();
    expect(outbox.snapshot).toHaveLength(0);
  });

  it("HTTP ticket отбрасывается; переход возможен только после свежего matching SSE LoginBlock", async () => {
    const started = await legacyJob();
    const app = renderApp(`/connect/${started.jobId}`);
    await screen.findByRole("button", { name: "Открыть браузер" }, WAIT);
    const thread = String(app.router.state.location.search.thread);
    const runtime = await app.queryClient.fetchQuery(threadRuntimeQuery(thread));
    let pending: ThreadRuntime["pending"] = runtime.pending;
    const requested = vi.fn();
    current.server.use(
      http.get("*/api/v1/chat/threads/:threadId/runtime", () => HttpResponse.json({ ...runtime, pending })),
      http.post("*/api/v1/connections/:connectionId/login-session", async ({ params, request }) => {
        requested(params.connectionId, await request.text());
        return HttpResponse.json(
          { ticket: "discard-http-ticket", wsUrl: "/api/v1/login-sessions/L/ws", expiresAt: AT },
          { status: 201 },
        );
      }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Открыть браузер" }));
    await waitFor(() => expect(requested).toHaveBeenCalledExactlyOnceWith(started.connection.id, ""), WAIT);
    expect(screen.queryByText("Ручной вход")).toBeNull();
    expect(screen.getByLabelText("Ответ")).toBeDisabled();
    const fresh = parseBlock({
      type: "login",
      id: "01JAAAAAAAAAAAAAAAAAAAAA02",
      at: AT,
      connectionId: started.connection.id,
      wsUrl: "/api/v1/login-sessions/fresh/ws",
      ticket: "sse-only-ticket",
      expiresAt: AT,
    });
    if (!fresh) throw new Error("invalid login fixture");
    pending = [{ jobId: started.jobId, blockId: fresh.id, channel: "manual", secret: false }];
    const snapshot = jobManager.get(started.jobId);
    if (!snapshot) throw new Error("missing job");
    act(() => jobManager.seed({ ...snapshot, lastEventId: "999", blocks: [...snapshot.blocks, fresh] }));
    await act(() => app.queryClient.invalidateQueries({ queryKey: threadRuntimeKey(thread) }));
    expect(await screen.findByText("Ручной вход", undefined, WAIT)).toBeInTheDocument();
    expect(screen.queryByLabelText("Ответ")).toBeNull();
    expect(jobManager.get(started.jobId)?.answers).toEqual({});
    const cached = JSON.stringify(
      app.queryClient
        .getQueryCache()
        .getAll()
        .map((query) => query.state.data),
    );
    expect(cached).not.toContain("discard-http-ticket");
    expect(cached).not.toContain("sse-only-ticket");
    expect(window.sessionStorage.getItem("botai.activeJob")).not.toContain("ticket");
  });

  it("manual API 404 виден человеку, без вечного spinner и без подставного ответа", async () => {
    const started = await legacyJob();
    current.server.use(
      http.post("*/api/v1/connections/:connectionId/login-session", () =>
        HttpResponse.json(
          { code: "not_found", detail: "Экран входа уже закрыт. Начните вход заново." },
          { status: 404 },
        ),
      ),
    );
    renderApp(`/connect/${started.jobId}`);
    await userEvent.click(await screen.findByRole("button", { name: "Открыть браузер" }, WAIT));
    expect(
      await screen.findByText(/Браузер пока недоступен для этого шага входа/, undefined, WAIT),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Открыть браузер" })).toBeEnabled();
    expect(screen.queryByText("Открываю браузер…")).toBeNull();
    expect(jobManager.get(started.jobId)?.answers).toEqual({});
  });

  it("done+login_failed не выглядит успехом; новый вход сохраняет connection и canonical thread", async () => {
    const started = await legacyJob();
    const sim = current.db.sims.get(started.jobId);
    if (!sim) throw new Error("missing sim");
    const app = renderApp(`/connect/${started.jobId}`);
    await screen.findByRole("button", { name: "Открыть браузер" }, WAIT);
    const thread = app.router.state.location.search.thread;
    started.connection.status = "error";
    const stored = current.db.connections.find((item) => item.id === started.connection.id);
    if (stored) stored.status = "error";
    act(() => sim.emit("done", { result: { status: "error", detail: "login_failed" } }));
    await userEvent.click(await screen.findByRole("button", { name: "Войти заново" }, WAIT));
    await waitFor(() => expect(useActiveJob.getState().job?.jobId).not.toBe(started.jobId), WAIT);
    expect(app.router.state.location.search.thread).toBe(thread);
    expect(useActiveJob.getState().job?.connectionId).toBe(started.connection.id);
    expect(current.db.connections).toHaveLength(1);
    expect(await screen.findByRole("button", { name: "Открыть браузер" }, WAIT)).toBeInTheDocument();
  });
});
