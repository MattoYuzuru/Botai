import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MASCOT_EVENTS } from "./events";
import { MASCOT_ACTS, MASCOT_SITUATIONS, MASCOT_SKINS } from "./types";
import { getVarya, type VaryaApi } from "./varya";

const sprite = (name: string) => join(import.meta.dirname, "vendor", "sprites", name);
const varya = (): VaryaApi => {
  const api = getVarya();
  if (!api) throw new Error("renderer не загрузился");
  return api;
};

describe("векторный renderer Вари", () => {
  it("загружается и отдаёт API", () => {
    const api = varya();
    expect(typeof api.mount).toBe("function");
    expect(typeof api.svg).toBe("function");
  });

  it("список ситуаций, образов и действий в TypeScript совпадает с renderer", () => {
    const api = varya();
    expect(new Set(api.SITUATIONS.map((item) => item.id))).toEqual(new Set(MASCOT_SITUATIONS));
    expect([...api.SKINS].sort()).toEqual([...MASCOT_SKINS].sort());
    expect(Object.keys(api.ACTS).sort()).toEqual([...MASCOT_ACTS].sort());
  });

  it("у каждой ситуации Botai есть поза, у новых нет реплик (реплики — реестр Botai)", () => {
    const original = new Set(["connect", "tip", "deadline", "skips", "night", "class", "done"]);
    for (const item of varya().SITUATIONS) {
      expect(item.pose.eyes, item.id).toBeTruthy();
      if (!original.has(item.id)) expect(item.lines, item.id).toEqual([]);
    }
  });

  it("спрайты в sprites/ соответствуют коду renderer (export.mjs запущен после правок)", () => {
    const api = varya();
    const expected: [string, string][] = [
      ["varya.svg", api.svg({ id: "varya-ring" })],
      ["varya-pj.svg", api.svg({ theme: "pj", id: "varya-ring" })],
      ...api.SITUATIONS.map((item): [string, string] => [
        `varya-${item.id}.svg`,
        api.svg({ situation: item.id, id: "varya-ring" }),
      ]),
      ...MASCOT_SKINS.map((skin): [string, string] => [
        `varya-skin-${skin}.svg`,
        api.svg({ skin, id: "varya-ring" }),
      ]),
    ];
    for (const [name, body] of expected) {
      expect(existsSync(sprite(name)), name).toBe(true);
      expect(readFileSync(sprite(name), "utf8"), name).toBe(`${body}\n`);
    }
  });

  it("у каждого сезонного окна есть готовый спрайт для статичного режима", () => {
    for (const event of MASCOT_EVENTS) {
      expect(existsSync(sprite(event.staticFallback)), `${event.eventId}: ${event.staticFallback}`).toBe(
        true,
      );
    }
  });

  it("у каждого предмета, который держит ситуация, в CSS есть правило показа (иначе знак невидим)", () => {
    const api = varya() as VaryaApi & { CSS: string };
    const used = new Set<string>();
    for (const item of api.SITUATIONS) {
      for (const prop of [...(item.hold?.props ?? []), ...(item.pose.props ?? [])]) used.add(prop);
    }
    expect(used.size).toBeGreaterThan(8);
    for (const prop of used) {
      expect(api.CSS, `предмет ${prop}`).toContain(`.p-${prop} [data-prop="${prop}"]`);
    }
  });

  it("статичная картинка собирается в браузере кодом и не ходит в сеть", () => {
    const body = varya().svg({ situation: "reauth", skin: "new-year" });
    expect(body).toMatch(/^<svg /);
    expect(body).not.toMatch(/https?:\/\/(?!www\.w3\.org)/);
    expect(body).not.toMatch(/<image|href=|<script/);
  });

  it("портрет для favicon и PWA использует векторную голову без внешних ресурсов", () => {
    for (const maskable of [false, true]) {
      const body = varya().portrait({ maskable });
      expect(body).toContain('class="head"');
      expect(body).not.toMatch(/<image|href=|<script/);
      expect(body).toContain('viewBox="0 0 128 128"');
    }
  });

  it("сезонный образ рисуется только выбранный; без образа слоёв образов нет", () => {
    const api = varya();
    expect(api.svg({ skin: "new-year" })).toContain('class="santa"');
    expect(api.svg({ skin: "leaf" })).toContain('class="leaf"');
    expect(api.svg({ skin: "leaf" })).not.toContain('class="santa"');
    expect(api.svg({ skin: "mug" })).toContain('class="mug"');
    const bare = api.svg({});
    for (const layer of ["santa", "leaf", "mug", "glint"]) expect(bare).not.toContain(`class="${layer}"`);
  });
});

describe("mount: режимы живой Вари", () => {
  let host: HTMLDivElement;
  beforeEach(() => {
    vi.useFakeTimers();
    host = document.createElement("div");
    document.body.appendChild(host);
  });
  afterEach(() => {
    host.remove();
    vi.useRealTimers();
  });

  const classes = (svg: SVGSVGElement) => [...svg.classList];

  it("static не держит таймеров: возобновление, остановка во время моргания и destroy освобождают их", () => {
    const handle = varya().mount(host, { situation: "idle", still: true, idleRange: [100, 100] });
    expect(vi.getTimerCount()).toBe(0);
    handle.setStill(false);
    vi.advanceTimersByTime(100);
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    handle.setStill(true);
    expect(vi.getTimerCount()).toBe(0);
    handle.setStill(false);
    vi.advanceTimersByTime(100);
    handle.destroy();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("системное уменьшение движения останавливает жесты и idle и меняется без перемонтирования", () => {
    const listeners = new Set<(event: { matches: boolean }) => void>();
    const media = vi.spyOn(window, "matchMedia").mockReturnValue({
      matches: true,
      addEventListener: (_: string, listener: (event: { matches: boolean }) => void) =>
        listeners.add(listener),
      removeEventListener: (_: string, listener: (event: { matches: boolean }) => void) =>
        listeners.delete(listener),
    } as unknown as MediaQueryList);
    try {
      const handle = varya().mount(host, { situation: "sync" });
      handle.act("wave");
      expect(handle.still).toBe(true);
      expect(vi.getTimerCount()).toBe(0);
      for (const listener of listeners) listener({ matches: false });
      expect(handle.still).toBe(false);
      expect(handle.svg.classList.contains("l-run")).toBe(true);
      handle.setStill(true);
      for (const listener of listeners) listener({ matches: true });
      for (const listener of listeners) listener({ matches: false });
      expect(handle.still).toBe(true);
      handle.destroy();
      expect(listeners.size).toBe(0);
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      media.mockRestore();
    }
  });

  it("animated: ситуация держит петлю и предметы; sync бежит на месте", () => {
    const handle = varya().mount(host, { situation: "sync", idle: false });
    expect(classes(handle.svg)).toContain("l-run");
    expect(handle.svg.getAttribute("aria-hidden")).toBe("true");
    handle.setSituation("think");
    expect(classes(handle.svg)).toContain("l-tilt");
    expect(classes(handle.svg)).toContain("p-bubbles");
    expect(classes(handle.svg)).not.toContain("l-run");
    handle.destroy();
    expect(host.querySelector("svg")).toBeNull();
  });

  it("still: ни петель, ни реакций, поза ситуации, смысл (знак) остаётся", () => {
    const handle = varya().mount(host, { situation: "overdue", still: true, idle: false });
    expect(handle.svg.classList.contains("still")).toBe(true);
    expect(classes(handle.svg).some((name) => name.startsWith("l-"))).toBe(false);
    expect(classes(handle.svg)).toContain("p-excl");
    handle.react();
    handle.act("wave");
    expect(classes(handle.svg).some((name) => name.startsWith("a-"))).toBe(false);
    handle.setStill(false);
    expect(handle.svg.classList.contains("still")).toBe(false);
    handle.setSituation("sync");
    expect(classes(handle.svg)).toContain("l-run");
    handle.setStill(true);
    expect(classes(handle.svg)).not.toContain("l-run");
    expect(handle.svg.dataset.legs).toBe("b");
    handle.destroy();
  });

  it("reauth и offline показывают смысл предметами: ключ и облако", () => {
    const reauth = varya().mount(host, { situation: "reauth", still: true, idle: false });
    expect(classes(reauth.svg)).toContain("p-key");
    reauth.setSituation("offline");
    expect(classes(reauth.svg)).toContain("p-cloudx");
    reauth.destroy();
  });

  it("react() у ситуаций Botai ничего не говорит: реплик у renderer нет", () => {
    const onSay = vi.fn();
    const handle = varya().mount(host, { situation: "perch", onSay, idle: false });
    for (const id of MASCOT_SITUATIONS) {
      handle.setSituation(id);
      handle.react();
      vi.advanceTimersByTime(4000);
    }
    // Реплики оригинальных ситуаций doodle_vibe в Botai не показываются (в них придуманные числа):
    // обёртка `Mascot` не передаёт onSay, а у ситуаций Botai реплик нет вовсе.
    const botai = ["sync", "think", "overdue", "offline", "reauth", "error", "idle", "perch", "wave"];
    for (const call of onSay.mock.calls) expect(botai).not.toContain((call[2] as { id: string }).id);
    handle.destroy();
  });

  it("act(): класс действия ставится и снимается, потом возврат к hold", () => {
    const handle = varya().mount(host, { situation: "perch", idle: false });
    expect(classes(handle.svg)).toContain("l-calm");
    handle.act("peck");
    expect(classes(handle.svg)).toContain("a-peck");
    expect(classes(handle.svg)).not.toContain("l-calm");
    vi.advanceTimersByTime(2000);
    expect(classes(handle.svg).some((name) => name.startsWith("a-"))).toBe(false);
    expect(classes(handle.svg)).toContain("l-calm");
    handle.destroy();
  });

  it("образ переключается классом skin-…", () => {
    const handle = varya().mount(host, { situation: "idle", skin: "leaf", idle: false });
    expect(classes(handle.svg)).toContain("skin-leaf");
    handle.setSkin("new-year");
    expect(classes(handle.svg)).toContain("skin-new-year");
    expect(classes(handle.svg)).not.toContain("skin-leaf");
    handle.setSkin(null);
    expect(classes(handle.svg).some((name) => name.startsWith("skin-"))).toBe(false);
    handle.destroy();
  });

  it("idle детерминирован: одинаковый генератор — одинаковые взгляды", () => {
    const run = (seed: number) => {
      const handle = varya().mount(host, {
        situation: "idle",
        random: (() => {
          let state = seed;
          return () => {
            state = (state * 16807) % 2147483647;
            return state / 2147483647;
          };
        })(),
      });
      const seen: string[] = [];
      for (let step = 0; step < 30; step++) {
        vi.advanceTimersByTime(500);
        seen.push(handle.svg.dataset.eyes ?? "");
      }
      handle.destroy();
      return seen;
    };
    expect(run(7)).toEqual(run(7));
  });
});
