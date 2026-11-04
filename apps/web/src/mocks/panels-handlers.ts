/**
 * MSW-обработчики панелей «Учёбы» (D-039): `GET /api/v1/panels`, `PATCH /panels/{id}`, `accept`, `dismiss`.
 * Форма и правила — как у сервера (`docs/v4/23-panels-backend.md` §6): выключенные студентом панели
 * приходят с `data: null`, предложения агента — отдельным статусом, чужая панель — 404, неверный переход — 409.
 *
 * Состояния для стенда и e2e: `?panels=none` (панелей нет), `?panels=fail` (сбой сервера),
 * `?panels=unknown` (среди панелей одна неизвестного типа — клиент обязан её скрыть).
 */
import { delay, HttpResponse, http } from "msw";
import type { components } from "@/api/schema.gen";
import { findUser, type MockDb, type MockPanel, persistDb } from "./db";
import { panelData } from "./panels-seed";

type S = components["schemas"];

function problem(status: number, code: string, detail: string) {
  return new HttpResponse(
    JSON.stringify({ type: `https://botai.mnema.app/errors/${code}`, title: code, status, code, detail }),
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

type Variant = "none" | "fail" | "unknown" | null;

function readVariant(): Variant {
  try {
    const value = new URLSearchParams(window.location.search).get("panels");
    return value === "none" || value === "fail" || value === "unknown" ? value : null;
  } catch {
    return null;
  }
}

export function createPanelsHandlers(
  db: MockDb,
  options: { latency?: number; persist?: boolean; variant?: Variant } = {},
) {
  const { latency = 0, persist = false } = options;
  const wait = async () => {
    if (latency > 0) await delay(latency);
  };
  const variant = () => (options.variant !== undefined ? options.variant : readVariant());
  const signedIn = () => Boolean(findUser(db, db.sessionUserId));
  const unauthorized = () => problem(401, "unauthorized", "Нужно войти в BotAI");
  const nowIso = () => new Date(db.now()).toISOString();

  const out = (item: MockPanel): S["PanelOut"] | null => {
    const space = db.spaces.find((row) => row.id === item.spaceId);
    if (!space) return null;
    const { spaceId: _spaceId, ...rest } = item;
    return {
      ...rest,
      space: {
        id: space.id,
        title: space.title,
        shortTitle: space.shortTitle ?? space.title,
        accentHue: space.accentHue,
      },
      data: panelData(db, item),
    };
  };
  const order = (a: MockPanel, b: MockPanel) => {
    const created = (id: string) => db.spaces.findIndex((space) => space.id === id);
    return created(a.spaceId) - created(b.spaceId) || a.position - b.position;
  };
  const found = (id: string) => db.panels.find((item) => item.id === id);
  const save = () => {
    if (persist) persistDb(db);
  };

  return [
    http.get("*/api/v1/panels", async ({ request }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const mode = variant();
      if (mode === "fail") return problem(500, "internal_error", "Панели временно недоступны");
      if (mode === "none") return HttpResponse.json([]);
      const url = new URL(request.url);
      const spaceId = url.searchParams.get("space_id");
      const status = url.searchParams.get("status") ?? "active";
      const rows = db.panels
        .filter((item) => (spaceId ? item.spaceId === spaceId : true))
        .filter((item) => item.status === status)
        .sort(order)
        .flatMap((item) => out(item) ?? []);
      if (mode === "unknown" && status === "active" && rows[0]) {
        const odd = {
          ...rows[0],
          id: "panel-unknown",
          key: "from-the-future",
          type: "heatmap",
          position: 99,
        };
        rows.push(odd as unknown as S["PanelOut"]);
      }
      return HttpResponse.json(rows);
    }),

    http.patch("*/api/v1/panels/:id", async ({ params, request }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const item = found(String(params.id));
      if (!item) return problem(404, "not_found", "Панель не найдена");
      const body = (await request.json()) as S["PanelPatch"];
      if (body.visible === undefined && body.hiddenInAll === undefined && body.position === undefined)
        return problem(422, "validation", "Нечего менять");
      if (item.status === "dismissed") return problem(409, "conflict", "Панель отклонена");
      if (typeof body.visible === "boolean") item.visible = body.visible;
      if (typeof body.hiddenInAll === "boolean") item.hiddenInAll = body.hiddenInAll;
      if (typeof body.position === "number") item.position = body.position;
      item.updatedAt = nowIso();
      save();
      return HttpResponse.json(out(item));
    }),

    http.post("*/api/v1/panels/:id/:action", async ({ params }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const action = String(params.action);
      if (action !== "accept" && action !== "dismiss")
        return problem(404, "not_found", "Нет такого действия");
      const item = found(String(params.id));
      if (!item) return problem(404, "not_found", "Панель не найдена");
      if (item.status !== "proposed") return problem(409, "conflict", "Панель не предложена агентом");
      item.status = action === "accept" ? "active" : "dismissed";
      item.visible = action === "accept";
      item.updatedAt = nowIso();
      save();
      return HttpResponse.json(out(item));
    }),
  ];
}
