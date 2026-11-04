import {
  ChartBlock,
  type ChartSpec,
  DataTable,
  IconButton,
  MonogramBadge,
  Panel,
  ProfileCard,
  RankList,
  StatGrid,
  StatTile,
} from "@botai/design";
import { Demo, Grid, Section } from "./layout";

const GRADES_CHART: ChartSpec = {
  type: "bar",
  title: "Оценки по предметам",
  labels: ["Алгоритмы", "Матанализ", "Дискретная математика", "История", "Английский"],
  series: [{ name: "Оценка (10 баллов)", values: [8, 9, 7, 10, 8] }],
};

const DYNAMICS_CHART: ChartSpec = {
  type: "line",
  title: "Динамика по месяцам",
  labels: ["Сентябрь", "Октябрь", "Ноябрь"],
  series: [{ name: "Балл", values: [7, 8, 9] }],
};

const GRADES_ROWS = [
  { course: "Алгоритмы", value: "8", scale: "10 баллов", date: "21 октября" },
  { course: "Математический анализ", value: "86", scale: "100 баллов", date: "28 октября" },
  { course: "История", value: "зачтено", scale: "зачёт", date: "30 октября" },
];

const RANKING = [
  { id: "1", position: 1, label: "Участник 1", score: "9,31" },
  { id: "2", position: 2, label: "Участник 2", score: "9,12" },
  { id: "3", position: 3, label: "Участник 3", score: "8,97" },
  { id: "12", position: 12, label: "Вы", score: "8,42", isMe: true },
];

const UNIVERSITIES = [
  { title: "Высшая школа экономики", shortTitle: "ВШЭ", hue: 262.2 },
  { title: "Центральный университет", shortTitle: "ЦУ", hue: 292 },
  { title: "Резерв", shortTitle: "Резерв", hue: 325 },
] as const;

/** Блоки панелей «Учёбы»: знак вуза, плитки показателей, рейтинг, таблица, профиль, рамка панели. */
export function PanelsSection() {
  return (
    <Section
      id="panels"
      title="Панели «Учёбы»"
      note="Рамка у панели одна: внутри неё плитки, график, рейтинг, таблица и профиль без своих рамок. Вуз — монограмма на его акценте, логотипов нет. Значения показаны как у источника, без пересчёта."
    >
      <Demo label="MonogramBadge — sm / md / lg на акцентах вузов; 4 знака и больше сокращаются до трёх">
        {UNIVERSITIES.map((university) => (
          <MonogramBadge key={university.title} {...university} size="sm" />
        ))}
        {UNIVERSITIES.map((university) => (
          <MonogramBadge key={university.title} {...university} size="md" />
        ))}
        {UNIVERSITIES.map((university) => (
          <MonogramBadge key={university.title} {...university} size="lg" />
        ))}
        <MonogramBadge title="Московский государственный технический университет" size="lg" hue={300} />
      </Demo>
      <Grid>
        <Demo
          label="Panel + StatGrid + StatTile — шкала, единица, тренд словами, нечётная плитка на всю строку"
          wide
        >
          <Panel
            title="Успеваемость"
            icon="grade"
            subtitle="Обновлено 3 ноября"
            university={UNIVERSITIES[0]}
            collapsible
            actions={<IconButton icon="more" label="Настроить панель «Успеваемость»" />}
          >
            <StatGrid label="Показатели успеваемости" columns={3}>
              <StatTile label="Средний балл" value="8,4" scale="10" icon="star" caption="по данным ЛК" />
              <StatTile
                label="Место в потоке"
                value="12"
                scale="140"
                icon="trophy"
                trend={{ direction: "up", label: "на 3 места выше, чем в сентябре" }}
              />
              <StatTile label="Предметов" value="7" unit="шт." icon="course" />
            </StatGrid>
          </Panel>
        </Demo>
        <Demo label="Panel + ChartBlock bare — график без второй рамки" wide>
          <Panel title="Оценки по предметам" icon="chart" university={UNIVERSITIES[1]} collapsible>
            <ChartBlock spec={GRADES_CHART} bare />
          </Panel>
        </Demo>
        <Demo label="Panel + RankList — «это вы»: метка, вес шрифта, подложка; итог «12 из 140»" wide>
          <Panel title="Рейтинг потока" icon="trophy" subtitle="осень 2026">
            <RankList label="Рейтинг потока" items={RANKING} total={140} />
          </Panel>
        </Demo>
        <Demo label="Panel + DataTable — таблица от 768 px, карточки строк на телефоне" wide>
          <Panel title="Оценки за семестр" icon="grade">
            <DataTable
              caption="Оценки за семестр"
              captionHidden
              columns={[
                { key: "course", header: "Предмет" },
                { key: "value", header: "Оценка", numeric: true },
                { key: "scale", header: "Шкала" },
                { key: "date", header: "Дата" },
              ]}
              rows={GRADES_ROWS}
            />
          </Panel>
        </Demo>
        <Demo label="Panel + ProfileCard — монограмма в слоте, пустые поля скрыты" wide>
          <Panel title="Профиль студента" icon="idCard">
            <ProfileCard
              name="Студент Тестовый"
              subtitle="Бакалавриат, очная форма"
              monogram={<MonogramBadge size="lg" {...UNIVERSITIES[0]} />}
              fields={[
                { label: "Группа", value: "БИ-21" },
                { label: "Программа", value: "Бизнес-информатика" },
                { label: "Курс", value: "3" },
                { label: "Статус", value: "Учится" },
                { label: "Стипендия", value: "" },
              ]}
            />
          </Panel>
        </Demo>
        <Demo label="Panel — загрузка и пустое состояние" wide>
          <div className="flex flex-col gap-3">
            <Panel title="Динамика" icon="trend" state="loading" />
            <Panel
              title="Рейтинг"
              icon="trophy"
              state="empty"
              emptyDescription="Источник пока не отдал рейтинг"
            />
            <Panel title="Динамика баллов" icon="trend">
              <ChartBlock spec={DYNAMICS_CHART} bare tableToggle={false} />
            </Panel>
          </div>
        </Demo>
      </Grid>
    </Section>
  );
}
