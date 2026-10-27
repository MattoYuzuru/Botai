/**
 * «Один анимированный маскот на экран» (docs/design/mascot.md §2). Каждая анимированная Варя занимает место в
 * очереди, когда монтируется, и освобождает его, когда исчезает. Анимацию получает только первая; остальные на
 * этом экране показываются статичной позой, пока первая не уйдёт, — тогда место переходит к следующей.
 * Хранилище — модуль, а не React-контекст: экраны сменяют друг друга, а правило касается всего приложения.
 */
type Listener = () => void;

const claims: symbol[] = [];
const listeners = new Set<Listener>();

const notify = () => {
  for (const listener of listeners) listener();
};

/** Занять место в очереди анимированных. Возвращает функцию, которая его освобождает. */
export function claimAnimation(id: symbol): () => void {
  if (!claims.includes(id)) claims.push(id);
  notify();
  return () => {
    const index = claims.indexOf(id);
    if (index >= 0) {
      claims.splice(index, 1);
      notify();
    }
  };
}

/** Анимирован ли сейчас этот маскот: он первый в очереди. */
export function holdsAnimation(id: symbol): boolean {
  return claims[0] === id;
}

export function subscribeAnimationClaims(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Только для тестов: очередь пуста. */
export function resetAnimationClaims(): void {
  claims.length = 0;
  notify();
}
