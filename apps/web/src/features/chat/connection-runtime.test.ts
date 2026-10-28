import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { api, call } from "@/api/client";
import { ApiError } from "@/api/errors";
import { clientId } from "@/lib/client-id";
import { createTestServer } from "@/test/server";
import { sendConnectText, startConnectThread, threadRuntimeQuery } from "./connection-api";

let current = createTestServer("empty");
beforeAll(() => current.server.listen({ onUnhandledRequest: "error" }));
afterAll(() => current.server.close());
beforeEach(() => {
  current.server.close();
  current = createTestServer("empty");
  current.server.listen({ onUnhandledRequest: "error" });
});
afterEach(() => current.server.resetHandlers());
const runtime = async (id: string) => {
  const response = await fetch(`http://localhost/api/v1/chat/threads/${id}/runtime`);
  return response.json() as Promise<import("./connection-api").ThreadRuntime>;
};
const messages = (id: string) =>
  call(api.GET("/api/v1/chat/threads/{thread_id}/messages", { params: { path: { thread_id: id } } }));

describe("граница direct connection dialogue", () => {
  it("повтор creation/send с тем же client id возвращает тот же thread/job/message", async () => {
    const key = clientId();
    const first = await startConnectThread({ clientRequestId: key });
    const second = await startConnectThread({ clientRequestId: key });
    expect(second.id).toBe(first.id);
    expect(current.db.sims.size).toBe(1);
    const job = (await runtime(first.id)).activeJob;
    if (!job) throw new Error("missing source job");
    const messageId = clientId();
    const accepted = await sendConnectText(first.id, "Я уточню ссылку чуть позже", messageId, job.id);
    const replay = await sendConnectText(first.id, "Я уточню ссылку чуть позже", messageId, job.id);
    expect(replay).toEqual(accepted);
    expect(accepted.jobId).toBe(job.id);
    expect((await messages(first.id)).filter((item) => item.role === "user")).toHaveLength(1);
    expect(threadRuntimeQuery(first.id).queryKey).toEqual(["chat", "threads", first.id, "runtime"]);
  });

  it("native/secret race отвергается до persistence свободного текста", async () => {
    const thread = await startConnectThread({ clientRequestId: clientId() });
    const job = (await runtime(thread.id)).activeJob;
    if (!job) throw new Error("missing source job");
    await sendConnectText(thread.id, "https://lk.fakeuni.test/", clientId(), job.id);
    expect((await runtime(thread.id)).pending[0]?.channel).toBe("native");
    await expect(
      sendConnectText(thread.id, "must-not-be-a-chat-message", clientId(), job.id),
    ).rejects.toMatchObject({ status: 409 });
    expect(JSON.stringify(await messages(thread.id))).not.toContain("must-not-be-a-chat-message");
  });

  it("явный stale CONNECT target отвергается; без target после terminal начинается обычный CHAT в том же thread", async () => {
    const thread = await startConnectThread({ clientRequestId: clientId() });
    const job = (await runtime(thread.id)).activeJob;
    if (!job) throw new Error("missing source job");
    current.db.sims.get(job.id)?.cancel();
    try {
      await sendConnectText(thread.id, "Уточнение к закрытой попытке", clientId(), job.id);
      throw new Error("stale target accepted");
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError);
      expect(error).toMatchObject({ status: 409 });
    }
    const normal = await sendConnectText(thread.id, "Расскажи про пары", clientId());
    expect(normal.jobId).not.toBe(job.id);
    expect(current.db.sims.get(normal.jobId)?.kind).toBe("chat");
    expect(JSON.stringify(await messages(thread.id))).not.toContain("Уточнение к закрытой попытке");
  });
});
