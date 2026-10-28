import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { api, call } from "@/api/client";
import { useActiveJob } from "@/features/jobs/active-job";
import { parseBlock } from "@/features/jobs/blocks";
import { jobManager } from "@/features/jobs/job-manager";
import { initialJob } from "@/features/jobs/job-state";
import { renderApp } from "@/test/render-app";
import { createTestServer } from "@/test/server";

vi.mock("@/features/remote-screen/remote-screen", () => ({ RemoteScreen: () => <div>Ручной вход</div> }));

const current = createTestServer("empty");
const ID = (n: number) => `01JAAAAAAAAAAAAAAAAAAAAA${String(n).padStart(2, "0")}`;
const JOB = ID(1);
const CONN = ID(9);
const AT = "2026-10-28T10:00:00Z";
const auth = parseBlock({
  type: "input",
  id: ID(2),
  at: AT,
  question: "Логин",
  inputKind: "text",
  autocomplete: "username",
  required: true,
});
if (!auth) throw new Error("invalid fixture");

beforeAll(() => current.server.listen({ onUnhandledRequest: "error" }));
afterAll(() => current.server.close());
beforeEach(() => {
  jobManager.stopAll();
  useActiveJob.setState({ job: null });
  window.sessionStorage.clear();
});
afterEach(() => {
  current.server.resetHandlers();
  act(() => jobManager.stopAll());
});

describe("ConnectProgress: источник подключения и явный новый вход", () => {
  it("переоткрытый URL без active metadata получает JobOut из общего fixture и Готово не восстанавливает job обратно", async () => {
    const space = await call(api.POST("/api/v1/spaces", { body: { title: "Учебный стенд" } }));
    const started = await call(
      api.POST("/api/v1/spaces/{space_id}/connections", {
        params: { path: { space_id: space.id } },
        body: { title: "Кабинет", kind: "portal", url: "https://lk.fakeuni.test/" },
      }),
    );
    const metadata = await call(
      api.GET("/api/v1/jobs/{job_id}", { params: { path: { job_id: started.jobId } } }),
    );
    expect(metadata).toMatchObject({
      id: started.jobId,
      connectionId: started.connection.id,
      kind: "connect",
      state: "waiting_user",
    });
    const { router } = renderApp(`/connect/${started.jobId}`);
    expect(await screen.findByRole("button", { name: "Открыть браузер" })).toBeInTheDocument();
    expect(useActiveJob.getState().job).toMatchObject({
      jobId: started.jobId,
      connectionId: started.connection.id,
    });
    act(() => jobManager.seed({ ...initialJob(started.jobId), state: "done", result: { status: "ok" } }));
    await userEvent.click(screen.getByRole("button", { name: "Готово" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    expect(useActiveJob.getState().job).toBeNull();
  });

  it("direct reload получает connectionId из настоящего Job API и отбрасывает HTTP ticket до SSE", async () => {
    jobManager.seed({ ...initialJob(JOB), state: "waiting_user", blocks: [auth] });
    // Вкладка могла наблюдать другое задание: его connectionId нельзя угадать для открытого URL.
    useActiveJob.getState().set({ jobId: ID(6), connectionId: ID(8), title: "Другая работа" });
    const requested = vi.fn();
    current.server.use(
      http.get("*/api/v1/jobs/:jobId", ({ params }) => {
        expect(params.jobId).toBe(JOB);
        return HttpResponse.json({
          id: JOB,
          connectionId: CONN,
          kind: "connect",
          state: "waiting_user",
          stage: "Жду логин",
          createdAt: AT,
          startedAt: AT,
          finishedAt: null,
        });
      }),
      http.post("*/api/v1/connections/:connectionId/login-session", async ({ params, request }) => {
        requested(params.connectionId, await request.text());
        return HttpResponse.json(
          { ticket: "discard-http-ticket", wsUrl: "/api/v1/login-sessions/L/ws", expiresAt: AT },
          { status: 201 },
        );
      }),
    );
    const { queryClient } = renderApp(`/connect/${JOB}`);
    await userEvent.click(await screen.findByRole("button", { name: "Открыть браузер" }));
    await waitFor(() => expect(requested).toHaveBeenCalledExactlyOnceWith(CONN, ""));
    expect(screen.getByText("Открываю браузер…")).toBeInTheDocument();
    expect(screen.getByLabelText("Ответ")).toBeDisabled();
    expect(
      JSON.stringify(
        queryClient
          .getQueryCache()
          .getAll()
          .map((query) => query.state.data),
      ),
    ).not.toContain("discard-http-ticket");
    expect(window.sessionStorage.getItem("botai.activeJob")).not.toContain("discard-http-ticket");
    expect(useActiveJob.getState().job).toMatchObject({ jobId: JOB, connectionId: CONN });
    expect(jobManager.get(JOB)?.answers).toEqual({});
  });

  it("из уведомления открывается тот же вопрос с browser option; login_failed начинает новый job вместо пустого чата", async () => {
    jobManager.seed({ ...initialJob(JOB), state: "waiting_user", blocks: [auth] });
    useActiveJob.getState().set({ jobId: JOB, connectionId: CONN, title: "Вхожу в вуз", spaceTitle: "Вуз" });
    const next = ID(7);
    jobManager.seed({ ...initialJob(next), state: "waiting_user", blocks: [auth] });
    const sync = vi.fn();
    current.server.use(
      http.post("*/api/v1/connections/:connectionId/sync", ({ params }) => {
        sync(params.connectionId);
        return HttpResponse.json({ jobId: next }, { status: 202 });
      }),
    );
    const { router } = renderApp("/");
    await userEvent.click(await screen.findByRole("link", { name: "Открыть" }));
    expect(await screen.findByRole("button", { name: "Открыть браузер" })).toBeInTheDocument();
    act(() =>
      jobManager.seed({
        ...initialJob(JOB),
        state: "done",
        result: { status: "error", detail: "login_failed" },
      }),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Войти заново" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/connect/${next}`));
    expect(sync).toHaveBeenCalledExactlyOnceWith(CONN);
    expect(useActiveJob.getState().job).toMatchObject({ jobId: next, connectionId: CONN });
    expect(await screen.findByRole("button", { name: "Открыть браузер" })).toBeInTheDocument();
  });
});
