import { afterEach, describe, expect, it, vi } from "vitest";
import { applyUpdate, type RegisterEnv, registerServiceWorker, usePwaStore } from "./register";

/** Минимальный EventTarget с состоянием, как у ServiceWorker и ServiceWorkerRegistration. */
class FakeWorker extends EventTarget {
  state: ServiceWorkerState = "installing";
  readonly posted: unknown[] = [];
  postMessage(message: unknown) {
    this.posted.push(message);
  }
  become(state: ServiceWorkerState) {
    this.state = state;
    this.dispatchEvent(new Event("statechange"));
  }
}

class FakeRegistration extends EventTarget {
  installing: FakeWorker | null = null;
  waiting: FakeWorker | null = null;
  updates = 0;
  async update() {
    this.updates += 1;
  }
}

function setup(options: { controller?: boolean; enabled?: boolean; failRegister?: boolean } = {}) {
  const registration = new FakeRegistration();
  const container = Object.assign(new EventTarget(), {
    controller: options.controller ? ({} as ServiceWorker) : null,
    register: vi.fn(async () => {
      if (options.failRegister) throw new Error("denied");
      return registration;
    }),
  });
  const documentTarget = Object.assign(new EventTarget(), {
    visibilityState: "visible" as DocumentVisibilityState,
  });
  const reload = vi.fn();
  let nowMs = 1_000_000;
  const env = {
    enabled: options.enabled ?? true,
    container,
    window: Object.assign(new EventTarget(), { document: documentTarget, location: { reload } }),
    now: () => nowMs,
  } as unknown as RegisterEnv;
  return {
    env,
    registration,
    container,
    reload,
    documentTarget,
    advance: (ms: number) => {
      nowMs += ms;
    },
  };
}

afterEach(() => usePwaStore.setState({ waiting: null }));

describe("регистрация service worker", () => {
  it("в dev-режиме и без поддержки браузера ничего не регистрируется", async () => {
    const dev = setup({ enabled: false });
    expect(await registerServiceWorker(dev.env)).toBe(false);
    expect(dev.container.register).not.toHaveBeenCalled();
    expect(await registerServiceWorker({ ...dev.env, enabled: true, container: undefined })).toBe(false);
  });

  it("регистрируется в корне и всегда проверяет sw.js в сети (updateViaCache: none)", async () => {
    const { env, container } = setup();
    expect(await registerServiceWorker(env)).toBe(true);
    expect(container.register).toHaveBeenCalledWith("/sw.js", { scope: "/", updateViaCache: "none" });
  });

  it("сбой регистрации не ломает приложение: оно просто работает без офлайна", async () => {
    const { env } = setup({ failRegister: true });
    expect(await registerServiceWorker(env)).toBe(false);
    expect(usePwaStore.getState().waiting).toBeNull();
  });

  it("первая установка не объявляет «новую версию» и не перезагружает страницу", async () => {
    const { env, registration, container, reload } = setup({ controller: false });
    await registerServiceWorker(env);
    const worker = new FakeWorker();
    registration.installing = worker;
    registration.dispatchEvent(new Event("updatefound"));
    worker.become("installed");
    container.dispatchEvent(new Event("controllerchange"));
    expect(usePwaStore.getState().waiting).toBeNull();
    expect(reload).not.toHaveBeenCalled();
  });

  it("новая версия, пока работает старая: ждёт, объявляется плашкой и применяется по запросу человека", async () => {
    const { env, registration, container, reload } = setup({ controller: true });
    await registerServiceWorker(env);
    const worker = new FakeWorker();
    registration.installing = worker;
    registration.dispatchEvent(new Event("updatefound"));
    expect(usePwaStore.getState().waiting).toBeNull();
    worker.become("installed");
    expect(usePwaStore.getState().waiting).toBe(worker);
    // Сама страница не перезагружается, пока человек не согласился.
    expect(reload).not.toHaveBeenCalled();
    applyUpdate();
    expect(worker.posted).toEqual([{ type: "SKIP_WAITING" }]);
    container.dispatchEvent(new Event("controllerchange"));
    container.dispatchEvent(new Event("controllerchange"));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("страница, открытая до первой установки, перезагружается при следующей смене версии, а не только при первой", async () => {
    const { env, registration, container, reload } = setup({ controller: false });
    await registerServiceWorker(env);
    // Первая установка: service worker берёт страницу, перезагрузки нет.
    (container as { controller: unknown }).controller = {} as ServiceWorker;
    container.dispatchEvent(new Event("controllerchange"));
    expect(reload).not.toHaveBeenCalled();
    // Выходит новая версия: плашка, «Перезапустить», смена управляющего — теперь страница обязана перезагрузиться.
    const worker = new FakeWorker();
    registration.installing = worker;
    registration.dispatchEvent(new Event("updatefound"));
    worker.become("installed");
    expect(usePwaStore.getState().waiting).toBe(worker);
    applyUpdate();
    container.dispatchEvent(new Event("controllerchange"));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("версия, уже ждавшая при открытии страницы, тоже объявляется", async () => {
    const { env, registration } = setup({ controller: true });
    registration.waiting = new FakeWorker();
    await registerServiceWorker(env);
    expect(usePwaStore.getState().waiting).toBe(registration.waiting);
  });

  it("обновления проверяются при возвращении на вкладку, не чаще раза в полчаса", async () => {
    const { env, registration, documentTarget, advance } = setup({ controller: true });
    await registerServiceWorker(env);
    documentTarget.dispatchEvent(new Event("visibilitychange"));
    expect(registration.updates).toBe(0);
    advance(31 * 60_000);
    documentTarget.dispatchEvent(new Event("visibilitychange"));
    expect(registration.updates).toBe(1);
    advance(60_000);
    documentTarget.dispatchEvent(new Event("visibilitychange"));
    expect(registration.updates).toBe(1);
  });
});
