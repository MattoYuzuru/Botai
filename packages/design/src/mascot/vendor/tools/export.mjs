// Статичные картинки Вари из varya.js → mascot/sprites/*.svg (для README, презентации, иконок и приложения).
// Запуск из корня репозитория: node packages/design/src/mascot/vendor/tools/export.mjs
// BOTAI: добавлены сезонные образы (varya-skin-<id>.svg); ситуации Botai попадают в список из SITUATIONS сами.
// После правок спрайта в varya.js запусти снова и закоммить обновлённые SVG вместе с кодом.
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const mascot = join(here, "..");
const out = join(mascot, "sprites");

const sandbox = {};
vm.runInNewContext(readFileSync(join(mascot, "varya.js"), "utf8"), sandbox, { filename: "varya.js" });
const { Varya } = sandbox;

mkdirSync(out, { recursive: true });
const files = [
  ["varya.svg", Varya.svg({ id: "varya-ring" })],
  ["varya-pj.svg", Varya.svg({ theme: "pj", id: "varya-ring" })],
  ...Varya.SITUATIONS.map((s) => [`varya-${s.id}.svg`, Varya.svg({ situation: s.id, id: "varya-ring" })]),
  ...Varya.SKINS.map((k) => [`varya-skin-${k}.svg`, Varya.svg({ skin: k, id: "varya-ring" })])
];
for (const [name, body] of files) writeFileSync(join(out, name), body + "\n");
console.log(`Записано ${files.length} файлов в ${out}`);
