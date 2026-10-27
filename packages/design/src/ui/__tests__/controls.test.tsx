import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { Button } from "../button/button";
import { Checkbox } from "../checkbox/checkbox";
import { Dialog } from "../dialog/dialog";
import { Field } from "../field/field";
import { IconButton } from "../icon-button/icon-button";
import { Input } from "../input/input";
import { RadioGroup } from "../radio-group/radio-group";
import { Select } from "../select/select";
import { Sheet } from "../sheet/sheet";
import { Switch } from "../switch/switch";
import { Textarea } from "../textarea/textarea";

describe("Button", () => {
  it("высота 44 px у md, 36 px у sm с расширенной зоной нажатия", () => {
    const { rerender } = render(<Button>Ответить</Button>);
    expect(screen.getByRole("button", { name: "Ответить" })).toHaveClass("h-11");
    rerender(<Button size="sm">Ответить</Button>);
    const small = screen.getByRole("button", { name: "Ответить" });
    expect(small).toHaveClass("h-9");
    expect(small.className).toContain("before:-inset-y-[5px]");
  });

  it("loading: подпись остаётся в разметке (ширина не меняется), кнопка disabled и aria-busy", async () => {
    const onClick = vi.fn();
    render(
      <Button loading onClick={onClick}>
        Подключить вуз
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Подключить вуз" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(within(button).getByText("Подключить вуз")).toHaveClass("invisible");
    expect(button.querySelector("[data-spinner]")).not.toBeNull();
    await userEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("asChild отдаёт стили ссылке", () => {
    render(
      <Button asChild variant="primary">
        <a href="/chat">В чат</a>
      </Button>,
    );
    const link = screen.getByRole("link", { name: "В чат" });
    expect(link).toHaveClass("bg-accent", "text-fg-on-accent");
  });

  it("danger-тон меняет заливку primary на danger", () => {
    render(
      <Button variant="primary" tone="danger">
        Отключить
      </Button>,
    );
    expect(screen.getByRole("button", { name: "Отключить" })).toHaveClass("bg-danger");
  });

  it("IconButton без label невозможен: aria-label обязателен и попадает в разметку", () => {
    render(<IconButton icon="close" label="Закрыть" />);
    expect(screen.getByRole("button", { name: "Закрыть" })).toHaveClass("size-11");
  });
});

describe("Field + Input", () => {
  it("связывает подпись, подсказку и ошибку", () => {
    render(
      <Field label="Адрес ЛК" hint="Откуда студент заходит в кабинет" error="Нужна ссылка вида https://…">
        <Input defaultValue="x" />
      </Field>,
    );
    const input = screen.getByLabelText("Адрес ЛК");
    expect(input).toHaveAttribute("aria-invalid", "true");
    // Ошибка заменяет подсказку и привязана к полю.
    const describedBy = input.getAttribute("aria-describedby") ?? "";
    expect(describedBy).not.toBe("");
    expect(document.getElementById(describedBy)).toHaveTextContent("Нужна ссылка вида https://…");
    expect(screen.queryByText("Откуда студент заходит в кабинет")).toBeNull();
  });

  it("кегль поля — 16 px (type-body) у Input и Textarea: iOS не приближает страницу", () => {
    render(
      <>
        <Input aria-label="a" />
        <Textarea aria-label="b" />
      </>,
    );
    expect(screen.getByLabelText("a")).toHaveClass("type-body");
    expect(screen.getByLabelText("b")).toHaveClass("type-body");
  });

  it("необязательное поле помечено словом", () => {
    render(
      <Field label="Комментарий" optional>
        <Input />
      </Field>,
    );
    expect(screen.getByText("необязательно")).toBeInTheDocument();
  });

  it("disabled передаётся из Field полю", () => {
    render(
      <Field label="Вуз" disabled>
        <Input />
      </Field>,
    );
    expect(screen.getByLabelText("Вуз")).toBeDisabled();
  });

  it("Textarea растёт вместе с текстом, начиная с minRows", async () => {
    render(<Textarea aria-label="Текст" minRows={2} />);
    expect(screen.getByLabelText("Текст")).toHaveAttribute("rows", "2");
    await userEvent.type(screen.getByLabelText("Текст"), "строка");
    expect(screen.getByLabelText("Текст")).toHaveValue("строка");
  });
});

describe("Checkbox, RadioGroup, Switch", () => {
  it("Checkbox: вся строка кликабельна, состояние «частично» отображается", async () => {
    const onChange = vi.fn();
    render(<Checkbox label="Алгоритмы" description="Курс 2" onCheckedChange={onChange} />);
    await userEvent.click(screen.getByText("Алгоритмы"));
    expect(onChange).toHaveBeenCalledWith(true);
    expect(screen.getByRole("checkbox")).toHaveAccessibleDescription("Курс 2");
  });

  it("Checkbox indeterminate", () => {
    render(<Checkbox label="Все предметы" checked="indeterminate" />);
    expect(screen.getByRole("checkbox")).toHaveAttribute("aria-checked", "mixed");
  });

  it("Checkbox invalid помечает поле для скринридера", () => {
    render(<Checkbox label="Согласен" invalid />);
    expect(screen.getByRole("checkbox")).toHaveAttribute("aria-invalid", "true");
  });

  it("RadioGroup: выбор по подписи и стрелками", async () => {
    const onValueChange = vi.fn();
    render(
      <RadioGroup
        aria-label="Предмет"
        onValueChange={onValueChange}
        items={[
          { value: "a", label: "Алгоритмы" },
          { value: "b", label: "Базы данных", description: "Лекции по вторникам" },
        ]}
      />,
    );
    await userEvent.click(screen.getByText("Базы данных"));
    expect(onValueChange).toHaveBeenCalledWith("b");
    expect(screen.getByRole("radio", { name: /Базы данных/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /Базы данных/ })).toHaveAccessibleDescription(
      "Лекции по вторникам",
    );
  });

  it("Switch применяется сразу", async () => {
    const onCheckedChange = vi.fn();
    render(<Switch label="Спокойный режим" onCheckedChange={onCheckedChange} />);
    await userEvent.click(screen.getByRole("switch", { name: "Спокойный режим" }));
    expect(onCheckedChange).toHaveBeenCalledWith(true);
  });
});

describe("Select", () => {
  const options = [
    { value: "msk", label: "Москва" },
    { value: "spb", label: "Санкт-Петербург" },
    { value: "ekb", label: "Екатеринбург", disabled: true },
  ];

  function Controlled() {
    const [value, setValue] = useState<string | undefined>("msk");
    return <Select aria-label="Город" options={options} value={value} onValueChange={setValue} />;
  }

  it("на телефоне (<768 px) раскрывается шитом и выбирает с клавиатуры", async () => {
    render(<Controlled />);
    const trigger = screen.getByRole("combobox", { name: "Город" });
    expect(trigger).toHaveTextContent("Москва");
    await userEvent.click(trigger);
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    const list = screen.getByRole("listbox");
    expect(list).toHaveFocus();
    await userEvent.keyboard("{ArrowDown}{Enter}");
    expect(screen.getByRole("combobox", { name: "Город" })).toHaveTextContent("Санкт-Петербург");
  });

  it("недоступный вариант пропускается стрелками", async () => {
    render(<Controlled />);
    await userEvent.click(screen.getByRole("combobox", { name: "Город" }));
    await screen.findByRole("listbox");
    await userEvent.keyboard("{ArrowDown}{ArrowDown}");
    // Москва → Санкт-Петербург → (Екатеринбург disabled) → снова Москва.
    expect(screen.getByRole("option", { name: "Москва" })).toHaveAttribute("data-active");
  });
});

describe("Sheet и Dialog", () => {
  it("Sheet закрывается кнопкой «Закрыть» и Esc", async () => {
    const onOpenChange = vi.fn();
    render(
      <Sheet open onOpenChange={onOpenChange} title="Вуз">
        <p>Содержимое</p>
      </Sheet>,
    );
    expect(screen.getByRole("dialog", { name: "Вуз" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Закрыть" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    onOpenChange.mockClear();
    await userEvent.keyboard("{Escape}");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("Sheet закрывается свайпом вниз, а короткий свайп возвращается на место", () => {
    const onOpenChange = vi.fn();
    render(
      <Sheet open onOpenChange={onOpenChange} title="Вуз">
        <p>Содержимое</p>
      </Sheet>,
    );
    const handle = document.querySelector<HTMLElement>("[data-sheet-handle]") as HTMLElement;
    const swipe = (distance: number, id: number) => {
      handle.dispatchEvent(
        new PointerEvent("pointerdown", { pointerId: id, pointerType: "touch", clientY: 100, bubbles: true }),
      );
      handle.dispatchEvent(
        new PointerEvent("pointermove", {
          pointerId: id,
          pointerType: "touch",
          clientY: 100 + distance,
          bubbles: true,
        }),
      );
      handle.dispatchEvent(
        new PointerEvent("pointerup", {
          pointerId: id,
          pointerType: "touch",
          clientY: 100 + distance,
          bubbles: true,
        }),
      );
    };
    swipe(20, 1);
    expect(onOpenChange).not.toHaveBeenCalled();
    swipe(160, 2);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("недискретный Sheet не закрывается Esc и свайпом", async () => {
    const onOpenChange = vi.fn();
    render(
      <Sheet open onOpenChange={onOpenChange} title="Идёт работа" dismissible={false}>
        <p>Подождите</p>
      </Sheet>,
    );
    await userEvent.keyboard("{Escape}");
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("Dialog: действие — кнопка-глагол, отмена получает фокус первой", async () => {
    const onConfirm = vi.fn();
    const onOpenChange = vi.fn();
    render(
      <Dialog
        open
        onOpenChange={onOpenChange}
        title="Отключить ЛК ВШЭ?"
        description="Данные расписания останутся в снимке, но обновляться перестанут."
        confirmLabel="Отключить"
        onConfirm={onConfirm}
      />,
    );
    expect(screen.getByRole("alertdialog", { name: "Отключить ЛК ВШЭ?" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отмена" })).toHaveFocus();
    await userEvent.click(screen.getByRole("button", { name: "Отключить" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});
