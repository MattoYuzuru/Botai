import { describe, expect, it } from "vitest";
import { lintFiles, lintSource, RULES } from "./design-lint.mjs";

const rules = (path, source) => lintSource(path, source).map((problem) => problem.rule);
const APP = "apps/web/src/screens/today.tsx";

describe("design-lint: каждое правило §10 из стайлгайда есть", () => {
  it("реестр правил содержит все 28 имён", () => {
    const expected = [
      "native-control",
      "raw-color",
      "arbitrary-visual",
      "off-grid-spacing",
      "raw-font",
      "font-too-small",
      "touch-target",
      "pseudo-icon",
      "inline-svg",
      "inline-visual-style",
      "browser-dialog",
      "raw-scroll",
      "vh-unit",
      "no-safe-area",
      "viewport-lock",
      "raw-keyframes",
      "unguarded-motion",
      "semantic-color-override",
      "accent-hue-out-of-window",
      "dash-placeholder",
      "retry-button",
      "fabricated-progress",
      "mascot-data",
      "agent-block-html",
      "icon-import",
      "fill-as-text",
      "input-font-size",
      "extra-tab",
    ];
    expect(RULES.map((rule) => rule.id).sort()).toEqual(expected.sort());
  });
});

describe("design-lint: правила", () => {
  it("native-control", () => {
    expect(rules(APP, "<button onClick={x}>Готово</button>")).toContain("native-control");
    expect(rules(APP, '<input type="text" />')).toContain("native-control");
    expect(rules(APP, "<Button>Готово</Button>")).toEqual([]);
  });

  it("raw-color", () => {
    expect(rules(APP, '<div className="bg-[#ff0000]" />')).toContain("raw-color");
    expect(rules("apps/web/src/a.css", "a { color: rgb(1 2 3) }")).toContain("raw-color");
    expect(rules("apps/web/src/a.css", "a { color: oklch(0.5 0.1 200) }")).toContain("raw-color");
    expect(rules(APP, 'const c = "#1e4fb1"')).toContain("raw-color");
    expect(rules(APP, '<div className="bg-surface text-fg-muted" />')).toEqual([]);
    expect(rules(APP, '<a href="#main">К содержимому</a>')).toEqual([]);
  });

  it("arbitrary-visual", () => {
    expect(rules(APP, '<p className="text-[15px]" />')).toContain("arbitrary-visual");
    expect(rules(APP, '<p className="rounded-[10px]" />')).toContain("arbitrary-visual");
    expect(rules(APP, '<p className="duration-[400ms]" />')).toContain("arbitrary-visual");
    expect(rules(APP, '<p className="hover:p-[7px]" />')).toContain("arbitrary-visual");
    expect(rules(APP, '<p className="data-[state=open]:bg-hover w-[22rem]" />')).toEqual([]);
  });

  it("off-grid-spacing", () => {
    expect(rules(APP, '<p className="p-7" />')).toContain("off-grid-spacing");
    expect(rules(APP, '<p className="gap-0.5 px-4" />')).toContain("off-grid-spacing");
    expect(rules(APP, '<p className="-mt-9" />')).toContain("off-grid-spacing");
    expect(rules(APP, '<p className="p-4 px-1.5 gap-x-2.5 mt-16 m-0 pb-safe-2" />')).toEqual([]);
  });

  it("raw-font", () => {
    expect(rules("apps/web/src/a.css", "a { font-family: Arial }")).toContain("raw-font");
    expect(rules(APP, "<p className=\"font-['Inter']\" />")).toContain("raw-font");
    expect(rules("apps/web/src/a.css", "@font-face { }")).toContain("raw-font");
  });

  it("font-too-small", () => {
    expect(rules(APP, '<p className="text-[12px]" />')).toContain("font-too-small");
    expect(rules("apps/web/src/a.css", "a { font-size: 11px }")).toContain("font-too-small");
    expect(rules("apps/web/src/a.css", "a { font-size: 0.75rem }")).toContain("font-too-small");
    expect(rules("apps/web/src/a.css", "a { font-size: 0.8125rem }")).toEqual([]);
  });

  it("touch-target", () => {
    expect(rules(APP, '<Button size="sm">Ещё</Button>')).toContain("touch-target");
    expect(rules(APP, '<IconButton size="sm" icon="more" label="Ещё" />')).toContain("touch-target");
    expect(rules(APP, '<Button size="sm" data-touch-expanded="">Ещё</Button>')).toEqual([]);
    expect(rules(APP, '<Button size="md">Ещё</Button>')).toEqual([]);
    expect(rules(APP, '<a href="/x" className="size-8">x</a>')).toContain("touch-target");
    expect(rules(APP, '<a href="/x" className="size-11">x</a>')).toEqual([]);
  });

  it("pseudo-icon: символы и эмодзи, но не разделитель «·»", () => {
    expect(rules(APP, "<span>Далее →</span>")).toContain("pseudo-icon");
    expect(rules(APP, "<span>Готово ✓</span>")).toContain("pseudo-icon");
    expect(rules(APP, "<span>Готово 🎉</span>")).toContain("pseudo-icon");
    expect(rules(APP, "<span>Думает · 12 с</span>")).toEqual([]);
  });

  it("inline-svg", () => {
    expect(rules(APP, '<svg viewBox="0 0 16 16">')).toContain("inline-svg");
    expect(rules("packages/design/src/ui/icon/icon.tsx", "<svg />")).toEqual([]);
  });

  it("inline-visual-style", () => {
    expect(rules(APP, "<div style={{ color: x }} />")).toContain("inline-visual-style");
    expect(rules(APP, "<div style={{ background: x }} />")).toContain("inline-visual-style");
    expect(rules(APP, "<div style={{ backgroundColor: x }} />")).toContain("inline-visual-style");
    expect(rules(APP, "<div style={{ borderRadius: 4 }} />")).toContain("inline-visual-style");
    expect(rules(APP, "<div style={{ width: w }} />")).toEqual([]);
  });

  it("browser-dialog", () => {
    expect(rules("apps/web/src/a.ts", 'if (confirm("Удалить?")) {}')).toContain("browser-dialog");
    expect(rules("apps/web/src/a.ts", 'window.alert("x")')).toContain("browser-dialog");
    expect(rules(APP, '<span title="Удалить">x</span>')).toContain("browser-dialog");
    expect(rules(APP, '<EmptyState title="Пусто" icon="deadline" />')).toEqual([]);
  });

  it("raw-scroll", () => {
    expect(rules(APP, '<div className="h-full overflow-y-auto" />')).toContain("raw-scroll");
    expect(rules("apps/web/src/a.css", ".x { overflow-x: auto }")).toContain("raw-scroll");
    expect(rules(APP, '<div className="overflow-hidden" />')).toEqual([]);
  });

  it("vh-unit", () => {
    expect(rules(APP, '<div className="h-screen" />')).toContain("vh-unit");
    expect(rules("apps/web/src/a.css", ".x { height: 100vh }")).toContain("vh-unit");
    expect(rules("apps/web/src/a.css", ".x { height: 100dvh }")).toEqual([]);
    expect(rules(APP, '<div className="min-h-dvh" />')).toEqual([]);
  });

  it("no-safe-area", () => {
    expect(rules(APP, '<nav className="fixed inset-x-0 bottom-0" />')).toContain("no-safe-area");
    expect(rules(APP, '<header className="sticky top-0" />')).toContain("no-safe-area");
    expect(rules(APP, '<nav className="fixed inset-x-0 bottom-0 pb-safe-2" />')).toEqual([]);
    expect(rules(APP, '<header className="sticky top-0 pt-safe" />')).toEqual([]);
    expect(rules(APP, '<div className="sticky top-4" />')).toEqual([]);
  });

  it("viewport-lock", () => {
    expect(rules("apps/web/index.html", '<meta name="viewport" content="user-scalable=no">')).toContain(
      "viewport-lock",
    );
    expect(rules("apps/web/index.html", '<meta name="viewport" content="maximum-scale=1">')).toContain(
      "viewport-lock",
    );
    expect(
      rules("apps/web/index.html", '<meta name="viewport" content="width=device-width, initial-scale=1">'),
    ).toEqual([]);
  });

  it("raw-keyframes и unguarded-motion", () => {
    expect(rules("apps/web/src/a.css", "@keyframes spin { }")).toContain("raw-keyframes");
    expect(rules("apps/web/src/a.css", ".x { animation: spin 1s }")).toContain("raw-keyframes");
    expect(rules("apps/web/src/a.css", ".x { transition: all 1s }")).toContain("unguarded-motion");
    expect(rules(APP, "<div style={{ transition: 'all 1s' }} />")).toContain("unguarded-motion");
    expect(
      rules(APP, '<div className="transition-colors duration-fast ease-soft animate-fade-in" />'),
    ).toEqual([]);
  });

  it("semantic-color-override", () => {
    expect(rules("apps/web/src/a.css", "[data-space=x] { --color-danger: red }")).toContain(
      "semantic-color-override",
    );
    expect(rules("apps/web/src/a.css", "[data-space=x] { --color-accent: var(--y) }")).toEqual([]);
  });

  it("accent-hue-out-of-window", () => {
    expect(rules("packages/design/src/lib/x.ts", '{ id: "x", hue: 80 }')).toContain(
      "accent-hue-out-of-window",
    );
    expect(rules("packages/design/src/lib/x.ts", '{ id: "x", hue: 350 }')).toContain(
      "accent-hue-out-of-window",
    );
    expect(rules("packages/design/src/lib/x.ts", '{ id: "x", hue: 292 }')).toEqual([]);
  });

  it("dash-placeholder", () => {
    expect(rules("apps/web/src/a.ts", "value ?? '—'")).toContain("dash-placeholder");
    expect(rules(APP, "<td>—</td>")).toContain("dash-placeholder");
    expect(rules(APP, "<p>Договор — за 15 дней</p>")).toEqual([]);
  });

  it("retry-button: ни «Повторить», ни «Обновить», ни «Загрузить»", () => {
    expect(rules(APP, "<Button>Повторить</Button>")).toContain("retry-button");
    expect(rules(APP, "<Button>Обновить</Button>")).toContain("retry-button");
    expect(rules(APP, "<Button>Попробовать снова</Button>")).toContain("retry-button");
    expect(rules(APP, 'const label = "Загрузить"')).toContain("retry-button");
    expect(rules(APP, "<Error onRetry={refetch} />")).toContain("retry-button");
    expect(rules(APP, "<Button>Обновить пароль</Button>")).toEqual([]);
    expect(rules(APP, "<Button>Ответить</Button>")).toEqual([]);
  });

  it("fabricated-progress", () => {
    expect(rules(APP, "<span>{progress}%</span>")).toContain("fabricated-progress");
    expect(rules(APP, "<span>ETA {minutes}</span>")).toContain("fabricated-progress");
    expect(rules(APP, "<ProgressRail etaKnown={false} stage={2} />")).toEqual([]);
  });

  it("mascot-data", () => {
    expect(rules(APP, "<MascotStage situation={s} profile={user} />")).toContain("mascot-data");
    expect(rules(APP, "Varya.mount(el, { messages })")).toContain("mascot-data");
    expect(rules(APP, '<MascotStage situation="sync" size={36} />')).toEqual([]);
  });

  it("agent-block-html", () => {
    expect(rules(APP, "<div dangerouslySetInnerHTML={{ __html: x }} />")).toContain("agent-block-html");
    expect(rules(APP, 'el.innerHTML = "<b>x</b>"')).toContain("agent-block-html");
    expect(rules(APP, '<a href="javascript:alert(1)">x</a>')).toContain("agent-block-html");
  });

  it("icon-import", () => {
    expect(rules(APP, 'import { IconCheck } from "@tabler/icons-react";')).toContain("icon-import");
    expect(
      rules("packages/design/src/ui/icon/registry.ts", 'import { IconCheck } from "@tabler/icons-react";'),
    ).toEqual([]);
  });

  it("fill-as-text: accent и danger — только заливки", () => {
    expect(rules(APP, '<p className="text-accent" />')).toContain("fill-as-text");
    expect(rules(APP, '<p className="text-danger" />')).toContain("fill-as-text");
    expect(rules(APP, '<p className="border-danger" />')).toContain("fill-as-text");
    expect(rules(APP, '<p className="fill-accent" />')).toContain("fill-as-text");
    expect(rules(APP, '<p className="hover:text-accent/80" />')).toContain("fill-as-text");
    expect(
      rules(APP, '<p className="text-accent-fg border-danger-fg bg-accent text-danger-fg bg-danger" />'),
    ).toEqual([]);
  });

  it("input-font-size", () => {
    expect(rules(APP, '<Input className="type-ui" />')).toContain("input-font-size");
    expect(rules(APP, '<Textarea className="text-[14px]" />')).toContain("input-font-size");
    expect(rules("apps/web/src/a.css", "input { font-size: 14px; }")).toContain("input-font-size");
    expect(rules(APP, '<Input className="w-full" />')).toEqual([]);
  });

  it("extra-tab: ровно пять вкладок §7.1", () => {
    const six = [
      "import type { TabItem } from '@botai/design'",
      "const TABS: TabItem[] = [",
      '  { id: "today", label: "Сегодня", icon: "today" },',
      '  { id: "schedule", label: "Расписание", icon: "schedule" },',
      '  { id: "tasks", label: "Задания", icon: "deadline" },',
      '  { id: "chat", label: "Чат", icon: "chat" },',
      '  { id: "study", label: "Учёба", icon: "university" },',
      '  { id: "settings", label: "Настройки", icon: "settings" },',
      "]",
    ].join("\n");
    expect(rules("apps/web/src/app/nav.ts", six)).toContain("extra-tab");
    const five = six.replace('  { id: "settings", label: "Настройки", icon: "settings" },\n', "");
    expect(rules("apps/web/src/app/nav.ts", five)).toEqual([]);
    const sixthKnown = six.replace('id: "settings"', 'id: "study"');
    expect(rules("apps/web/src/app/nav.ts", sixthKnown)).toContain("extra-tab");
  });
});

describe("design-lint: исключения и области", () => {
  it("не действует в ui, styles, вендоренном маскоте, тестах, моках и сгенерированных файлах", () => {
    const bad = "<button style={{ color: 'red' }}>Повторить</button>";
    for (const path of [
      "packages/design/src/ui/button/button.tsx",
      "packages/design/src/styles/tokens.css",
      "packages/design/src/mascot/vendor/varya.js",
      "apps/web/src/screens/a.test.tsx",
      "apps/web/src/mocks/handlers.ts",
      "apps/web/src/api/schema.gen.ts",
    ]) {
      expect(rules(path, bad), path).toEqual([]);
    }
  });

  it("design-lint-ignore с причиной пропускает строку, без причины — нарушение", () => {
    expect(rules(APP, "<button /> // design-lint-ignore: запасной путь для менеджера паролей")).toEqual([]);
    expect(rules(APP, "<button /> // design-lint-ignore")).toEqual(["ignore-without-reason"]);
    expect(rules(APP, "// design-lint-ignore: сторонний виджет\n<button />\n<button />")).toEqual([
      "native-control",
    ]);
  });

  it("комментарии не проверяются", () => {
    expect(rules(APP, "// пример: <button> и #ff0000")).toEqual([]);
    expect(rules(APP, "/** Не используйте text-accent */")).toEqual([]);
  });

  it("чужие расширения игнорируются", () => {
    expect(rules("apps/web/src/a.md", "<button />")).toEqual([]);
  });
});

describe("design-lint: репозиторий", () => {
  it("в apps/web и packages/design нет нарушений (тот же прогон, что `node scripts/design-lint.mjs`)", () => {
    const problems = lintFiles().map((p) => `${p.file}:${p.line} [${p.rule}]`);
    expect(problems).toEqual([]);
  });
});
