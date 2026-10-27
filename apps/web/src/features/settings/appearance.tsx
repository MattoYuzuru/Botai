import {
  motionPreference,
  SegmentedControl,
  Switch,
  seasonalPreference,
  type ThemePreference,
  themePreference,
} from "@botai/design";

/** Оформление: тема (системная по умолчанию), спокойное движение и сезонные образы Вари. Хранится локально. */
export function AppearanceSettings() {
  const theme = themePreference.use();
  const motion = motionPreference.use();
  const seasonal = seasonalPreference.use();
  return (
    <div className="flex flex-col gap-2">
      <div className="flex max-w-sm flex-col gap-2 px-3">
        <p className="type-label text-fg">Тема</p>
        <SegmentedControl<ThemePreference>
          label="Тема"
          value={theme}
          onValueChange={(value) => themePreference.write(value)}
          options={[
            { value: "system", label: "Система" },
            { value: "light", label: "Светлая" },
            { value: "dark", label: "Тёмная" },
          ]}
        />
      </div>
      <Switch
        label="Спокойное движение"
        description="Без декоративных анимаций и сезонного оживления"
        checked={motion === "calm"}
        onCheckedChange={(checked) => motionPreference.write(checked ? "calm" : "system")}
      />
      <Switch
        label="Сезонные образы Вари"
        description="Колпак на Новый год, кружка в сессию"
        checked={seasonal === "on"}
        onCheckedChange={(checked) => seasonalPreference.write(checked ? "on" : "off")}
      />
    </div>
  );
}
