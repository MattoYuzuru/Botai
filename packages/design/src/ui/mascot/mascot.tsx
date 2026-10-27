import { type Ref, useEffect, useImperativeHandle, useRef } from "react";
import { cn } from "../../lib/cn";
import { useLatest } from "../../lib/hooks";
import type {
  MascotAct,
  MascotLoop,
  MascotMotion,
  MascotSituationId,
  MascotSkinId,
  MascotTheme,
} from "../../mascot/types";
import { getVarya, seededRandom, type VaryaHandle } from "../../mascot/varya";

/** Что можно сделать с живой Варей снаружи: действия сцены и петля бега. Данных наружу не отдаёт. */
export interface MascotHandle {
  act: (name: MascotAct) => void;
  react: () => void;
  setLoop: (name: MascotLoop | null) => void;
  /** Вернуться к «держащему» виду ситуации после действия или бега. */
  settle: () => void;
}

export interface MascotProps {
  situation: MascotSituationId;
  theme?: MascotTheme;
  /** Сезонный образ из реестра §7. */
  skin?: MascotSkinId | null;
  /** `static` — поза без петель (reduced motion, спокойный маскот, скрытая вкладка). */
  motion?: MascotMotion;
  /**
   * Сторона квадрата, px. Спрайт 32×32 стоит в поле 40×40, поэтому кратные 40 размеры дают целые пиксели.
   * `"fill"` — размер задаёт родитель или `className`.
   */
  size?: number | "fill";
  /** Смотреть вправо: бежит по рельсу слева направо. По умолчанию клюв влево. */
  flip?: boolean;
  /** Зерно детерминированного idle (моргание, взгляд по сторонам). */
  seed?: number;
  /** Насест: моргает по `idleRange`, голову поворачивает не чаще `lookRange`. */
  idleRange?: readonly [number, number];
  lookRange?: readonly [number, number];
  className?: string;
  ref?: Ref<MascotHandle>;
}

/**
 * Живая Варя: монтирует renderer (`Varya.mount`) в `useEffect` и разбирает его при размонтировании. Декоративна
 * (`aria-hidden`), фокус не принимает, не получает никаких данных, кроме ситуации, темы, образа и размера.
 * Повреждённый или отсутствующий renderer даёт пустое место — экран от картинки не зависит.
 */
export function Mascot({
  situation,
  theme = "orig",
  skin = null,
  motion = "animated",
  size = 48,
  flip = false,
  seed = 1,
  idleRange,
  lookRange,
  className,
  ref,
}: MascotProps) {
  const host = useRef<HTMLSpanElement>(null);
  const handle = useRef<VaryaHandle | null>(null);
  const latest = useLatest({ situation, theme, skin, motion });

  useImperativeHandle(
    ref,
    () => ({
      act: (name) => handle.current?.act(name),
      react: () => handle.current?.react(),
      setLoop: (name) => handle.current?.setLoop(name),
      settle: () => handle.current?.settle(),
    }),
    [],
  );

  // Один экземпляр на жизнь компонента; правки пропсов идут отдельными эффектами ниже.
  const idleKey = idleRange ? `${idleRange[0]}-${idleRange[1]}` : "";
  const lookKey = lookRange ? `${lookRange[0]}-${lookRange[1]}` : "";
  // biome-ignore lint/correctness/useExhaustiveDependencies: границы idle читаются из ключей, пересоздавать renderer при смене массива не нужно
  useEffect(() => {
    const api = getVarya();
    const element = host.current;
    if (!api || !element) return;
    const current = latest.current;
    try {
      handle.current = api.mount(element, {
        theme: current.theme,
        situation: current.situation,
        skin: current.skin,
        still: current.motion === "static",
        idleRange,
        lookRange,
        random: seededRandom(seed),
      });
    } catch {
      handle.current = null;
      return;
    }
    return () => {
      handle.current?.destroy();
      handle.current = null;
    };
  }, [seed, idleKey, lookKey, latest]);

  useEffect(() => {
    if (handle.current && handle.current.theme !== theme) handle.current.setTheme(theme);
  }, [theme]);
  useEffect(() => {
    if (handle.current && handle.current.situation !== situation) handle.current.setSituation(situation);
  }, [situation]);
  useEffect(() => {
    if (handle.current && handle.current.skin !== skin) handle.current.setSkin(skin);
  }, [skin]);
  useEffect(() => {
    const still = motion === "static";
    if (handle.current && handle.current.still !== still) handle.current.setStill(still);
  }, [motion]);

  return (
    <span
      ref={host}
      aria-hidden="true"
      data-mascot=""
      data-situation={situation}
      data-motion={motion}
      data-skin={skin ?? undefined}
      style={size === "fill" ? undefined : { width: size, height: size }}
      className={cn(
        "pointer-events-none inline-block shrink-0 select-none [&>svg]:block [&>svg]:size-full",
        flip && "-scale-x-100",
        className,
      )}
    />
  );
}
