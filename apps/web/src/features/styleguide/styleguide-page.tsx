import {
  AccentScope,
  Card,
  motionPreference,
  SegmentedControl,
  Switch,
  themePreference,
  UNIVERSITY_ACCENTS,
  Wordmark,
} from "@botai/design";
import { type ReactNode, useEffect, useState } from "react";
import { AgentBlocksSection, ProgressWallSection } from "./sections-agent";
import { ControlsSection, PaletteSection, StatusSection, TypographySection } from "./sections-foundation";
import { ChatSection, NavigationSection, StudySection } from "./sections-product";

type ThemeChoice = "system" | "light" | "dark";
type Zoom = "100" | "150" | "200";
type Frame = "full" | "320" | "390" | "430";

const FRAME_WIDTH: Record<Frame, string> = {
  full: "",
  "320": "max-w-[20rem]",
  "390": "max-w-[24.375rem]",
  "430": "max-w-[26.875rem]",
};

const SECTIONS = [
  ["palette", "Палитра"],
  ["typography", "Типографика"],
  ["controls", "Ввод"],
  ["status", "Состояния"],
  ["study", "Учебные данные"],
  ["chat", "Чат и агент"],
  ["blocks", "Блоки агента"],
  ["progress-wall", "ProgressWall"],
  ["navigation", "Навигация"],
] as const;

function Gallery() {
  return (
    <div className="flex flex-col gap-12">
      <PaletteSection />
      <TypographySection />
      <ControlsSection />
      <StatusSection />
      <StudySection />
      <ChatSection />
      <AgentBlocksSection />
      <ProgressWallSection />
      <NavigationSection />
    </div>
  );
}

/** Панель с принудительной темой: рядом видны обе. Токены берут тему из color-scheme узла. */
function ThemePanel({ theme, children }: { theme: "light" | "dark"; children: ReactNode }) {
  return (
    <div
      data-theme={theme}
      className="rounded-xl border border-border bg-bg p-4 text-fg"
      data-testid={`panel-${theme}`}
    >
      <p className="mb-6 type-label text-fg-muted">{theme === "light" ? "Светлая тема" : "Тёмная тема"}</p>
      {children}
    </div>
  );
}

/**
 * Витрина /styleguide: все компоненты каталога §8 во всех состояниях, обе темы, акценты вузов,
 * ширины 320/390/430, зум текста до 200 % и спокойное движение. Данные синтетические, вход в вуз не нужен.
 */
export function StyleguidePage() {
  const theme = themePreference.use() as ThemeChoice;
  const motion = motionPreference.use();
  const [accent, setAccent] = useState<string>(UNIVERSITY_ACCENTS[0].id);
  const [zoom, setZoom] = useState<Zoom>("100");
  const [frame, setFrame] = useState<Frame>("full");
  const [sideBySide, setSideBySide] = useState(false);

  // Зум текста: меняем кегль корня, все размеры в rem следуют за ним.
  useEffect(() => {
    document.documentElement.style.fontSize = `${zoom}%`;
    return () => {
      document.documentElement.style.fontSize = "";
    };
  }, [zoom]);

  const hue = UNIVERSITY_ACCENTS.find((row) => row.id === accent)?.hue ?? UNIVERSITY_ACCENTS[0].hue;

  return (
    <AccentScope
      hue={hue}
      as="main"
      className="min-h-dvh bg-bg px-safe-4 pt-safe-6 pb-safe-12 text-fg xs:px-safe-5"
    >
      <div className="mx-auto flex max-w-page flex-col gap-8">
        <header className="flex flex-col gap-3">
          <div className="flex items-center justify-between gap-4">
            <Wordmark size="lg" />
            <p className="type-caption text-fg-muted">дизайн-система</p>
          </div>
          <h1 className="type-title text-fg">Витрина компонентов</h1>
          <p className="max-w-prose type-ui text-fg-muted">
            Все компоненты каталога в обеих темах. Данные синтетические: это не доказательство живого входа в
            вуз и не измерение производительности.
          </p>
          <nav aria-label="Разделы витрины">
            <ul className="flex flex-wrap gap-x-4 gap-y-1">
              {SECTIONS.map(([id, title]) => (
                <li key={id}>
                  <a
                    href={`#${id}`}
                    className="inline-flex min-h-11 min-w-11 items-center justify-center type-ui text-accent-fg underline-offset-2 hover:underline"
                  >
                    {title}
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        </header>

        <Card variant="sunken" className="flex flex-col gap-4" aria-label="Настройки витрины">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <div className="flex flex-col gap-2">
              <p className="type-label text-fg-muted">Тема</p>
              <SegmentedControl
                label="Тема"
                value={theme}
                onValueChange={(value) => themePreference.write(value)}
                options={[
                  { value: "system", label: "Система", icon: "theme-system" },
                  { value: "light", label: "Светлая", icon: "theme-light" },
                  { value: "dark", label: "Тёмная", icon: "theme-dark" },
                ]}
              />
            </div>
            <div className="flex flex-col gap-2">
              <p className="type-label text-fg-muted">Акцент пространства вуза</p>
              <SegmentedControl
                label="Акцент вуза"
                value={accent}
                onValueChange={setAccent}
                options={UNIVERSITY_ACCENTS.map((row) => ({ value: row.id, label: row.shortTitle }))}
              />
            </div>
            <div className="flex flex-col gap-2">
              <p className="type-label text-fg-muted">Зум текста</p>
              <SegmentedControl
                label="Зум текста"
                value={zoom}
                onValueChange={setZoom}
                options={[
                  { value: "100", label: "100 %" },
                  { value: "150", label: "150 %" },
                  { value: "200", label: "200 %" },
                ]}
              />
            </div>
            <div className="flex flex-col gap-2">
              <p className="type-label text-fg-muted">Ширина экрана</p>
              <SegmentedControl
                label="Ширина экрана"
                value={frame}
                onValueChange={setFrame}
                options={[
                  { value: "full", label: "Весь" },
                  { value: "320", label: "320" },
                  { value: "390", label: "390" },
                  { value: "430", label: "430" },
                ]}
              />
            </div>
          </div>
          <div className="grid grid-cols-1 gap-x-4 md:grid-cols-2">
            <Switch
              label="Светлая и тёмная рядом"
              description="Две панели с принудительной темой"
              checked={sideBySide}
              onCheckedChange={setSideBySide}
            />
            <Switch
              label="Спокойное движение"
              description="Циклы остановлены, переходы мгновенные"
              checked={motion === "calm"}
              onCheckedChange={(checked) => motionPreference.write(checked ? "calm" : "system")}
            />
          </div>
          <p className="type-caption text-fg-subtle">
            Ширина ограничивает только содержимое: адаптивные классы реагируют на настоящий размер окна,
            проверяйте 320/390/430 px изменением окна.
          </p>
        </Card>

        <div className={`mx-auto w-full ${FRAME_WIDTH[frame]}`}>
          {sideBySide ? (
            <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
              <ThemePanel theme="light">
                <Gallery />
              </ThemePanel>
              <ThemePanel theme="dark">
                <Gallery />
              </ThemePanel>
            </div>
          ) : (
            <Gallery />
          )}
        </div>
      </div>
    </AccentScope>
  );
}
