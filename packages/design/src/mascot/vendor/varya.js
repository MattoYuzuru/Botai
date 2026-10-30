// Векторная ворона Варя v3 — персонаж BotAI. SVG 128×128 и CSS-анимация отдельных частей.
// BOTAI: первоначальный контроллер происходит из doodle_vibe/mascot (d84008e), рисунок и движение
// полностью переработаны для BotAI. Происхождение: docs/design/vendor.md; правила: docs/design/mascot.md.
// Публичный API сохраняется для React-обёртки, статичных экспортов и генератора иконок.
(function (root) {
"use strict";

/* ---------- палитра: синие и серые ВШЭ, жёлтый клюв ---------- */
// BOTAI: векторная Варя v3. Палитра персонажа — токены рисунка, не цвета интерфейса.
// Один рисунок для сцены, насеста, экспортов и иконок. Никаких внешних ресурсов и фильтров.
const PALETTE = {
  INK:'#142C53', EDGE:'var(--mascot-edge,#142C53)', EDGE_DARK:'#8CA9D9', BODY:'#315DB6', LIGHT:'#6088DA', DARK:'#244783', BELLY:'#83AAEF',
  PAPER:'#FFF9E9', WHITE:'#FFFFFF', GOLD:'#F4BE60', GOLD_DARK:'#D68A36',
  RED:'#D86060', GREEN:'#73A98D', NIGHT:'#A8C2EF', BACKGROUND:'#E4EDFF'
};
const THEMES = { day:{ pj:false }, orig:{ pj:false }, pj:{ pj:true } };
const VIEWBOX = '0 0 128 128';
const G = (attrs, inner) => `<g ${attrs}>${inner}</g>`;
const path = (d, fill, extra='') => `<path d="${d}" fill="${fill}" ${extra}/>`;
const ellipse = (cx, cy, rx, ry, fill, extra='') => `<ellipse cx="${cx}" cy="${cy}" rx="${rx}" ry="${ry}" fill="${fill}" ${extra}/>`;
const stroke = (d, color=PALETTE.INK, width=2.5) => path(d, 'none', `stroke="${color}" stroke-width="${width}" stroke-linecap="round" stroke-linejoin="round"`);
const star = (x,y,r=5,color=PALETTE.GOLD) => path(`M${x} ${y-r}Q${x+1} ${y-1} ${x+r} ${y}Q${x+1} ${y+1} ${x} ${y+r}Q${x-1} ${y+1} ${x-r} ${y}Q${x-1} ${y-1} ${x} ${y-r}Z`,color);

function drawEyes(name) {
  const {PAPER, INK, WHITE} = PALETTE;
  if(name==='closed'||name==='happy') {
    return stroke(name==='happy'?'M39 53Q46 44 53 53M64 52Q71 43 78 52':'M39 54Q46 59 53 54M64 53Q71 58 78 53', PAPER, 3.5);
  }
  const wide=name==='wide', half=name==='half', stern=name==='stern';
  const dx=name==='left'?-3:name==='right'?3:0, dy=name==='up'?-4:half?3:1;
  return [46,72].map((x,i)=> {
    const y=i?50:52, rx=wide?12.5:11.5, ry=wide?16:14;
    return ellipse(x,y,rx,ry,PAPER) +
      ellipse(x-2+dx,y+dy,wide?4:5,wide?5:7,INK)+
      ellipse(x-3+dx,y-2+dy,1.5,2,WHITE)+
      (half?path(`M${x-13} ${y-15}H${x+13}V${y}Q${x} ${y+3} ${x-13} ${y}Z`,PALETTE.BODY):'')+
      (stern?stroke(`M${x-12} ${y-11}L${x+12} ${y-4}`,INK,4):'');
  }).join('');
}

function drawHead(theme, pick) {
  const {INK,BODY,LIGHT,PAPER,GOLD,GOLD_DARK} = PALETTE;
  const live=!pick, hasSkin=(name)=>live||pick.skin===name;
  const eyes=live?['open','up','left','right','closed','half','happy','wide','stern'].map(k=>G(`data-eye="${k}"`,drawEyes(k))).join(''):drawEyes(pick.eyes||'open');
  const tuft=G('class="tuft"',path('M49 30Q42 19 46 16Q50 14 58 23Q59 8 65 9Q70 9 71 23Q82 15 85 19Q88 23 79 32Z',PALETTE.EDGE));
  const face=path('M32 60C28 40 39 26 57 25C77 20 97 31 101 49C105 63 96 78 80 80L49 81C37 79 32 71 32 60Z',BODY)+
    path('M36 44C40 30 56 26 70 29C54 30 42 38 39 51Z',LIGHT,'opacity=".65"')+
    eyes+
    G('class="detail"',stroke('M34 69L39 71M82 66L88 64',LIGHT,3))+
    G('class="beakL"',path('M26 74Q36 85 52 75L48 69Z',GOLD_DARK))+
    G('class="beakU"',path('M51 64Q35 62 22 72Q32 78 52 73Q56 69 51 64Z',GOLD)+stroke('M27 71Q36 67 43 67',PAPER,1.6));
  const cap=path('M43 29Q47 8 67 11Q78 11 90 19Q82 15 82 27Z',PALETTE.NIGHT)+stroke('M44 30Q62 23 82 28',PAPER,7)+ellipse(90,20,5,5,PAPER);
  const santa=path('M43 29Q49 4 70 9Q86 11 91 26Q79 20 77 26Z',PALETTE.RED)+stroke('M43 30Q63 22 81 29',PAPER,7)+ellipse(92,27,5,5,PAPER);
  return G('class="head"',
    (live||(!theme.pj&&!pick.night&&pick.skin!=='new-year')?tuft:'')+face+
    (live?G('class="capg"',cap):theme.pj||pick.night?G('class="capg"',cap):'')+
    (hasSkin('new-year')?G('class="santa"',santa):''));
}

function drawCrow(theme, pick, id) {
  const {INK,BODY,LIGHT,DARK,BELLY,PAPER,GOLD,GOLD_DARK,WHITE,GREEN,RED,NIGHT}=PALETTE;
  const live=!pick, has=(k)=>live||(pick.props||[]).includes(k), skin=(k)=>live||pick.skin===k;
  const legs={
    a:stroke('M52 107L51 119M45 120L57 120M73 108L75 119M69 120L81 120',PALETTE.EDGE,3.5),
    b:stroke('M52 107L45 115L38 115M73 108L80 120L87 120',PALETTE.EDGE,3.5),
    c:stroke('M52 107L45 120L38 120M73 108L80 115L87 115',PALETTE.EDGE,3.5)
  };
  const feet=live?G('class="legsA"',legs.a)+G('class="legsB"',legs.b)+G('class="legsC"',legs.c):legs[pick.legs||'a'];
  const body=path('M40 64C26 77 25 99 40 108C53 117 81 115 94 103C106 92 104 71 91 62Z',`url(#${id}-body)`)+
    G('class="detail"',path('M40 82Q46 74 61 79Q77 81 83 96Q69 110 50 105Q38 100 40 82Z',BELLY,'opacity=".46"')+stroke('M44 94Q49 98 53 98',BELLY,2));
  const wingL=G('class="wingL"',path('M33 67Q19 69 20 86Q21 93 30 92L37 78Z',DARK));
  const wingR=G('class="wingR"',path('M88 67C105 69 114 83 108 96C105 103 96 106 89 99Q82 92 88 67Z',DARK)+
    G('class="detail"',stroke('M95 78Q105 86 101 94',LIGHT,2)+star(97,87,4,GOLD)));
  const tail=G('class="tail"',path('M89 90Q109 92 116 103Q101 109 87 103Z',INK));
  const mug=G('class="mug"',stroke('M110 78H115Q121 78 120 85Q119 90 113 89',PAPER,3)+path('M99 75H114V91Q107 96 100 91Z',PAPER)+
    ellipse(106.5,75,7.5,2.5,GOLD_DARK)+star(106,84,3,BODY)+G('class="steam"',stroke('M103 67Q99 63 103 59M111 67Q107 63 111 59',NIGHT,2)));
  const leaf=G('class="leaf"',path('M10 56Q27 45 31 64Q15 71 10 56Z',GREEN)+stroke('M14 57L35 73',DARK,1.5));
  const props={
    excl:()=>ellipse(16,32,11,14,GOLD)+stroke('M16 24V33',INK,3)+ellipse(16,39,1.6,1.6,INK),
    q:()=>ellipse(15,30,12,14,PAPER)+stroke('M11 25C11 20 21 20 20 26Q20 29 15 31V33',INK,2.5)+ellipse(15,38,1.4,1.4,INK),
    bubbles:()=>G('class="b2"',ellipse(25,48,2,2,NIGHT))+G('class="b3"',ellipse(20,42,3,3,NIGHT)),
    sweat:()=>path('M107 41Q118 54 107 54Q97 54 107 41Z',NIGHT),
    zzz:()=>stroke('M7 25H18L7 36H18M20 12H28L20 20H28',NIGHT,2.5),
    anger:()=>stroke('M14 23V28H9M20 23V28H25M14 40V35H9M20 40V35H25',RED,2.5),
    spark:()=>star(15,41,6)+star(109,28,5)+star(109,100,4),
    clock:()=>ellipse(16,31,12,12,PAPER,`stroke="${INK}" stroke-width="2"`)+stroke('M16 23V32L22 35M5 19L9 16M23 16L27 19',INK,2.5),
    key:()=>ellipse(13,27,6,6,GOLD)+ellipse(13,27,2.5,2.5,INK)+stroke('M19 27H32V32M27 27V31',GOLD,3.5),
    cloudx:()=>path('M6 36C0 31 4 23 10 24C12 14 27 16 27 25C36 22 39 36 29 36Z',NIGHT)+stroke('M16 25L23 32M23 25L16 32',INK,2)
  };
  const objects=Object.entries(props).filter(([k])=>has(k)).map(([k,draw])=>G(`data-prop="${k}"`,draw())).join('');
  const defs=`<defs><linearGradient id="${id}-body" x1=".15" y1="0" x2=".9" y2="1" gradientUnits="objectBoundingBox"><stop stop-color="${LIGHT}"/><stop offset=".55" stop-color="${BODY}"/><stop offset="1" stop-color="${DARK}"/></linearGradient></defs>`;
  return defs+G('class="shadow detail"',ellipse(65,121,30,3,INK,'opacity=".12"'))+
    G('class="whole"',G('class="anatomy"',feet+G('class="rig"',tail+wingL+body+drawHead(theme,pick)+wingR+
      (live||pick.night||skin('mug')?mug:'')+(skin('leaf')?leaf:'')+
      (skin('new-year')?G('class="glint"',star(14,56,5)):''))))+objects;
}
let uid=0;
const nextId=()=>`varya-vector-${++uid}`;
const markup=(theme,pick,id)=>drawCrow(theme,pick,id);

/* ---------- ситуации ---------- */
// anim — реакция (класс a-…); prop — предметы по тактам; beat2 — что показать во втором такте вместо prop целиком;
// eyes — глаза по тактам; rest — глаза после реакции; pose — кадр для статичного превью.
// BOTAI: hold — «держащий» вид живой Вари, пока ситуация не сменилась: { eyes, props, loop }, где loop — петля l-run | l-calm | l-tilt;
// ms — длительность реакции react() (по умолчанию 3200); pose.legs — кадр ног "b"/"c" для статичной картинки бега.
const SITUATIONS = [
 { id:"connect", label:"Подключить учёбу", icon:"+", anim:"collect", prop:[], eyes:["up","wide","happy"], rest:"open",
   hold:{ eyes:"up", loop:"run" }, pose:{ eyes:"happy", props:[] },
   lines:[]},
 { id:"tip", label:"Совет", icon:"?", anim:"think", prop:["q","bubbles"], beat2:["bubbles"], eyes:["up","up","open"], rest:"open",
   hold:{ eyes:"up", props:["bubbles"], loop:"calm" }, pose:{ eyes:"up", props:["bubbles"] },
   lines:[]},
 { id:"deadline", label:"Дедлайн", icon:"!", anim:"panic", prop:["excl","sweat"], eyes:["wide","wide","open"], rest:"open",
   hold:{ eyes:"wide", props:["excl"], loop:"calm" }, pose:{ eyes:"wide", props:["excl","sweat"] },
   lines:[]},
 { id:"skips", label:"Пропуски", icon:"×", anim:"stern", prop:["anger"], eyes:["stern","stern","stern"], rest:"stern",
   pose:{ eyes:"stern", props:["anger"] },
   lines:[]},
 { id:"night", label:"Учёба ночью", icon:"z", anim:"sleepy", prop:["zzz"], eyes:["half","closed","wide"], rest:"open",
   hold:{ eyes:"half", props:["zzz"], loop:"calm" }, pose:{ eyes:"half", props:["zzz"], night:true },
   lines:[]},
 { id:"class", label:"Скоро пара", icon:"◷", anim:"alert", prop:["clock"], eyes:["left","right","open"], rest:"open",
   hold:{ eyes:"left", props:["clock"], loop:"calm" }, pose:{ eyes:"left", props:["clock"] },
   lines:[]},
 { id:"done", label:"Всё сдано", icon:"✓", anim:"happy", ms:1500, prop:["spark"], eyes:["happy","happy","happy"], rest:"happy",
   hold:{ eyes:"happy", props:["spark"] }, pose:{ eyes:"happy", props:["spark"] },
   lines:[]},
 // BOTAI: ситуации продукта (docs/design/mascot.md §2). Реплики берёт реестр Botai (mascot/lines.ts) из настоящих данных,
 // поэтому lines пусты и react() ничего не говорит.
 { id:"sync", label:"Обновляю", icon:"~", anim:"nod", ms:1200, prop:[], eyes:["open","open","open"], rest:"open",
   hold:{ eyes:"open", loop:"run" }, pose:{ eyes:"open", props:[], legs:"b" }, lines:[] },
 { id:"think", label:"Думаю", icon:"?", anim:"think", ms:1600, prop:["q","bubbles"], beat2:["bubbles"], eyes:["up","up","up"], rest:"up",
   hold:{ eyes:"up", props:["q","bubbles"], loop:"tilt" }, pose:{ eyes:"up", props:["q","bubbles"] }, lines:[] },
 { id:"overdue", label:"Срок прошёл", icon:"!", anim:"nod", ms:1200, prop:[], eyes:["open","open","open"], rest:"open",
   hold:{ eyes:"open", props:["excl"] }, pose:{ eyes:"open", props:["excl"] }, lines:[] },
 { id:"offline", label:"Нет сети", icon:"x", anim:"nod", ms:1200, prop:[], eyes:["half","half","half"], rest:"half",
   hold:{ eyes:"half", props:["cloudx"] }, pose:{ eyes:"half", props:["cloudx"] }, lines:[] },
 { id:"reauth", label:"Нужен вход", icon:"k", anim:"nod", ms:1200, prop:[], eyes:["open","open","open"], rest:"open",
   hold:{ eyes:"open", props:["key"], loop:"calm" }, pose:{ eyes:"open", props:["key"] }, lines:[] },
 { id:"error", label:"Не получилось", icon:"x", anim:"nod", ms:1200, prop:[], eyes:["half","half","half"], rest:"half",
   hold:{ eyes:"half", props:["sweat"] }, pose:{ eyes:"half", props:["sweat"] }, lines:[] },
 { id:"idle", label:"В покое", icon:"-", anim:"nod", ms:1000, prop:[], eyes:["open","open","open"], rest:"open",
   hold:{ eyes:"open", loop:"calm" }, pose:{ eyes:"open", props:[] }, lines:[] },
 { id:"perch", label:"На жёрдочке", icon:"-", anim:"nod", ms:1000, prop:[], eyes:["open","open","open"], rest:"open",
   hold:{ eyes:"open", loop:"calm" }, pose:{ eyes:"open", props:[] }, lines:[] },
 { id:"wave", label:"Машет крылом", icon:"w", anim:"wave", ms:1900, prop:[], eyes:["happy","happy","happy"], rest:"open",
   hold:{ eyes:"open", loop:"calm" }, pose:{ eyes:"happy", props:[] }, lines:[] }
];
// BOTAI: одноразовые действия сцены пустого чата (mount().act): анимация, глаза по времени, длительность.
const ACTS = {
  peck:  { anim:"peck",  ms:1900, seq:[[0,"half"],[1500,"open"]] },
  shake: { anim:"shake", ms:1300, seq:[[0,"closed"],[1100,"open"]] },
  look:  { anim:"look",  ms:2200, seq:[[0,"up"],[700,"left"],[1400,"right"],[2000,"open"]] },
  wave:  { anim:"wave",  ms:1900, seq:[[0,"happy"],[1700,"open"]] },
  nod:   { anim:"nod",   ms:900,  seq:[[0,"open"]] },
  hop:   { anim:"hop",   ms:300,  seq:[[0,"happy"],[260,"open"]] }
};
const SKINS = ["new-year", "mug", "leaf"]; // BOTAI: сезонные образы
const GUIDE = {
  collect:"смотрит вверх, к ней слетается всё блестящее с учебных сайтов, она щёлкает клювом и проглатывает",
  think:"наклоняет голову, смотрит вверх, над ней «?», потом по одному всплывают пузырьки мысли",
  panic:"трясётся, машет крыльями, щёлкает клювом, глаза в точку, а тень убегает без неё",
  stern:"злится: брови вниз, глаза-щёлочки, топает, трясёт головой, стучит крылом, над головой злая метка",
  sleepy:"в ночном колпаке и с кружкой кофе: клюёт носом, глаза слипаются, летят «z», потом резко просыпается",
  alert:"оглядывается по сторонам, над ней звенит будильник, подпрыгивает",
  happy:"прыгает, хлопает крыльями, глаза-улыбки, вокруг искры",
  nod:"один кивок головой (BOTAI)",
  wave:"поднимает и качает крыло, глаза-улыбки (BOTAI)",
  peck:"клюёт пол, наклоняясь вперёд (BOTAI)",
  shake:"встряхивается всем телом (BOTAI)",
  look:"смотрит вверх, влево и вправо (BOTAI)",
  hop:"короткий прыжок (BOTAI)"
};
const byId = (id) => SITUATIONS.find((s) => s.id === id) || SITUATIONS[0];

/**
 * Статичная картинка SVG строкой. opts: { situation, theme = "orig", eyes, props, night, size }.
 * BOTAI: ещё skin ("new-year" | "mug" | "leaf") и legs ("b" | "c" — кадр бега).
 * Без situation — Варя в покое; eyes/props/night перекрывают позу ситуации.
 */
function svg(opts = {}) {
  const s = opts.situation ? byId(opts.situation) : null;
  const pose = { eyes:"open", props:[], night:false, skin:null, legs:"a", ...(s ? s.pose : {}), ...pick3(opts) };
  const id = opts.id || nextId();
  const size = opts.size ? ` width="${opts.size}" height="${opts.size}"` : "";
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${VIEWBOX}"${size} shape-rendering="geometricPrecision" role="img" aria-label="Ворона Варя${s ? ": " + s.label : ""}">` +
    markup(THEMES[opts.theme] || THEMES.orig, pose, id) + `</svg>`;
}
function pick3(o) { const r = {}; for (const k of ["eyes", "props", "night", "skin", "legs"]) if (o[k] !== undefined) r[k] = o[k]; return r; }

/* ---------- живая ворона: стили и проигрыватель ---------- */
const CSS = `
/* BOTAI: токены движения персонажа. Контейнер и ввод никогда не анимируются вместе с рисунком. */
.varya{--mascot-edge:light-dark(${PALETTE.INK},${PALETTE.EDGE_DARK});--varya-breathe:4s;--varya-run:.56s;--varya-gesture:.9s;--varya-ease:cubic-bezier(.65,0,.35,1);isolation:isolate}
.varya .rig,.varya .whole{transform-origin:64px 112px}
.varya .head{transform-origin:65px 75px}
.varya .wingL{transform-origin:32px 70px}.varya .wingR{transform-origin:91px 71px}
.varya .legsB,.varya .legsC,.varya .mug,.varya .capg,.varya .santa,.varya .glint,.varya .leaf,.varya [data-eye],.varya [data-prop]{display:none}
.varya[data-sit="night"] .mug,.varya[data-sit="night"] .capg,.varya.pj .capg,.varya.skin-mug .mug,.varya.skin-new-year .santa,.varya.skin-new-year .glint,.varya.skin-leaf .leaf{display:inline}
.varya.pj .tuft,.varya[data-sit="night"] .tuft,.varya.skin-new-year .tuft,.varya.skin-new-year .capg{display:none}
.varya[data-eyes="open"] [data-eye="open"],.varya[data-eyes="up"] [data-eye="up"],.varya[data-eyes="left"] [data-eye="left"],.varya[data-eyes="right"] [data-eye="right"],.varya[data-eyes="closed"] [data-eye="closed"],.varya[data-eyes="half"] [data-eye="half"],.varya[data-eyes="happy"] [data-eye="happy"],.varya[data-eyes="wide"] [data-eye="wide"],.varya[data-eyes="stern"] [data-eye="stern"]{display:inline}
.varya.p-excl [data-prop="excl"],.varya.p-sweat [data-prop="sweat"],.varya.p-zzz [data-prop="zzz"],.varya.p-q [data-prop="q"],.varya.p-bubbles [data-prop="bubbles"],.varya.p-anger [data-prop="anger"],.varya.p-spark [data-prop="spark"],.varya.p-clock [data-prop="clock"],.varya.p-key [data-prop="key"],.varya.p-cloudx [data-prop="cloudx"]{display:inline}
.varya.l-calm .rig{animation:varya-breathe var(--varya-breathe) var(--varya-ease) infinite}
.varya.l-tilt .head{animation:varya-ponder var(--varya-breathe) var(--varya-ease) infinite}
.varya.l-run .rig{animation:varya-stride var(--varya-run) var(--varya-ease) infinite}
.varya.l-run .wingR{animation:varya-wingbeat var(--varya-run) var(--varya-ease) infinite}
.varya.l-run .legsA{display:none}.varya.l-run .legsB,.varya.l-run .legsC{display:inline}
.varya.l-run .legsB{animation:varya-leg-b var(--varya-run) steps(1) infinite}.varya.l-run .legsC{animation:varya-leg-c var(--varya-run) steps(1) infinite}
.varya.a-nod .head{animation:varya-nod var(--varya-gesture) var(--varya-ease)}
.varya.a-wave .wingR,.varya.a-happy .wingR{animation:varya-wave .62s var(--varya-ease) 3}
.varya.a-hop .whole{animation:varya-hop .28s var(--varya-ease)}
.varya.a-happy .whole{animation:varya-hop .5s var(--varya-ease) 3}
.varya.a-peck .rig{animation:varya-peck .6s var(--varya-ease) 3}
.varya.a-shake .rig,.varya.a-stern .head{animation:varya-shake .26s var(--varya-ease) 5}
.varya.a-think .head,.varya.a-look .head{animation:varya-ponder 1.6s var(--varya-ease)}
.varya.a-panic .head{animation:varya-nod var(--varya-gesture) var(--varya-ease) 2}
.varya.a-sleepy .head{animation:varya-doze 3s var(--varya-ease)}
.varya.a-alert .whole,.varya.a-collect .whole{animation:varya-hop .5s var(--varya-ease) 2}
.varya .steam{animation:varya-steam 3s var(--varya-ease) infinite}
@keyframes varya-breathe{50%{transform:translateY(-1.5px) scale(1.008,1.012)}}
@keyframes varya-ponder{25%,70%{transform:rotate(-7deg)}85%{transform:rotate(2deg)}}
@keyframes varya-stride{25%,75%{transform:translateY(-2px)}50%{transform:translateY(0)}}
@keyframes varya-wingbeat{50%{transform:rotate(-10deg)}}
@keyframes varya-leg-b{50%{opacity:0}}@keyframes varya-leg-c{0%{opacity:0}50%{opacity:1}}
@keyframes varya-nod{25%,65%{transform:rotate(-7deg) translateY(2px)}45%,100%{transform:rotate(0)}}
@keyframes varya-wave{35%,65%{transform:rotate(-65deg)}50%{transform:rotate(-48deg)}}
@keyframes varya-hop{40%{transform:translateY(-6px) scale(.98,1.02)}75%{transform:translateY(0) scale(1.03,.97)}}
@keyframes varya-peck{45%{transform:translate(-2px,4px) rotate(-13deg)}}
@keyframes varya-shake{25%{transform:rotate(-3deg)}75%{transform:rotate(3deg)}}
@keyframes varya-doze{60%{transform:rotate(-12deg) translateY(3px)}80%{transform:rotate(3deg)}}
@keyframes varya-steam{50%{transform:translateY(-3px);opacity:.4}}
.varya[data-legs="b"] .legsA,.varya[data-legs="c"] .legsA{display:none}.varya[data-legs="b"] .legsB,.varya[data-legs="c"] .legsC{display:inline}
/* Насест и индикатор: крупный силуэт и глаза; микродетали и предметы несут смысл соседним текстом. */
[data-mascot-detail="compact"] .varya .detail,[data-mascot-detail="compact"] .varya [data-prop],[data-mascot-detail="compact"] .varya .glint{display:none}
[data-mascot-detail="compact"] .varya .anatomy{transform:translate(-8px,-17px) scale(1.14);transform-origin:0 0}
[data-mascot-detail="compact"] .varya .mug,[data-mascot-detail="compact"] .varya .leaf{display:none}
.varya.still *{animation:none!important}
@media(prefers-reduced-motion:reduce){.varya *{animation:none!important}}
`;
function injectCSS(doc) {
  if (doc.getElementById("varya-css")) return;
  const st = doc.createElement("style"); st.id = "varya-css"; st.textContent = CSS; doc.head.appendChild(st);
}

/**
 * Живая ворона внутри el. opts: { theme = "orig", situation = "connect", idle = true,
 *   onSay(label, text, situation) — реплика после нажатия, onAnim(anim | null) — начало и конец реакции }.
 * BOTAI: ещё opts.still (без анимаций, поза pose), opts.skin ("new-year" | "mug" | "leaf"),
 *   opts.idleRange = [мин, макс] мс между морганиями, opts.lookRange = [мин, макс] мс между поворотами головы,
 *   opts.random — источник случайности для idle (по умолчанию Math.random; Botai передаёт детерминированный).
 * Возвращает { svg, react(), act(name), setSituation(id), setTheme(name), setSkin(name), setStill(bool), setLoop(name),
 *   settle() — вернуться к hold/pose ситуации, staticSVG(), destroy() }.
 */
function mount(el, opts = {}) {
  const doc = el.ownerDocument; injectCSS(doc);
  const svgEl = doc.createElementNS("http://www.w3.org/2000/svg", "svg");
  svgEl.setAttribute("viewBox", VIEWBOX); svgEl.setAttribute("shape-rendering", "geometricPrecision");
  svgEl.setAttribute("aria-hidden", "true"); svgEl.classList.add("varya");
  svgEl.style.overflow = "visible";
  el.appendChild(svgEl);
  const ringId = nextId();
  const media = doc.defaultView?.matchMedia?.("(prefers-reduced-motion: reduce)");
  let reduce = !!media?.matches, requestedStill = !!opts.still;
  const rand = typeof opts.random === "function" ? opts.random : () => .5;
  let cur = byId(opts.situation), themeName = "orig", skinName = null, loopName = null, still = !!opts.still || reduce;
  let timers = new Set(), busy = false, stopped = false, idleT = null, sinceLook = 0, nextLook = 0;
  const later = (ms, f) => { const timer = setTimeout(() => { timers.delete(timer); if (!stopped) f(); }, ms); timers.add(timer); };
  const clear = () => { timers.forEach(clearTimeout); timers.clear(); };
  const setProps = (list) => { [...svgEl.classList].filter((c) => c.startsWith("p-")).forEach((c) => svgEl.classList.remove(c)); list.forEach((p) => svgEl.classList.add("p-" + p)); };
  const setAnim = (a) => { [...svgEl.classList].filter((c) => c.startsWith("a-")).forEach((c) => svgEl.classList.remove(c)); void svgEl.getBoundingClientRect();
    if (a) svgEl.classList.add("a-" + a); if (opts.onAnim) opts.onAnim(a || null); };
  // BOTAI: петля l-run | l-calm | l-tilt живёт, пока нет реакции; в статичном режиме петель нет
  const setLoop = (name) => { loopName = name || null; [...svgEl.classList].filter((c) => c.startsWith("l-")).forEach((c) => svgEl.classList.remove(c));
    if (loopName && !still) svgEl.classList.add("l-" + loopName); };
  const hold = () => cur.hold || {};
  const holdEyes = () => hold().eyes || cur.rest;
  // вид «в покое»: живой — hold ситуации, статичный — её поза pose
  function settle() {
    if (still) { const p = cur.pose || {}; svgEl.dataset.eyes = p.eyes || "open"; svgEl.dataset.legs = p.legs || "a"; setProps(p.props || []); setLoop(null); }
    else { svgEl.dataset.eyes = holdEyes(); delete svgEl.dataset.legs; setProps(hold().props || []); setLoop(hold().loop); }
  }

  function setTheme(name) {
    themeName = THEMES[name] ? name : "orig";
    svgEl.innerHTML = markup(THEMES[themeName], null, ringId);
    svgEl.classList.toggle("pj", themeName === "pj");
  }
  function setSkin(name) {
    skinName = SKINS.includes(name) ? name : null;
    [...svgEl.classList].filter((c) => c.startsWith("skin-")).forEach((c) => svgEl.classList.remove(c));
    if (skinName) svgEl.classList.add("skin-" + skinName);
  }
  function setStill(value) {
    requestedStill = !!value; still = requestedStill || reduce; svgEl.classList.toggle("still", still);
    clear(); clearTimeout(idleT); if (busy) { busy = false; setAnim(null); }
    settle();
    if (!still && opts.idle !== false && !stopped) idle();
  }
  function setSituation(id) {
    cur = byId(id); svgEl.dataset.sit = cur.id;
    clear(); if (busy) { busy = false; setAnim(null); }
    settle();
  }
  function react() {
    if (still || stopped) return;
    clear(); busy = true; setLoop(null);
    const s = cur, ms = s.ms || 3200;
    setAnim(s.anim);
    svgEl.dataset.eyes = s.eyes[0]; setProps(s.prop.slice(0, 1));
    if (600 < ms) later(600, () => { svgEl.dataset.eyes = s.eyes[1]; if (s.prop[1]) setProps(s.beat2 || s.prop); });
    if (s.id === "night") later(1900, () => { svgEl.dataset.eyes = "wide"; setProps([]); });
    if (2200 < ms) later(2200, () => { svgEl.dataset.eyes = s.eyes[2]; });
    later(ms, () => { setAnim(null); settle(); busy = false; });
  }
  // BOTAI: одноразовое действие сцены (peck | shake | look | wave | nod | hop), потом возврат к hold
  function act(name) {
    const a = ACTS[name];
    if (!a || still || stopped) return;
    clear(); busy = true; setLoop(null);
    setAnim(a.anim);
    a.seq.forEach(([t, eyes]) => { if (t) later(t, () => { svgEl.dataset.eyes = eyes; }); else svgEl.dataset.eyes = eyes; });
    later(a.ms, () => { setAnim(null); settle(); busy = false; });
  }
  // в покое: моргает и поглядывает по сторонам
  // BOTAI: с opts.lookRange моргает по idleRange, а голову поворачивает не чаще lookRange (насест: раз в 8–14 с)
  function drawLook(range) { return range[0] + rand() * (range[1] - range[0]); }
  function idle() {
    const blink = opts.idleRange || [1800, 4000], delay = drawLook(blink);
    if (still || stopped) return;
    idleT = setTimeout(() => {
      if (stopped) return;
      sinceLook += delay;
      if (!busy && !still && !reduce && cur.rest !== "happy") {
        const r = rand();
        let look;
        if (opts.lookRange) {
          if (!nextLook) nextLook = drawLook(opts.lookRange);
          if (sinceLook >= nextLook) { look = r < .4 ? "left" : r < .7 ? "up" : "right"; sinceLook = 0; nextLook = drawLook(opts.lookRange); }
          else look = "closed";
        } else look = r < .5 ? "closed" : r < .7 ? "left" : r < .85 ? "up" : "right";
        svgEl.dataset.eyes = look;
        later(look === "closed" ? 140 : 700, () => { if (!busy && !still && !stopped) svgEl.dataset.eyes = holdEyes(); });
      }
      idle();
    }, delay);
  }
  const onMotionChange = (event) => { reduce = event.matches; setStill(requestedStill); };
  media?.addEventListener?.("change", onMotionChange);
  setTheme(opts.theme); setSkin(opts.skin); svgEl.classList.toggle("still", still); setSituation(cur.id);
  if (opts.idle !== false && !still) idle();
  return {
    svg: svgEl, react, act, setSituation, setTheme, setSkin, setStill, setLoop, settle,
    get situation() { return cur.id; }, get theme() { return themeName; }, get skin() { return skinName; }, get still() { return still; },
    staticSVG: () => svg({ situation: cur.id, theme: themeName, skin: skinName }),
    destroy() { media?.removeEventListener?.("change", onMotionChange); stopped = true; clear(); clearTimeout(idleT); svgEl.remove(); }
  };
}

function portrait({maskable=false,transparent=false}={}) {
  const transform=maskable?'translate(23.04 23.04) scale(.64)':'translate(0 0)';
  const headTransform=transparent?'translate(-22 3) scale(1.4)':'translate(-16 -12) scale(1.32)';
  return `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 128 128" role="img" aria-label="Варя — BotAI">${transparent?'':`<rect width="128" height="128" fill="${PALETTE.BACKGROUND}"/>`}`+
    `<defs><clipPath id="varya-portrait-clip"><rect width="128" height="128"/></clipPath></defs><g class="portrait" transform="${transform}"><g clip-path="url(#varya-portrait-clip)"><g transform="${headTransform}">`+
    (transparent?'':path('M39 65C24 87 29 114 65 116C100 116 107 84 90 65Z',PALETTE.DARK))+
    drawHead(THEMES.orig,{eyes:'open',props:[]})+`</g></g></g></svg>`;
}
root.Varya = { portrait, PALETTE, THEMES, SITUATIONS, ACTS, SKINS, GUIDE, VIEWBOX, CSS, svg, mount };
})(typeof window !== "undefined" ? window : globalThis);
