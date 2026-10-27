import { createHash } from "node:crypto";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { build, type Plugin } from "vite";

// `import.meta.dirname` — каталог этого файла (Node 20.11+), без percent-кодирования пути, как у `URL.pathname`.
const SW_ENTRY = join(import.meta.dirname ?? process.cwd(), "src/features/pwa/sw/sw.ts");

function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? walk(join(dir, entry.name)) : [join(dir, entry.name)],
  );
}

/**
 * Адреса оболочки: всё, что лежит в `dist` после сборки — страница, ассеты, манифест и иконки из `public/`.
 * Самого `sw.js` и карт исходников в списке нет.
 */
export function precacheList(files: readonly string[]): string[] {
  const urls = new Set<string>();
  for (const file of files) {
    if (file === "sw.js" || file.endsWith(".map")) continue;
    urls.add(`/${file}`);
  }
  return [...urls].sort();
}

/**
 * Идентификатор сборки: от адресов и от содержимого файлов. У ассетов хеш уже в имени, а у `index.html`,
 * манифеста и иконок имя постоянное — без содержимого правка только страницы или иконки не меняла бы `sw.js`,
 * и браузеры не увидели бы обновления.
 */
export function buildId(
  urls: readonly string[],
  content: (url: string) => string | Uint8Array = () => "",
): string {
  const hash = createHash("sha1");
  for (const url of urls) {
    hash.update(url);
    hash.update("\0");
    hash.update(content(url));
    hash.update("\n");
  }
  return hash.digest("hex").slice(0, 12);
}

/**
 * Собирает service worker после основной сборки: отдельная сборка `sw.ts` в `dist/sw.js` (IIFE, без импортов
 * приложения) с подставленными списком оболочки и идентификатором сборки. Новых зависимостей нет — это тот же Vite.
 * В dev-сервере плагин не работает: service worker в разработке — MSW.
 */
export function botaiServiceWorker(targets: string[]): Plugin {
  let outDir = "dist";
  let root = process.cwd();
  return {
    name: "botai-service-worker",
    apply: "build",
    configResolved(config) {
      outDir = config.build.outDir;
      root = config.root;
    },
    async closeBundle() {
      const dist = resolve(root, outDir);
      // Основная сборка могла упасть до записи на диск: настоящую ошибку не прячем ошибкой чтения каталога.
      if (!existsSync(dist)) return;
      // Страницу `index.html` Vite кладёт на диск в самом конце, поэтому список берётся с диска, а не из бандла.
      const urls = precacheList(walk(dist).map((file) => relative(dist, file)));
      if (!urls.includes("/index.html")) return;
      await build({
        configFile: false,
        root,
        logLevel: "warn",
        publicDir: false,
        define: {
          __PRECACHE__: JSON.stringify(urls),
          __BUILD__: JSON.stringify(buildId(urls, (url) => readFileSync(join(dist, url)))),
        },
        build: {
          outDir,
          emptyOutDir: false,
          minify: true,
          target: targets,
          lib: { entry: SW_ENTRY, formats: ["iife"], name: "botaiSw", fileName: () => "sw.js" },
        },
      });
    },
  };
}
