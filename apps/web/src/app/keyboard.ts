import { useMediaQuery } from "@botai/design";
import { useEffect, useState } from "react";

/**
 * Открыта ли экранная клавиатура над полем чата. На телефоне при фокусе в поле чата нижняя панель
 * скрывается, поле прилипает к границе клавиатуры (`interactive-widget=resizes-content`), Варя уходит
 * (styleguide §7). Определяем по фокусу внутри `[data-composer]` на устройствах с касанием: размер
 * `visualViewport` ненадёжен — iOS и Android ведут себя по-разному.
 */
export function useComposerKeyboard(): boolean {
  const touch = useMediaQuery("(max-width: 1023px) and (pointer: coarse)");
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!touch) {
      setOpen(false);
      return;
    }
    // Экранную клавиатуру открывает только текстовое поле: кнопки «+», микрофон и «Отправить» её не вызывают.
    const textField =
      "textarea, input:not([type=checkbox], [type=radio], [type=file], [type=button], [type=submit]), [contenteditable=true]";
    const inComposer = (target: EventTarget | null) =>
      target instanceof Element &&
      target.matches(textField) &&
      target.closest("[data-composer], [data-chat-input]") !== null;
    const onFocusIn = (event: FocusEvent) => setOpen(inComposer(event.target));
    const onFocusOut = (event: FocusEvent) => {
      if (!inComposer(event.relatedTarget)) setOpen(false);
    };
    document.addEventListener("focusin", onFocusIn);
    document.addEventListener("focusout", onFocusOut);
    return () => {
      document.removeEventListener("focusin", onFocusIn);
      document.removeEventListener("focusout", onFocusOut);
    };
  }, [touch]);

  return open;
}
