import { describe, expect, it } from "vitest";
import {
  EVENTS_PAGE_SIZE,
  isAgentRunsUnavailable,
  isRunning,
  runIdFromResult,
  runTraceHref,
} from "./agent-runs";
import { ApiError, NetworkError } from "./errors";

describe("agent runs: адрес и наличие", () => {
  it("ссылка на трассу кодирует идентификатор и не ведёт на API", () => {
    expect(runTraceHref("01JMKCDB00000000000000RUN")).toBe("/agent/runs/01JMKCDB00000000000000RUN");
    expect(runTraceHref(" a/b ")).toBe("/agent/runs/a%2Fb");
  });

  it("404/501 — v4 выключен, а сеть и 5xx — настоящая ошибка", () => {
    expect(isAgentRunsUnavailable(new ApiError(404, "not_found", ""))).toBe(true);
    expect(isAgentRunsUnavailable(new ApiError(501, "not_implemented", ""))).toBe(true);
    expect(isAgentRunsUnavailable(new ApiError(500, "internal_error", ""))).toBe(false);
    expect(isAgentRunsUnavailable(new ApiError(401, "unauthorized", ""))).toBe(false);
    expect(isAgentRunsUnavailable(new NetworkError())).toBe(false);
  });

  it("идущий ход — единственное состояние, которое опрашивается", () => {
    expect(isRunning({ status: "running" })).toBe(true);
    expect(isRunning({ status: "answer" })).toBe(false);
    expect(isRunning(undefined)).toBe(false);
  });

  it("страница шагов — 100, сервер разрешает до 500", () => {
    expect(EVENTS_PAGE_SIZE).toBe(100);
  });
});

describe("run_id из результата задания", () => {
  it("берёт только непустую строку: у прежних заданий ссылки не будет", () => {
    expect(runIdFromResult({ run_id: "run-1", status: "answer" })).toBe("run-1");
    expect(runIdFromResult({ run_id: "  run-1  " })).toBe("run-1");
    expect(runIdFromResult({ run_id: 42 })).toBeNull();
    expect(runIdFromResult({ run_id: "" })).toBeNull();
    expect(runIdFromResult({ status: "answer" })).toBeNull();
    expect(runIdFromResult(null)).toBeNull();
    expect(runIdFromResult(["run-1"])).toBeNull();
    expect(runIdFromResult("run-1")).toBeNull();
  });
});
