/**
 * Варя в чате (docs/design/mascot.md §4): на сцене пустого чата и на насесте над полем ввода, с пузырями-репликами.
 * Всё, что она «знает», приходит одним объектом состояния (`useMascot` → `resolveMascot`); сообщений чата и текста,
 * который человек печатает, у неё нет — только признаки «агент отвечает», «ждёт вас» и «человек печатает».
 */
import {
  BUBBLE_MS,
  cycleLines,
  isMascotStatic,
  MascotBubble,
  type MascotDockProps,
  type MascotHandle,
  type MascotSituationId,
  REACTION_MS,
  useMascotQuiet,
} from "@botai/design";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { useMascot } from "@/features/mascot/use-mascot";

/** Стадию работы агента Варя озвучивает не чаще раза в 4 секунды (mascot.md §4.3). */
const STAGE_BUBBLE_GAP_MS = 4_000;
/** Три быстрых нажатия — она машет крылом: не дольше 1.2 с между первым и третьим. */
const TAP_WINDOW_MS = 1_200;
const WAVE_KEY = "botai.mascot.waved";

export interface ChatMascotInput {
  /** Пустой чат — сцена, иначе насест. */
  mode: "floor" | "perch";
  /** Агент отвечает или ждёт человека. */
  replying: boolean;
  waitingForHuman: boolean;
  /** Стадия работы агента словами сервера («Ищу расписание»). */
  stage: string | null;
  /** Человек печатает: Варя не говорит. Сам текст она не получает. */
  typing: boolean;
  keyboardOpen: boolean;
  /** Растёт, когда пришёл новый ответ агента: Варя кивает один раз. */
  replyCount: number;
  /** Смена чата сбрасывает «приветствие один раз за открытие». */
  threadKey: string;
}

type DockProps = Pick<
  MascotDockProps,
  "mode" | "situation" | "theme" | "skin" | "motion" | "seed" | "bubble" | "onTap" | "ref"
>;

/** Пузырь: один за раз, исчезает через 6 секунд или по нажатию. */
function useBubble() {
  const [text, setText] = useState<string | null>(null);
  const timer = useRef<number | null>(null);
  const hide = useCallback(() => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
    setText(null);
  }, []);
  const show = useCallback((next: string) => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    setText(next);
    timer.current = window.setTimeout(() => {
      timer.current = null;
      setText(null);
    }, BUBBLE_MS);
  }, []);
  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );
  return { text, show, hide };
}

function waveOnce(): boolean {
  try {
    if (window.sessionStorage.getItem(WAVE_KEY)) return false;
    window.sessionStorage.setItem(WAVE_KEY, "1");
  } catch {
    /* хранилище недоступно: машет по разу за загрузку страницы */
  }
  return true;
}

export function useChatMascot(input: ChatMascotInput): DockProps {
  const { mode, replying, waitingForHuman, stage, typing, keyboardOpen, replyCount, threadKey } = input;
  const { presentation, acknowledge } = useMascot({
    screen: "chat",
    activity: replying ? "thinking" : "idle",
    keyboardOpen,
  });
  const quietFlags = useMascotQuiet();
  const dock = useRef<MascotHandle>(null);
  const [seed] = useState(() => Math.floor(Date.now() / 1000));
  const { text: bubbleText, show: showBubble, hide: hideBubble } = useBubble();
  const quiet = typing || keyboardOpen || !presentation.visible;

  // Пока человек печатает, она молчит.
  useEffect(() => {
    if (quiet) hideBubble();
  }, [quiet, hideBubble]);

  // Приветствие из данных — один раз за открытие пустого чата.
  const greeted = useRef<string | null>(null);
  const greeting = presentation.line;
  useEffect(() => {
    if (mode !== "floor" || quiet || !greeting || greeted.current === threadKey) return;
    const timer = window.setTimeout(() => {
      greeted.current = threadKey;
      showBubble(greeting);
    }, 700);
    return () => window.clearTimeout(timer);
  }, [mode, quiet, greeting, threadKey, showBubble]);

  // Стадия работы агента словами сервера, не чаще раза в 4 секунды.
  const lastStage = useRef({ text: "", at: 0 });
  useEffect(() => {
    if (!replying || quiet || !stage || stage === lastStage.current.text) return;
    const now = Date.now();
    if (now - lastStage.current.at < STAGE_BUBBLE_GAP_MS) return;
    lastStage.current = { text: stage, at: now };
    showBubble(stage);
  }, [replying, quiet, stage, showBubble]);

  // Ответ пришёл — один кивок. Историю при открытии чата не кивает.
  const seenReplies = useRef<{ key: string; count: number } | null>(null);
  useEffect(() => {
    const seen = seenReplies.current;
    if (seen && seen.key === threadKey && replyCount > seen.count) dock.current?.act("nod");
    seenReplies.current = { key: threadKey, count: replyCount };
  }, [replyCount, threadKey]);

  // Событие «сдано» — одна реакция, затем оно подтверждается и не повторится.
  const reactionEventId = presentation.reactionEventId;
  const visible = presentation.visible;
  useEffect(() => {
    if (!reactionEventId || !visible) return;
    dock.current?.act("wave");
    const timer = window.setTimeout(() => acknowledge(reactionEventId), REACTION_MS);
    return () => window.clearTimeout(timer);
  }, [reactionEventId, visible, acknowledge]);

  // Нажатие: следующая реплика её ситуации; три быстрых — взмах крылом, один раз за сессию.
  const taps = useRef<number[]>([]);
  const shown = bubbleText;
  const lineKey = presentation.lineKey;
  const lineFields = presentation.lineFields;
  const onTap = useCallback(() => {
    const now = Date.now();
    taps.current = [...taps.current.filter((at) => now - at <= TAP_WINDOW_MS), now];
    if (taps.current.length >= 3 && waveOnce()) {
      taps.current = [];
      dock.current?.act("wave");
      return;
    }
    const next = lineKey ? cycleLines(lineKey, lineFields, shown ?? greeting, 1) : null;
    if (next) showBubble(next);
    else hideBubble();
  }, [lineKey, lineFields, shown, greeting, showBubble, hideBubble]);

  const base: MascotSituationId =
    mode === "floor" ? "idle" : waitingForHuman ? "tip" : replying ? "think" : "perch";
  // Состояние источника важнее позы: ключ — нужен вход, облако — нет сети (честно, без цвета и звука).
  const situation: MascotSituationId =
    presentation.situation === "reauth" || presentation.situation === "offline"
      ? presentation.situation
      : base;

  const node: ReactNode =
    bubbleText && !quiet ? (
      <MascotBubble text={bubbleText} tail={mode === "floor" ? "center" : "right"} onClick={hideBubble} />
    ) : null;

  return {
    ref: dock,
    mode,
    situation,
    theme: presentation.theme,
    skin: presentation.skin,
    // Движение по тихим режимам, а не по ситуации таблицы §2: «просрочено» статично само по себе, а насест живой.
    motion: typing || isMascotStatic({ ...quietFlags, keyboardOpen }) ? "static" : "animated",
    seed,
    bubble: node,
    onTap,
  };
}
