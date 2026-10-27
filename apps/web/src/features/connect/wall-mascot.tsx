import { isMascotStatic, Mascot, type MascotSituationId, useMascotQuiet } from "@botai/design";
import type { WallMascotState } from "@/widgets/progress-wall/progress-wall";

/** Поза Вари по состоянию работы (mascot.md §3): бежит на месте, ждёт ответа, сдала, не вышло. */
const SITUATION: Record<WallMascotState, MascotSituationId> = {
  running: "connect",
  waiting: "tip",
  done: "done",
  failed: "error",
};

/**
 * Варя на `ProgressWall`. Пока работа идёт — бежит по рельсу на месте (число стадий неизвестно, процентов нет);
 * когда агент ждёт человека — стоит в позе ожидания; после итога — рядом с названием. Всё, что она «говорит»,
 * уже есть текстом на экране: стадия, прошедшее время, вопрос. Анимацию гасят reduced motion, «спокойный
 * маскот», слабое устройство и скрытая вкладка.
 */
export function WallMascot({ state, slot }: { state: WallMascotState; slot: "rail" | "header" }) {
  const quiet = useMascotQuiet();
  const still = isMascotStatic(quiet) || state !== "running";
  return (
    <Mascot
      situation={SITUATION[state]}
      motion={still ? "static" : "animated"}
      size={slot === "rail" ? 40 : 48}
      // На рельсе Варя бежит слева направо: клюв вправо; контейнер рельса центрирует поле вместе с отступом, поэтому отступ слева равен ширине спрайта: левый край Вари встаёт ровно на начало рельса.
      flip={slot === "rail"}
      className={slot === "rail" ? "ml-10" : undefined}
    />
  );
}
