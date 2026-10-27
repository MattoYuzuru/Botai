import { type MascotScreen, MascotStage } from "@botai/design";
import { useMascotIntent } from "./use-intent";
import { useMascot } from "./use-mascot";

/**
 * Тихое присутствие Вари на экране (mascot.md §2 и §4.4): появляется, только когда ей есть что сказать по таблице
 * состояний — срочная работа, просрочка, пара через час, нужно войти снова, нет сети, идёт обновление. В покое и на
 * экранах без дела её нет. Смысл всегда текстом: бейдж, реплика и одна кнопка; нажатие по картинке не единственный
 * способ. Анимацию получает только первая Варя на экране.
 *
 * Подключение: `<MascotPresence screen="today" />` под шапкой экрана.
 */
const NOTICED: ReadonlySet<string> = new Set(["reauth", "offline", "sync"]);

export function MascotPresence({ screen, className }: { screen: MascotScreen; className?: string }) {
  const { presentation, acknowledge } = useMascot({ screen });
  const onIntent = useMascotIntent();
  const hasText = Boolean(presentation.line || presentation.badge || presentation.intent);
  // Покой без дела — тишина: ни картинки, ни подписи.
  if (presentation.situation === "idle" || !hasText) return null;
  // Нет сети, нужен вход, идёт починка — об этом уже говорит плашка источников экрана со своим действием;
  // второй «Войти снова» рядом — шум (styleguide §9: одно действие на смысл).
  if (NOTICED.has(presentation.situation)) return null;
  return (
    <MascotStage
      presentation={presentation}
      onIntent={onIntent}
      onReactionEnd={acknowledge}
      size={56}
      label="Варя"
      className={className}
    />
  );
}
