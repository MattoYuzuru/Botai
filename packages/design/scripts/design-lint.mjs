#!/usr/bin/env node
/**
 * design-lint — детерминированная проверка правил дизайн-системы (docs/design/styleguide.md §10).
 *
 * Запуск: `node packages/design/scripts/design-lint.mjs [файлы...]`. Без аргументов проверяет
 * `apps/web/src`, `apps/web/index.html` и `packages/design/src`. Тот же прогон идёт в `pnpm -r test`
 * (design-lint.repo.test.mjs), поэтому `make check` его включает.
 *
 * Правила не действуют внутри дизайн-системы (`ui`, `styles`), в вендоренном маскоте, тестах,
 * моках и сгенерированных файлах. Исключение для строки — комментарий `design-lint-ignore: причина`;
 * без причины само исключение считается нарушением.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { extname, join, relative, resolve, sep } from "node:path";

// import.meta.dirname есть в Node 20.11+; в окружении тестов (jsdom) он тоже определён.
const HERE = import.meta.dirname ?? process.cwd();
const ROOT = resolve(HERE, "../../..");
const SCAN_ROOTS = ["apps/web/src", "packages/design/src"];
const SCAN_FILES = ["apps/web/index.html"];
const EXTENSIONS = new Set([".ts", ".tsx", ".css", ".html"]);

/** Пути (от корня репозитория), где правила не действуют. */
export const EXEMPT = [
  /^packages\/design\/src\/ui\//,
  /^packages\/design\/src\/styles\//,
  /^packages\/design\/src\/mascot\/vendor\//,
  /\.gen\.ts$/,
  /\.test\.(ts|tsx|mjs)$/,
  /(^|\/)test-setup\.ts$/,
  /^apps\/web\/src\/mocks\//,
  /^apps\/web\/src\/test\//,
];

/** Символы, которыми любят заменять иконки. Иконки — только Tabler через <Icon>. Разделитель «·» в текстах разрешён. */
const PSEUDO_ICONS = "→←↑↓↔⟶⟵➜➔➝▶▸►▷◀◂◄▼▾▽▲▴△•●○◦✓✔✗✘✕✖★☆♦◆◇■□▪▫";

/** Допустимые ступени отступа сетки 4 px (styleguide §4). */
const SPACING_STEPS = new Set([
  "0",
  "px",
  "1",
  "1.5",
  "2",
  "2.5",
  "3",
  "4",
  "5",
  "6",
  "8",
  "10",
  "12",
  "16",
  "auto",
]);

const TAB_IDS = new Set(["today", "schedule", "tasks", "chat", "study"]);

const stripComments = (line, ext) => {
  if (ext === ".html") return line;
  const trimmed = line.trimStart();
  if (trimmed.startsWith("//") || trimmed.startsWith("*") || trimmed.startsWith("/*")) return "";
  return line.replace(/\{?\/\*.*?\*\/\}?/g, "").replace(/(^|\s)\/\/\s.*$/, "$1");
};

const isTsx = (ext) => ext === ".tsx";
const isCode = (ext) => ext === ".ts" || ext === ".tsx";
const classMatch = (line, source) => new RegExp(`(^|[\\s"'\`:!(])(${source})`).test(line);

export const RULES = [
  {
    id: "native-control",
    message:
      "Нативный элемент управления. Используй Button, IconButton, Input, Textarea, Select, Checkbox, RadioGroup, Switch.",
    test: (line, ext) => isTsx(ext) && /<(button|input|select|textarea|option)\b/.test(line),
  },
  {
    id: "raw-color",
    message: "Сырой цвет. Только семантические классы (bg-surface, text-fg-muted) или var(--color-…).",
    test: (line, ext) =>
      ext !== ".html" &&
      (/(^|[^&\w/])#[0-9a-fA-F]{6,8}\b/.test(line) ||
        /(^|[^&\w/])#(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{3,4}\b/.test(line.replace(/["']#[\w-]*["']/g, "")) ||
        /\b(rgba?|hsla?|oklch|oklab|lab|lch|color-mix)\(/.test(line) ||
        /-\[(#|rgb|hsl|oklch)/.test(line)),
  },
  {
    id: "arbitrary-visual",
    message:
      "Произвольное значение вместо токена: text-[15px], rounded-[10px], duration-[400ms], p-[7px]. Используй type-*, rounded-*, duration-*, ступени 4 px.",
    test: (line) =>
      classMatch(
        line,
        "(text|bg|border|ring|outline|shadow|rounded(-[a-z]+)?|font|leading|tracking|fill|stroke|decoration|duration|ease|delay|p[xytrbl]?|m[xytrbl]?|gap(-[xy])?|space-[xy])-[\\[(]",
      ),
  },
  {
    id: "off-grid-spacing",
    message: "Отступ не из ступеней сетки 4 px: 1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 12, 16.",
    test: (line) => {
      const re =
        /(^|[\s"'`:!(])-?(?:p[xytrbl]?|m[xytrbl]?|gap(?:-[xy])?|space-[xy])-(\d+(?:\.\d+)?|px|auto)(?![\w.\-[/])/g;
      for (const match of line.matchAll(re)) if (!SPACING_STEPS.has(match[2])) return true;
      return false;
    },
  },
  {
    id: "raw-font",
    message: "Шрифт задаётся токенами: font-sans, font-mono. Внешние шрифты запрещены — PWA работает офлайн.",
    test: (line) =>
      /font-family\s*:/.test(line) ||
      /font-\[['"]/.test(line) ||
      /@font-face/.test(line) ||
      /fonts\.(googleapis|gstatic)\.com/.test(line),
  },
  {
    id: "font-too-small",
    message: "Кегль меньше 13 px. Минимум — type-caption.",
    test: (line, ext) => {
      if (ext === ".html") return false;
      const px = /(?:text-\[|font-size\s*:\s*)(\d+(?:\.\d+)?)px/.exec(line);
      if (px && Number(px[1]) < 13) return true;
      const rem = /(?:text-\[|font-size\s*:\s*)(\d*\.?\d+)rem/.exec(line);
      return Boolean(rem && Number(rem[1]) * 16 < 13);
    },
  },
  {
    id: "touch-target",
    message:
      'Интерактивный элемент меньше 44 px без data-touch-expanded. Используй size="md" или расширь зону нажатия.',
    test: (line, ext) => {
      if (!isTsx(ext) || line.includes("data-touch-expanded")) return false;
      if (/<(Button|IconButton)\b[^>]*\bsize=["']sm["']/.test(line)) return true;
      const interactive = /<(a|Link)\b|onClick=|role=["']button["']/.test(line);
      return (
        interactive &&
        /(^|[\s"'`:!])(h|size|w|min-h|min-w)-(\d+(?:\.5)?)(?![\w.\-[/])/.test(line) &&
        hasSmallBox(line)
      );
    },
  },
  {
    id: "pseudo-icon",
    message: 'Символ или эмодзи вместо иконки. Используй <Icon name="…" />.',
    test: (line, ext) =>
      ext !== ".css" &&
      ext !== ".html" &&
      ([...PSEUDO_ICONS].some((char) => line.includes(char)) || /\p{Extended_Pictographic}/u.test(line)),
  },
  {
    id: "inline-svg",
    message: "<svg> в разметке экрана. Используй Icon или компонент дизайн-системы.",
    test: (line, ext) => isTsx(ext) && /<svg\b/.test(line),
  },
  {
    id: "inline-visual-style",
    message: "Цвет, фон, шрифт, тень, радиус или рамка в style={{…}}. Используй классы-токены.",
    test: (line, ext) =>
      isTsx(ext) &&
      /style=\{\{[^}]*\b(color|background\w*|font\w*|boxShadow|border\w*|fill|stroke|outline\w*)\s*:/.test(
        line,
      ),
  },
  {
    id: "browser-dialog",
    message: "Системные alert/confirm/prompt и атрибут title запрещены. Используй Dialog, toast, Tooltip.",
    test: (line, ext) =>
      (isCode(ext) && /(^|[^.\w])(window\.)?(alert|confirm|prompt)\(/.test(line)) ||
      (isTsx(ext) && /<[a-z][\w.-]*\b[^>]*\stitle=/.test(line)),
  },
  {
    id: "raw-scroll",
    message:
      "Прокрутка через overflow-auto/scroll. Вложенная область — <ScrollArea>; страница прокручивается документом с общим тонким бегунком.",
    test: (line, ext) =>
      ext === ".css"
        ? /overflow(-[xy])?\s*:\s*(auto|scroll)\b/.test(line)
        : ext !== ".html" && /(^|[\s"'`:!])overflow(-[xy])?-(auto|scroll)\b/.test(line),
  },
  {
    id: "raw-scrollbar",
    message:
      "Свой стиль скроллбара. Бегунок один — токены --scrollbar-* в packages/design/src/styles и <ScrollArea>; ::-webkit-scrollbar, scrollbar-width/color/gutter и no-scrollbar запрещены.",
    test: (line, ext) =>
      ext !== ".html" &&
      (/::-webkit-scrollbar/.test(line) ||
        /(^|[^\w-])scrollbar-(width|color|gutter|hide|none|thin|thumb|track)\b/.test(line) ||
        /(^|[^\w-])no-scrollbar\b/.test(line) ||
        /\[(&:*[^\]]*)?scrollbar/.test(line)),
  },
  {
    id: "vh-unit",
    message: "100vh в раскладке. Используй 100dvh (осознанно svh/lvh).",
    test: (line) => /(^|[^\w-])\d+(\.\d+)?vh\b/.test(line) || /\b(h|min-h|max-h)-screen\b/.test(line),
  },
  {
    id: "no-safe-area",
    message:
      "fixed/sticky у края без env(safe-area-inset-*). Добавь pt-safe, pb-safe-N, px-safe-N или env().",
    test: (line, ext) => {
      if (!isTsx(ext) && ext !== ".css") return false;
      const positioned = /(^|[\s"'`:!])(fixed|sticky)(?![\w-])|position\s*:\s*(fixed|sticky)/.test(line);
      const edge =
        /(^|[\s"'`:!])(inset(-[xy])?|top|bottom|left|right)-0(?![\w.])|(top|bottom|left|right)\s*:\s*0\b/.test(
          line,
        );
      return positioned && edge && !/safe|env\(/.test(line);
    },
  },
  {
    id: "viewport-lock",
    message: "user-scalable=no и maximum-scale запрещены: зум до 200 % обязателен.",
    test: (line) => /user-scalable\s*=\s*(no|0)/.test(line) || /maximum-scale/.test(line),
  },
  {
    id: "raw-keyframes",
    message:
      "@keyframes и animation: вне packages/design/src/styles. Используй токены движения (animate-*, duration-*).",
    test: (line, ext) =>
      ext !== ".html" &&
      (/@keyframes\b/.test(line) || (ext === ".css" && /(^|[\s;{])animation(-name)?\s*:/.test(line))),
  },
  {
    id: "unguarded-motion",
    message:
      "transition/animation в CSS или style мимо общего правила reduced-motion. Используй токены duration-* и ease-*.",
    test: (line, ext) =>
      (ext === ".css" && /(^|[\s;{])transition(-property|-duration)?\s*:/.test(line)) ||
      (isTsx(ext) && /style=\{\{[^}]*\b(transition|animation)\b/.test(line)),
  },
  {
    id: "semantic-color-override",
    message:
      "Переопределение --color-success/warning/danger/info. Пространство вуза меняет только --color-accent*.",
    test: (line) => /--color-(success|warning|danger|info)(-[a-z]+)?\s*:/.test(line),
  },
  {
    id: "accent-hue-out-of-window",
    message: "Тон акцента вуза вне окна 235°…345°.",
    test: (line) => {
      const match = /(?:\bhue|--accent-hue)\s*[:=]\s*(\d+(?:\.\d+)?)/.exec(line);
      return Boolean(match && (Number(match[1]) < 235 || Number(match[1]) > 345));
    },
  },
  {
    id: "dash-placeholder",
    message: "«—» как пустое значение. Скрой поле или покажи пустое состояние.",
    test: (line, ext) => isCode(ext) && (/(['"`])\s*[—–]\s*\1/.test(line) || />\s*[—–]\s*</.test(line)),
  },
  {
    id: "retry-button",
    message:
      "Кнопка или ссылка «Повторить», «Обновить», «Загрузить», «Попробовать снова». Ретраи идут в фоне, видны SyncIndicator и подпись свежести.",
    test: (line, ext) =>
      isCode(ext) &&
      (/(['"`>])\s*(Повторить( попытку)?|Обновить|Загрузить|Попробовать снова|Попробуйте снова|Перезагрузить)\s*[.!?]?\s*(['"`<])/.test(
        line,
      ) ||
        /\bon(Retry|Refresh|Reload)\b\s*=/.test(line)),
  },
  {
    id: "fabricated-progress",
    message: "Процент или ETA, которых не дал сервер. Используй ProgressRail с etaKnown={false}.",
    test: (line, ext) =>
      isTsx(ext) &&
      (/\}\s*%(\s|<|$)/.test(line) || /\bETA\b/.test(line) || /\bpercent(age)?\b/i.test(line)) &&
      !/etaKnown/.test(line),
  },
  {
    id: "mascot-data",
    message:
      "В renderer маскота ушли данные API, профиль, история чата или секреты. Передавай только объект состояния resolveMascot.",
    test: (line, ext) =>
      isCode(ext) &&
      /\b(MascotStage|Varya\.(mount|svg)|resolveMascot)\b/.test(line) &&
      /\b(password|token|secret|cookie|messages?|history|profile|response|session|credentials?)\b/i.test(
        line,
      ),
  },
  {
    id: "agent-block-html",
    message:
      "dangerouslySetInnerHTML, innerHTML или javascript: — блок агента рендерится только текстом (ADR-0004).",
    test: (line, ext) =>
      isCode(ext) && /dangerouslySetInnerHTML|\.innerHTML\s*=|insertAdjacentHTML|javascript:/.test(line),
  },
  {
    id: "icon-import",
    message: 'Импорт @tabler/icons-react вне ui/icon. Используй <Icon name="…" />.',
    test: (line, ext) => isCode(ext) && /from\s+["']@tabler\/icons-react["']/.test(line),
  },
  {
    id: "fill-as-text",
    message:
      "accent и danger — заливки. Цветной текст и иконка: text-accent-fg, text-danger-fg, border-danger-fg.",
    test: (line) =>
      classMatch(line, "(text|fill|stroke|decoration|caret)-(accent|danger)(-hover)?(?![\\w-]|/)") ||
      classMatch(line, "(text|fill|stroke|decoration|caret)-(accent|danger)(-hover)?/") ||
      classMatch(line, "(border|outline|ring)-danger(-hover)?(?![\\w-])"),
  },
  {
    id: "input-font-size",
    message: "Поле ввода меньше 16 px: iOS приблизит страницу при фокусе. Используй type-body.",
    test: (line, ext) => {
      if (isTsx(ext))
        return /<(Input|Textarea|Select|ChatComposer)\b[^>]*\bclassName=["'`{][^>]*\b(type-(ui|caption|label|subheading|mono)|text-\[)/.test(
          line,
        );
      if (ext === ".css")
        return (
          /(input|textarea|select)[^{]*\{[^}]*font-size\s*:\s*(\d+(?:\.\d+)?)px/.test(line) &&
          Number(/font-size\s*:\s*(\d+(?:\.\d+)?)px/.exec(line)?.[1]) < 16
        );
      return false;
    },
  },
  {
    id: "extra-tab",
    message:
      "Шестой пункт TabBar или вкладка вне §7.1 (today, schedule, tasks, chat, study). Новое — раздел в «Учёбе» через SECTION_REGISTRY.",
    file: (source) => {
      if (!/\bTabItem\b|<TabBar\b/.test(source)) return [];
      const problems = [];
      const lines = source.split("\n");
      let tabs = 0;
      // Пункт вкладки — плоский объект с id, label и icon.
      for (const match of source.matchAll(/\{[^{}]*\}/g)) {
        const body = match[0];
        const id = /\bid:\s*["']([\w-]+)["']/.exec(body);
        if (!id || !/\blabel:\s*["']/.test(body) || !/\bicon:\s*["']/.test(body)) continue;
        const line = source.slice(0, match.index).split("\n").length;
        if (lines[line - 1]?.includes("design-lint-ignore")) continue;
        tabs++;
        if (tabs > 5 || !TAB_IDS.has(id[1])) problems.push(line);
      }
      return [...new Set(problems)];
    },
  },
];

/** Есть ли на строке класс размера меньше 44 px (h/size/w/min-h/min-w ниже 11 шагов). */
function hasSmallBox(line) {
  for (const match of line.matchAll(/(?:^|[\s"'`:!])(?:h|size|w|min-h|min-w)-(\d+(?:\.5)?)(?![\w.\-[/])/g)) {
    if (Number(match[1]) < 11) return true;
  }
  return false;
}

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    if (name === "node_modules" || name === "dist") return [];
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

/** Проверяет текст файла и возвращает нарушения. Экспортируется для тестов. */
export function lintSource(relPath, source) {
  const ext = extname(relPath);
  if (!EXTENSIONS.has(ext) || EXEMPT.some((re) => re.test(relPath))) return [];
  const problems = [];
  const lines = source.split("\n");
  // Комментарий-исключение на отдельной строке действует на следующую строку кода.
  let skipNext = false;
  lines.forEach((raw, index) => {
    const ignore = /design-lint-ignore(?:\s*:)?\s*(\S.{2,})?/.exec(raw);
    if (ignore) {
      if (!ignore[1]) {
        problems.push({
          file: relPath,
          line: index + 1,
          rule: "ignore-without-reason",
          message: "design-lint-ignore без причины. Напиши после метки, почему исключение допустимо.",
        });
      }
      skipNext = /^\s*(\/\/|\/\*|\*|\{\/\*)/.test(raw);
      return;
    }
    if (skipNext) {
      if (raw.trim()) skipNext = false;
      return;
    }
    const line = stripComments(raw, ext);
    if (!line.trim()) return;
    for (const rule of RULES) {
      if (rule.test?.(line, ext)) {
        problems.push({ file: relPath, line: index + 1, rule: rule.id, message: rule.message });
      }
    }
  });
  if (ext === ".ts" || ext === ".tsx") {
    for (const rule of RULES) {
      for (const line of rule.file?.(source) ?? []) {
        problems.push({ file: relPath, line, rule: rule.id, message: rule.message });
      }
    }
  }
  return problems;
}

/** Список файлов репозитория, которые проверяет линтер. */
export function repoFiles() {
  const roots = SCAN_ROOTS.map((dir) => join(ROOT, dir)).filter((dir) => {
    try {
      return statSync(dir).isDirectory();
    } catch {
      return false;
    }
  });
  return [...roots.flatMap(walk), ...SCAN_FILES.map((file) => join(ROOT, file))].filter((file) => {
    try {
      return statSync(file).isFile();
    } catch {
      return false;
    }
  });
}

/** Прогон по файлам; без аргумента — по всему репозиторию. Возвращает нарушения. */
export function lintFiles(files = repoFiles()) {
  return files.flatMap((file) => {
    const rel = relative(ROOT, file).split(sep).join("/");
    try {
      return lintSource(rel, readFileSync(file, "utf8"));
    } catch {
      return [];
    }
  });
}

function main(argv) {
  const files = argv.length ? argv.map((file) => resolve(process.cwd(), file)) : undefined;
  const problems = lintFiles(files);
  for (const p of problems) console.error(`${p.file}:${p.line}  [${p.rule}]  ${p.message}`);
  if (problems.length) {
    console.error(`\ndesign-lint: нарушений — ${problems.length}. Правила: docs/design/styleguide.md §10`);
    process.exit(1);
  }
}

if (import.meta.filename && process.argv[1] && resolve(process.argv[1]) === import.meta.filename) {
  main(process.argv.slice(2));
}
