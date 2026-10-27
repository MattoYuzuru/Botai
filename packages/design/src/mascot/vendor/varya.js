// Ворона Варя — пиксельный маскот Doodle для ВШЭ. Один файл без зависимостей: спрайт 32×32 строится из
// прямоугольников SVG, реакции — CSS-анимации со steps(), чтобы движение было «спрайтовым».
//
// Подключение: <script src="varya.js"></script> → глобальный объект Varya.
//   Varya.mount(el, opts)  — живая ворона с реакциями внутри el (браузер);
//   Varya.svg(opts)        — статичная картинка SVG строкой (браузер, Node, сборка);
//   Varya.SITUATIONS       — ситуации: подпись, реакция, поза для превью и реплики;
//   Varya.GUIDE, Varya.PALETTE, Varya.THEMES.
(function (root) {
"use strict";

/* ---------- палитра: синие и серые ВШЭ, жёлтый клюв ---------- */
const PALETTE = { B:"#234B9B", BD:"#0F2D69", L:"#4E78CF", S:"#9CC3FF", W:"#FFFFFF", K:"#0A1838",
                  Y:"#F6BC32", YD:"#E8A51C", YL:"#FFE39A", G:"#C6C6C6", P:"#F59AB8", BELLY:"#3A64B8",
                  RED:"#E5484D", COFFEE:"#5B3A22" };
// day/orig отличаются только фоном страницы; pj — ночной колпак и звёзды
const THEMES = { day:{ pj:false }, orig:{ pj:false }, pj:{ pj:true } };
const VIEWBOX = "-4 -8 40 40"; // спрайт 32×32 плюс место сверху и слева для пузырей мысли и будильника

/* ---------- сборщик пиксельного спрайта ---------- */
const N = 32;
function layer() { const g = []; return {
  set(x, y, c) { if (x >= -8 && y >= -8 && x < N + 4 && y < N) g.push([x, y, c]); return this; },
  rect(x, y, w, h, c) { for (let j = 0; j < h; j++) for (let i = 0; i < w; i++) this.set(x + i, y + j, c); return this; },
  pts(a, c) { a.forEach(([x, y]) => this.set(x, y, c)); return this; },
  fill(fn, c) { for (let y = -8; y < N; y++) for (let x = -8; x < N + 4; x++) if (fn(x, y)) this.set(x, y, c); return this; },
  // контур: каждый пустой пиксель, касающийся фигуры (8 соседей), если skip() не говорит, что его закрывает другое
  outline(fn, c, skip = () => false) { for (let y = -9; y <= N; y++) for (let x = -9; x <= N + 4; x++) { if (fn(x, y) || skip(x, y)) continue;
      let hit = false; for (let j = -1; j <= 1 && !hit; j++) for (let i = -1; i <= 1; i++) if (fn(x + i, y + j)) { hit = true; break; }
      if (hit) this.set(x, y, c); } return this; },
  // соседние по строке пиксели одного цвета, нарисованные подряд, склеиваются в одну полосу: порядок наложения тот же
  svg() { const runs = [];
    for (const [x, y, c] of g) { const r = runs[runs.length - 1];
      if (r && r.y === y && r.c === c && r.x + r.w === x) r.w++; else runs.push({ x, y, c, w: 1 }); }
    return runs.map((r) => `<rect x="${r.x}" y="${r.y}" width="${r.w + .02}" height="1.02" fill="${r.c}"/>`).join(""); }
}; }
const G = (attrs, inner) => `<g ${attrs}>${inner}</g>`;
const R = (x, y, w, h) => (px, py) => px >= x && px < x + w && py >= y && py < y + h;
const OR = (...fs) => (x, y) => fs.some((f) => f(x, y));

/* ---------- формы ---------- */
// тело-яйцо, чуть выше, чем круглое
const inBody = (x, y) => { const px = x + .5, py = y + .5;
  if (py > 25) return false;
  const dx = (px - 17) / 8.5, dy = (py - 16) / 10; return dx * dx + dy * dy <= 1; };
const inBelly = (x, y) => { const dx = (x + .5 - 16) / 4.6, dy = (y + .5 - 20.5) / 3.6; return dx * dx + dy * dy <= 1; };
// небольшой острый клюв влево: верхняя и нижняя половины
const inBeakU = (x, y) => (y === 16 && x >= 9 && x <= 13) || (y === 17 && x >= 6 && x <= 13);
const inBeakL = (x, y) => y === 18 && x >= 8 && x <= 13;
const inWingR = OR(R(26, 16, 3, 5), R(29, 18, 1, 2));
const inWingL = OR(R(5, 18, 3, 4), R(4, 19, 1, 2));

/**
 * Разметка вороны внутри <svg>. Без pick — все кадры глаз и все предметы (живой режим, видимость задаёт CSS);
 * с pick = { eyes, props, night } — только выбранный кадр (статичная картинка).
 */
function drawCrow(theme, pick) {
const { B, BD, L, S, W, K, Y, YD, YL, G: GR, P } = PALETTE;
const live = !pick;
const has = (k) => live || pick.props.includes(k);
const night = live || !!pick.night, cap = live || !!pick.night || theme.pj;

const wingL = layer().fill(inWingL, BD).outline(inWingL, K, inBody);
const wingR = layer().fill(inWingR, BD).outline(inWingR, K, inBody);
// пара взъерошенных пёрышек на макушке
const tuft  = layer().pts([[16,4],[16,3],[17,2],[17,3],[18,3],[19,2],[15,4],[14,3]], K);
const body  = layer().fill(inBody, B)
  .fill((x, y) => inBody(x, y) && x >= 23, BD)
  .fill(inBelly, PALETTE.BELLY)
  .pts([[14,20],[17,22],[15,23]], L)
  .pts([[10,18],[10,19],[10,20]], L)
  .outline(inBody, K);
const blush = layer().rect(10,14,3,1,P).rect(18,14,3,1,P);
const beakU = layer().fill(inBeakU, Y).rect(10,16,3,1,YL).outline(inBeakU, K, (x, y) => inBeakL(x, y) || inBody(x, y) && x > 14);
const mouth = layer().rect(7,18,7,1,K);
const beakL = layer().fill(inBeakL, YD).outline(inBeakL, K, (x, y) => inBeakU(x, y) || y < 18 || inBody(x, y) && x > 14);
// тонкие ножки, чуть длинноваты, с кривыми коленками
const legs  = layer().pts([[14,26],[14,27],[13,28],[12,29],[13,29],[14,29]], K)
                     .pts([[19,26],[19,27],[20,28],[19,29],[20,29],[21,29]], K);
// кружка парит рядом сама по себе (только «Учёба ночью»)
const mug   = layer().rect(27,8,4,5,K).rect(28,9,2,3,B).set(28,10,W).set(29,11,W).rect(31,9,1,3,K).rect(28,8,2,1,PALETTE.COFFEE);
const steam = layer().pts([[28,6],[29,5],[28,4]], S).pts([[30,5],[29,3]], S);
// ночной колпак висит над головой: «Учёба ночью» и тёмный вид
const capL  = layer().rect(13,3,8,1,W).rect(14,2,7,1,S).rect(16,1,6,1,S).set(17,2,W).set(19,1,W).rect(19,0,4,1,S).set(22,1,S).rect(23,1,2,2,W);
const stars = theme.pj ? layer().pts([[1,2],[0,3],[1,3],[2,3],[1,4]], S).set(4,12,W).pts([[29,24],[28,25],[29,25],[30,25],[29,26]], S).set(2,27,W).set(26,29,S) : layer();

// кадры глаз: большие мультяшные глаза с бликом; блик смещается туда, куда она смотрит
const eye = (l, x, y, hx, hy) => l.rect(x,y,4,5,K).pts([[x,y],[x+3,y],[x,y+4],[x+3,y+4]], B)
  .rect(x+hx,y+hy,2,1,W).set(x+hx,y+hy+1,W).set(x+2,y+3,S);
const pair = (hx, hy) => eye(eye(layer(), 11, 9, hx, hy), 16, 9, hx, hy);
const eyes = {
  open:  () => pair(1,1),
  up:    () => pair(1,0),
  left:  () => pair(0,1),
  right: () => pair(2,1),
  wide:  () => layer().rect(11,9,4,5,W).pts([[11,9],[14,9],[11,13],[14,13]], B).set(12,11,K)
                      .rect(16,9,4,5,W).pts([[16,9],[19,9],[16,13],[19,13]], B).set(17,11,K)
                      .pts([[11,7],[12,6],[13,6],[14,7]], K).pts([[16,7],[17,6],[18,6],[19,7]], K),
  half:  () => layer().rect(11,11,4,3,K).pts([[11,13],[14,13]], B).set(12,12,W).rect(16,11,4,3,K).pts([[16,13],[19,13]], B).set(17,12,W),
  closed:() => layer().pts([[11,11],[12,12],[13,12],[14,11]], K).pts([[16,11],[17,12],[18,12],[19,11]], K),
  happy: () => layer().pts([[11,12],[12,11],[13,11],[14,12]], K).pts([[16,12],[17,11],[18,11],[19,12]], K),
  // злая: брови падают к клюву и режут глаза в щёлочки
  stern: () => layer().rect(11,11,4,3,K).pts([[14,11],[11,13],[14,13]], B).set(12,12,W)
                      .pts([[10,8],[11,8],[12,9],[13,9],[14,10]], K)
                      .rect(16,11,4,3,K).pts([[16,11],[16,13],[19,13]], B).set(18,12,W)
                      .pts([[21,8],[20,8],[19,9],[18,9],[17,10]], K)
};
// предметы над головой
const props = {
  excl:  () => layer().rect(4,1,2,5,Y).outline(R(4,1,2,5), K).rect(4,8,2,2,Y).outline(R(4,8,2,2), K),
  sweat: () => layer().pts([[24,6],[23,7],[24,7],[23,8],[24,8]], S),
  zzz:   () => layer().rect(2,2,4,1,S).set(4,3,S).set(3,4,S).rect(2,5,4,1,S).rect(6,7,3,1,S).set(7,8,S).rect(6,9,3,1,S),
  // пузыри мысли: точка, точка побольше, облачко с тремя точками
  bubbles: () => { const cloud = OR(R(-1,-7,5,1), R(-2,-6,7,2), R(-1,-4,5,1)), mid = R(3,0,2,2), tiny = R(7,5,1,1);
    const b1 = layer().fill(tiny, W).outline(tiny, K), b2 = layer().fill(mid, W).outline(mid, K),
          b3 = layer().fill(cloud, W).outline(cloud, K).pts([[-1,-5],[1,-5],[3,-5]], K);
    return { svg: () => G('class="b1"', b1.svg()) + G('class="b2"', b2.svg()) + G('class="b3"', b3.svg()) }; },
  anger: () => layer().pts([[24,0],[26,0],[23,1],[24,1],[26,1],[27,1],[23,3],[24,3],[26,3],[27,3],[24,4],[26,4]], PALETTE.RED),
  q:     () => layer().pts([[3,2],[4,2],[5,2],[6,3],[6,4],[5,5],[4,6],[4,8]], K),
  spark: () => layer().pts([[3,3],[2,4],[3,4],[4,4],[3,5]], Y).pts([[4,22],[3,23],[4,23],[5,23],[4,24]], S).pts([[27,22],[26,23],[27,23],[28,23],[27,24]], Y).pts([[25,1],[24,2],[25,2],[26,2],[25,3]], Y),
  // будильник: круглый циферблат, два жёлтых колокольчика, кнопка, ножки, стрелки
  clock: () => { const ox = -1, oy = -5, face = (x, y) => { const dx = x + .5 - (ox + 5), dy = y + .5 - (oy + 5.5); return dx * dx + dy * dy <= 3.6 * 3.6; };
    const bells = OR(R(ox,oy,3,1), R(ox-1,oy+1,3,1), R(ox+7,oy,3,1), R(ox+8,oy+1,3,1));
    return layer().fill(bells, Y).outline(bells, K, face).fill(face, W).outline(face, K)
      .set(ox+5,oy+2,S).set(ox+8,oy+5,S).set(ox+5,oy+8,S).set(ox+2,oy+5,S)
      .pts([[ox+5,oy+3],[ox+5,oy+4],[ox+5,oy+5],[ox+4,oy+5],[ox+3,oy+5]], K).set(ox+5,oy+5,PALETTE.RED)
      .rect(ox+4,oy,2,1,K).pts([[ox+2,oy+10],[ox+8,oy+10]], K)
      .pts([[ox-2,oy+4],[ox-3,oy+5],[ox+12,oy+4],[ox+13,oy+5]], S); }
};
// блестящее, что она тащит в гнездо при подключении учёбы: LMS, расписание, почта, кабинет
const items = live ? [
  { dx:11,  dy:16,  d:"0s",   l: layer().rect(1,1,3,3,Y).set(2,2,YL) },
  { dx:11,  dy:-9,  d:".2s",  l: layer().rect(1,26,3,3,W).rect(2,27,1,1,B) },
  { dx:-16, dy:-10, d:".4s",  l: layer().rect(28,27,3,3,S).set(29,28,W) },
  { dx:-12, dy:17,  d:".6s",  l: layer().rect(24,0,3,3,Y).set(25,1,W) }
] : [];

return G('class="stars"', stars.svg()) +
  G('filter="url(#RING)"',
    items.map((it) => G(`class="item" style="--dx:${it.dx}px;--dy:${it.dy}px;--d:${it.d}"`, it.l.svg())).join("") +
    (night ? G('class="mug"', '<g class="steam">' + steam.svg() + '</g>' + mug.svg()) : "") +
    G('class="whole"',
      legs.svg() +
      G('class="rig"',
        G('class="wingL"', wingL.svg()) + G('class="wingR"', wingR.svg()) +
        (live || !cap ? G('class="tuft"', tuft.svg()) : "") + body.svg() +
        Object.entries(eyes).filter(([k]) => live || k === pick.eyes).map(([k, f]) => G(`data-eye="${k}"`, f().svg())).join("") +
        blush.svg() + mouth.svg() + G('class="beakL"', beakL.svg()) + G('class="beakU"', beakU.svg()) +
        (cap ? G('class="capg"', capL.svg()) : "")
      )
    ) +
    Object.entries(props).filter(([k]) => has(k)).map(([k, f]) => G(`data-prop="${k}"`, f().svg())).join("")
  );
}
// белая обводка в один пиксель вокруг всего, как у стикеров с вороной ВШЭ
const defs = (id) => `<defs><filter id="${id}" x="-10%" y="-10%" width="120%" height="120%">` +
  `<feMorphology in="SourceAlpha" operator="dilate" radius="1" result="d"/><feFlood flood-color="#FFFFFF"/>` +
  `<feComposite in2="d" operator="in" result="w"/><feMerge><feMergeNode in="w"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>`;
let uid = 0;
const nextId = () => "varya-ring-" + (++uid);
const markup = (theme, pick, id) => defs(id) + drawCrow(theme, pick).replace(/#RING/g, "#" + id);

/* ---------- ситуации ---------- */
// anim — реакция (класс a-…); prop — предметы по тактам; beat2 — что показать во втором такте вместо prop целиком;
// eyes — глаза по тактам; rest — глаза после реакции; pose — кадр для статичного превью.
const SITUATIONS = [
 { id:"connect", label:"Подключить учёбу", icon:"+", anim:"collect", prop:[], eyes:["up","wide","happy"], rest:"open",
   pose:{ eyes:"happy", props:[] },
   lines:[
    "Скинь ссылку на LMS, логин и пароль. Я всё перетащу в гнездо: дедлайны, оценки, пары.",
    "Вороны тащат к себе всё блестящее. Я тащу дедлайны. Они тоже блестят, особенно ночью.",
    "Пароль я храню зашифрованным и никому не показываю. Даже другим воронам.",
    "Расписание подключено, LMS подключена. Осталось подключить тебя к учёбе. Это сложнее." ]},
 { id:"tip", label:"Совет", icon:"?", anim:"think", prop:["q","bubbles"], beat2:["bubbles"], eyes:["up","up","open"], rest:"open",
   pose:{ eyes:"up", props:["bubbles"] },
   lines:[
    "Накоп копится весь модуль. Экзамен только ставит точку.",
    "Сначала сделай самое неприятное задание. Остальные испугаются и сделаются сами.",
    "Объясни тему мне. Если поняла ворона, поймёт и семинарист.",
    "Закрой лишние вкладки. Я вижу, их 47, и в одной мемы.",
    "Сфоткай доску после семинара. Через неделю это будет единственный источник.",
    "Кофе не заменяет сон. Я проверяла, кружка у меня всегда с собой." ]},
 { id:"deadline", label:"Дедлайн", icon:"!", anim:"panic", prop:["excl","sweat"], eyes:["wide","wide","open"], rest:"open",
   pose:{ eyes:"wide", props:["excl","sweat"] },
   lines:[
    "Дедлайн через 2 часа. Я не паникую. Это перья сами шевелятся.",
    "23:47. Сдача в 23:59. Кар-р-р. Пальцы к бою.",
    "Моя тень уже убежала сдавать. Догоняй её.",
    "Это блокирующий элемент. Потом сдать не получится, только сейчас.",
    "Говорят, дедлайн лучший мотиватор. Сейчас проверим." ]},
 { id:"skips", label:"Пропуски", icon:"×", anim:"stern", prop:["anger"], eyes:["stern","stern","stern"], rest:"stern",
   pose:{ eyes:"stern", props:["anger"] },
   lines:[
    "Четыре пропуска. Ещё один, и я лично прилечу к тебе в общагу.",
    "Я не злюсь. Я просто записываю. Каждую пару. Каждую.",
    "На многих курсах посещаемость идёт в накоп. А ты идёшь мимо.",
    "Завтра первая пара в 9:30. Не придёшь, буду каркать под окном до самой сессии.",
    "Посещаемость 60%. Мне за тебя стыдно перед другими воронами." ]},
 { id:"night", label:"Учёба ночью", icon:"z", anim:"sleepy", prop:["zzz"], eyes:["half","closed","wide"], rest:"open",
   pose:{ eyes:"half", props:["zzz"], night:true },
   lines:[
    "Три часа ночи. Даже моя кружка уже спит.",
    "Вороны ночью не летают. И тебе не советуют.",
    "Ладно, ещё одна задача и спать. Я засекаю.",
    "Невыспавшийся мозг запоминает хуже. Это наука, а не я." ]},
 { id:"class", label:"Скоро пара", icon:"◷", anim:"alert", prop:["clock"], eyes:["left","right","open"], rest:"open",
   pose:{ eyes:"left", props:["clock"] },
   lines:[
    "Пара через 15 минут! Ноутбук, зарядка, пропуск: всё на месте?",
    "Очередь к лифту на Покре длиннее пары. Иди пешком.",
    "Аудиторию я написала в уведомлении. Не перепутай корпус, как в прошлый раз.",
    "Выйдешь сейчас, придёшь вовремя. Через 10 минут придёшь эффектно." ]},
 { id:"done", label:"Всё сдано", icon:"✓", anim:"happy", prop:["spark"], eyes:["happy","happy","happy"], rest:"happy",
   pose:{ eyes:"happy", props:["spark"] },
   lines:[
    "Всё сдано! Можно ничего не делать до понедельника.",
    "Кар! Кто сдал всё вовремя? Мы сдали!",
    "Ноль долгов. Мои перья гордятся тобой.",
    "Тень вернулась. Значит, точно всё сдано." ]}
];
const GUIDE = {
  collect:"смотрит вверх, к ней слетается всё блестящее с учебных сайтов, она щёлкает клювом и проглатывает",
  think:"наклоняет голову, смотрит вверх, над ней «?», потом по одному всплывают пузырьки мысли",
  panic:"трясётся, машет крыльями, щёлкает клювом, глаза в точку, а тень убегает без неё",
  stern:"злится: брови вниз, глаза-щёлочки, топает, трясёт головой, стучит крылом, над головой злая метка",
  sleepy:"в ночном колпаке и с кружкой кофе: клюёт носом, глаза слипаются, летят «z», потом резко просыпается",
  alert:"оглядывается по сторонам, над ней звенит будильник, подпрыгивает",
  happy:"прыгает, хлопает крыльями, глаза-улыбки, вокруг искры"
};
const byId = (id) => SITUATIONS.find((s) => s.id === id) || SITUATIONS[0];

/**
 * Статичная картинка SVG строкой. opts: { situation, theme = "orig", eyes, props, night, size }.
 * Без situation — Варя в покое; eyes/props/night перекрывают позу ситуации.
 */
function svg(opts = {}) {
  const s = opts.situation ? byId(opts.situation) : null;
  const pose = { eyes:"open", props:[], night:false, ...(s ? s.pose : {}), ...pick3(opts) };
  const id = opts.id || nextId();
  const size = opts.size ? ` width="${opts.size}" height="${opts.size}"` : "";
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${VIEWBOX}"${size} shape-rendering="crispEdges" role="img" aria-label="Ворона Варя${s ? ": " + s.label : ""}">` +
    markup(THEMES[opts.theme] || THEMES.orig, pose, id) + `</svg>`;
}
function pick3(o) { const r = {}; for (const k of ["eyes", "props", "night"]) if (o[k] !== undefined) r[k] = o[k]; return r; }

/* ---------- живая ворона: стили и проигрыватель ---------- */
const CSS = `
.varya g{transform-box:fill-box}
.varya .rig{transform-origin:50% 100%;animation:varya-idle 1.6s steps(1) infinite}
@keyframes varya-idle{0%{transform:translateY(0)}50%{transform:translateY(-1px)}}
.varya .wingL{transform-origin:100% 0%}.varya .wingR{transform-origin:0% 0%}
.varya .tuft{transform-origin:50% 100%;animation:varya-tuft 2.4s steps(1) infinite}
@keyframes varya-tuft{50%{transform:translateX(1px)}}
.varya .mug{animation:varya-float 6s steps(1) infinite;display:none}
.varya[data-sit="night"] .mug{display:inline}
.varya .capg{display:none;animation:varya-float 3s steps(1) infinite}
.varya.pj .capg,.varya[data-sit="night"] .capg{display:inline}
.varya.pj .tuft,.varya[data-sit="night"] .tuft{display:none}
@keyframes varya-float{0%,100%{transform:translate(0,0)}25%{transform:translate(0,-1px)}50%{transform:translate(1px,-2px)}75%{transform:translate(1px,-1px)}}
.varya .steam rect{animation:varya-zz 1.2s steps(3) infinite}
.varya .steam rect:nth-child(2n){animation-delay:-.6s}
.varya [data-eye]{display:none}
.varya[data-eyes="open"] [data-eye="open"],.varya[data-eyes="up"] [data-eye="up"],.varya[data-eyes="left"] [data-eye="left"],
.varya[data-eyes="right"] [data-eye="right"],.varya[data-eyes="closed"] [data-eye="closed"],.varya[data-eyes="half"] [data-eye="half"],
.varya[data-eyes="happy"] [data-eye="happy"],.varya[data-eyes="wide"] [data-eye="wide"],.varya[data-eyes="stern"] [data-eye="stern"]{display:inline}
.varya [data-prop]{display:none}
.varya.p-excl [data-prop="excl"],.varya.p-sweat [data-prop="sweat"],.varya.p-zzz [data-prop="zzz"],.varya.p-q [data-prop="q"],
.varya.p-bubbles [data-prop="bubbles"],.varya.p-anger [data-prop="anger"],.varya.p-spark [data-prop="spark"],.varya.p-clock [data-prop="clock"]{display:inline}
.varya .item{display:none}
.varya.a-collect .item{display:inline;animation:varya-fly 1.4s steps(7) both;animation-delay:var(--d)}
@keyframes varya-fly{0%{transform:translate(0,0);opacity:1}90%{opacity:1}100%{transform:translate(var(--dx),var(--dy));opacity:0}}
.varya.a-think .rig{animation:varya-think 1.4s steps(1) forwards}
@keyframes varya-think{0%{transform:rotate(0)}20%{transform:rotate(-6deg)}80%{transform:rotate(-6deg)}100%{transform:rotate(0)}}
.varya.a-panic .rig{animation:varya-panic .12s steps(1) 14}
@keyframes varya-panic{0%{transform:translateX(-1px)}50%{transform:translateX(1px)}}
.varya.a-panic .beakL,.varya.a-collect .beakL{animation:varya-chomp .3s steps(1) 6}
@keyframes varya-chomp{0%{transform:translateY(0)}50%{transform:translateY(1px)}}
.varya.a-panic .wingL{animation:varya-flapL .16s steps(1) 12}.varya.a-panic .wingR{animation:varya-flapR .16s steps(1) 12}
@keyframes varya-flapL{0%{transform:rotate(0)}50%{transform:rotate(35deg) translateY(-1px)}}
@keyframes varya-flapR{0%{transform:rotate(0)}50%{transform:rotate(-35deg) translateY(-1px)}}
.varya.a-stern .rig{animation:varya-nope .4s steps(1) 6}
.varya.a-stern .whole{animation:varya-stomp .3s steps(1) 5}
@keyframes varya-stomp{50%{transform:translateY(-1px)}}
@keyframes varya-nope{0%,100%{transform:translateX(0)}25%{transform:translateX(-1px)}75%{transform:translateX(1px)}}
.varya.a-stern .wingR{animation:varya-tap .3s steps(1) 6}
@keyframes varya-tap{0%{transform:rotate(0)}50%{transform:rotate(-25deg)}}
.varya.a-sleepy .rig{animation:varya-doze 3s steps(1) forwards}
@keyframes varya-doze{0%{transform:translateY(0) rotate(0)}30%{transform:translateY(1px) rotate(4deg)}60%{transform:translateY(1px) rotate(7deg)}75%{transform:translateY(-2px) rotate(0)}100%{transform:translateY(0)}}
.varya.a-happy .whole{animation:varya-hop .5s steps(1) 3}
@keyframes varya-hop{0%{transform:translateY(0) scale(1.05,.95)}25%{transform:translateY(-2px) scale(.96,1.04)}50%{transform:translateY(-3px)}75%{transform:translateY(-1px)}}
.varya.a-happy .wingL{animation:varya-flapL .25s steps(1) 6}.varya.a-happy .wingR{animation:varya-flapR .25s steps(1) 6}
.varya.a-alert .tuft{animation:varya-perk .3s steps(1) 4}
@keyframes varya-perk{0%{transform:translateY(0)}50%{transform:translateY(-1px)}}
.varya.a-alert .whole{animation:varya-hop .5s steps(1) 2}
.varya.a-collect .rig{animation:varya-gulp 1.6s steps(1) 1}
@keyframes varya-gulp{0%,100%{transform:scale(1,1)}70%{transform:scale(1.05,.96)}85%{transform:scale(.97,1.03)}}
.varya [data-prop="excl"],.varya [data-prop="q"]{animation:varya-bob .5s steps(1) infinite}
@keyframes varya-bob{50%{transform:translateY(-1px)}}
.varya [data-prop="bubbles"] .b2{animation:varya-appear .35s steps(1) both}
.varya [data-prop="bubbles"] .b3{animation:varya-appear .9s steps(1) both}
@keyframes varya-appear{0%{opacity:0}100%{opacity:1}}
.varya [data-prop="clock"]{transform-origin:50% 50%;animation:varya-ring .12s steps(1) infinite}
@keyframes varya-ring{0%{transform:rotate(-7deg)}50%{transform:rotate(7deg)}}
.varya [data-prop="anger"]{transform-origin:50% 50%;animation:varya-throb .3s steps(1) infinite}
@keyframes varya-throb{50%{transform:scale(1.3)}}
.varya [data-prop="zzz"] rect{animation:varya-zz 1.2s steps(3) infinite}
@keyframes varya-zz{0%{opacity:.2}100%{opacity:1}}
.varya [data-prop="spark"]{animation:varya-tw .4s steps(1) infinite}
.varya .stars{animation:varya-tw 1.6s steps(1) infinite}
@keyframes varya-tw{50%{opacity:.3}}
@media (prefers-reduced-motion: reduce){.varya *{animation:none!important}}`;
function injectCSS(doc) {
  if (doc.getElementById("varya-css")) return;
  const st = doc.createElement("style"); st.id = "varya-css"; st.textContent = CSS; doc.head.appendChild(st);
}

/**
 * Живая ворона внутри el. opts: { theme = "orig", situation = "connect", idle = true,
 *   onSay(label, text, situation) — реплика после нажатия, onAnim(anim | null) — начало и конец реакции }.
 * Возвращает { svg, react(), setSituation(id), setTheme(name), staticSVG(), destroy() }.
 */
function mount(el, opts = {}) {
  const doc = el.ownerDocument; injectCSS(doc);
  const svgEl = doc.createElementNS("http://www.w3.org/2000/svg", "svg");
  svgEl.setAttribute("viewBox", VIEWBOX); svgEl.setAttribute("shape-rendering", "crispEdges");
  svgEl.setAttribute("aria-hidden", "true"); svgEl.classList.add("varya");
  svgEl.style.overflow = "visible";
  el.appendChild(svgEl);
  const ringId = nextId();
  const reduce = !!(doc.defaultView && doc.defaultView.matchMedia && doc.defaultView.matchMedia("(prefers-reduced-motion: reduce)").matches);
  let cur = byId(opts.situation), themeName = "orig", idx = {}, timers = [], busy = false, stopped = false, idleT = null;
  const later = (ms, f) => timers.push(setTimeout(f, ms));
  const clear = () => { timers.forEach(clearTimeout); timers = []; };
  const setProps = (list) => { [...svgEl.classList].filter((c) => c.startsWith("p-")).forEach((c) => svgEl.classList.remove(c)); list.forEach((p) => svgEl.classList.add("p-" + p)); };
  const setAnim = (a) => { [...svgEl.classList].filter((c) => c.startsWith("a-")).forEach((c) => svgEl.classList.remove(c)); void svgEl.getBoundingClientRect();
    if (a) svgEl.classList.add("a-" + a); if (opts.onAnim) opts.onAnim(a || null); };

  function setTheme(name) {
    themeName = THEMES[name] ? name : "orig";
    svgEl.innerHTML = markup(THEMES[themeName], null, ringId);
    svgEl.classList.toggle("pj", themeName === "pj");
  }
  function setSituation(id) { cur = byId(id); svgEl.dataset.sit = cur.id; svgEl.dataset.eyes = cur.rest; setProps(cur.id === "done" ? ["spark"] : []); }
  function react() {
    clear(); busy = true;
    const s = cur; idx[s.id] = ((idx[s.id] ?? -1) + 1) % s.lines.length;
    if (opts.onSay) opts.onSay(s.label, s.lines[idx[s.id]], s);
    setAnim(s.anim);
    svgEl.dataset.eyes = s.eyes[0]; setProps(s.prop.slice(0, 1));
    later(600, () => { svgEl.dataset.eyes = s.eyes[1]; if (s.prop[1]) setProps(s.beat2 || s.prop); });
    if (s.id === "night") later(1900, () => { svgEl.dataset.eyes = "wide"; setProps([]); });
    later(2200, () => { svgEl.dataset.eyes = s.eyes[2]; });
    later(3200, () => { setAnim(null); setProps(s.id === "done" ? ["spark"] : []); svgEl.dataset.eyes = s.rest; busy = false; });
  }
  // в покое: моргает и поглядывает по сторонам
  function idle() {
    idleT = setTimeout(() => {
      if (stopped) return;
      if (!busy && !reduce && cur.rest !== "happy") {
        const base = cur.rest, r = Math.random();
        const look = r < .5 ? "closed" : r < .7 ? "left" : r < .85 ? "up" : "right";
        svgEl.dataset.eyes = look;
        setTimeout(() => { if (!busy) svgEl.dataset.eyes = base; }, look === "closed" ? 140 : 700);
      }
      idle();
    }, 1800 + Math.random() * 2200);
  }
  setTheme(opts.theme); setSituation(cur.id);
  if (opts.idle !== false) idle();
  return {
    svg: svgEl, react, setSituation, setTheme,
    get situation() { return cur.id; }, get theme() { return themeName; },
    staticSVG: () => svg({ situation: cur.id, theme: themeName }),
    destroy() { stopped = true; clear(); clearTimeout(idleT); svgEl.remove(); }
  };
}

root.Varya = { PALETTE, THEMES, SITUATIONS, GUIDE, VIEWBOX, CSS, svg, mount };
})(typeof window !== "undefined" ? window : globalThis);
