import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AttachmentOut } from "@/api/v3";

const api = vi.hoisted(() => ({ upload: vi.fn(), request: vi.fn() }));
vi.mock("@/api/v3", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/v3")>()),
  uploadAttachment: api.upload,
  v3: api.request,
}));

import { useAttachments } from "./use-attachments";

const doc = (name: string) => new File(["данные"], name, { type: "application/pdf" });
const uploaded = (name: string): AttachmentOut => ({
  id: `srv-${name}`,
  name,
  mime: "application/pdf",
  size: 6,
  kind: "document",
  createdAt: "",
  documentId: null,
  ingest: "none",
});

beforeEach(() => {
  api.upload.mockReset();
  api.request.mockReset().mockResolvedValue(undefined);
});

describe("вложения поля ввода", () => {
  it("предел в десять штук считается сразу: два подряд выбора до перерисовки не обходят его", () => {
    api.upload.mockReturnValue(new Promise(() => {}));
    const { result } = renderHook(() => useAttachments("t1"));
    act(() => {
      result.current.add(
        Array.from({ length: 8 }, (_, index) => doc(`a${index}.pdf`)),
        "picker",
        null,
      );
      result.current.add(
        Array.from({ length: 8 }, (_, index) => doc(`b${index}.pdf`)),
        "picker",
        null,
      );
    });
    expect(result.current.items).toHaveLength(10);
  });

  it("уход из чата удаляет загруженные, но не отправленные вложения на сервере", async () => {
    api.upload.mockImplementation(async (file: File) => uploaded(file.name));
    const { result, unmount } = renderHook(() => useAttachments("t1"));
    act(() => result.current.add([doc("лишнее.pdf")], "picker", null));
    await waitFor(() => expect(result.current.items[0]?.status).toBe("ready"));
    unmount();
    expect(api.request).toHaveBeenCalledWith(
      "DELETE",
      "/api/v1/attachments/srv-%D0%BB%D0%B8%D1%88%D0%BD%D0%B5%D0%B5.pdf",
    );
  });

  it("отправка уносит готовые вложения, а чип с ошибкой остаётся в поле, а не пропадает молча", async () => {
    api.upload.mockImplementation(async (file: File) => uploaded(file.name));
    const { result } = renderHook(() => useAttachments("t1"));
    const big = doc("запись.pdf");
    Object.defineProperty(big, "size", { value: 21 * 1024 * 1024 });
    act(() => result.current.add([doc("хорошее.pdf"), big], "picker", null));
    await waitFor(() => expect(result.current.items.map((item) => item.status)).toEqual(["ready", "failed"]));
    let taken: { id: string }[] = [];
    act(() => {
      taken = result.current.take();
    });
    expect(taken.map((item) => item.id)).toEqual(["srv-хорошее.pdf"]);
    expect(result.current.items.map((item) => [item.name, item.status])).toEqual([["запись.pdf", "failed"]]);
    // Взятое ушло с сообщением: на сервере его не удаляют.
    expect(api.request).not.toHaveBeenCalled();
  });

  it("отказ сервера возвращает загруженные вложения чипами в поле того чата, откуда они уходили", async () => {
    api.upload.mockImplementation(async (file: File) => uploaded(file.name));
    const { result, rerender } = renderHook(({ bucket }) => useAttachments(bucket), {
      initialProps: { bucket: "t1" },
    });
    act(() => result.current.add([doc("конспект.pdf")], "picker", null));
    await waitFor(() => expect(result.current.items[0]?.status).toBe("ready"));
    let taken: Parameters<typeof result.current.restore>[0] = [];
    act(() => {
      taken = result.current.take();
    });
    expect(result.current.items).toEqual([]);
    act(() => result.current.restore(taken, "t1"));
    expect(result.current.items.map((item) => [item.name, item.status])).toEqual([["конспект.pdf", "ready"]]);
    // Повторный возврат того же вложения не дублирует чип.
    act(() => result.current.restore(taken, "t1"));
    expect(result.current.items).toHaveLength(1);
    // Другой чат своих чипов не получает.
    rerender({ bucket: "t2" });
    expect(result.current.items).toEqual([]);
  });
});
