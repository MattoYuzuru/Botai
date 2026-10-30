import { isMascotStatic, Mascot, SyncIndicator, type SyncState, useMascotQuiet } from "@botai/design";

/**
 * `SyncIndicator` с Варей 20 px вместо точки (styleguide §8): фоновое обновление или починка. `aria-live` объявляет
 * только итог, как у точки. Бежит на месте, пока обновление идёт; в тихих режимах стоит позой. Первая анимированная
 * Варя на экране получает анимацию, остальные стоят (`MascotAnimationPolicy`).
 */
export function MascotSyncIndicator({ state, className }: { state: SyncState; className?: string }) {
  const quiet = useMascotQuiet();
  return (
    <SyncIndicator
      state={state}
      size="mascot"
      className={className}
      mascot={
        <Mascot
          detail="compact"
          situation="sync"
          flip
          size="fill"
          className="size-full"
          motion={isMascotStatic(quiet) ? "static" : "animated"}
        />
      }
    />
  );
}
