import {
  Badge,
  Card,
  CourseGradeCard,
  DeadlineItem,
  ScoreBar,
  ScoreStrip,
  type ScoreStripItem,
  UpdateRow,
} from "@botai/design";
import { Demo, Grid, Section } from "./layout";

const NOW = Date.UTC(2026, 10, 11, 12);

const TEN: ScoreStripItem[] = [
  { id: "1", fill: 0.9, text: "9 из 10", title: "Домашнее задание №1" },
  { id: "2", fill: 0.75, text: "7,5 из 10", title: "Домашнее задание №2" },
  { id: "3", fill: 0.6, text: "6 из 10", title: "Коллоквиум" },
  { id: "4", fill: 0.8, text: "8 из 10", title: "Контрольная №2" },
];

const MIXED: ScoreStripItem[] = [
  { id: "1", fill: 0.9, text: "9 из 10", title: "Домашнее задание №2" },
  { id: "2", fill: null, text: "зачтено", title: "Участие на семинарах" },
];

const HUNDRED: ScoreStripItem[] = [
  { id: "1", fill: 0.78, text: "78 из 100" },
  { id: "2", fill: 0.86, text: "86 из 100" },
  { id: "3", fill: 0.95, text: "95 из 100" },
  { id: "4", fill: 0, text: "0 из 100" },
];

/**
 * «Успеваемость» (D-046): карточка предмета, полоса последних работ, полоса одной оценки, строка «Нового» и статус
 * задания словами источника. Значения — как у источника; цвет не оценивает результат.
 */
export function PerformanceSection() {
  return (
    <Section
      id="performance"
      title="Успеваемость"
      note="Карточка предмета — итог или накопленное, как дал источник, и полоса последних работ. Столбик — доля одной записи (valueNum / scaleMax), ничего не усредняется. Оценка без числа — нейтральный чип со словом источника."
    >
      <Grid>
        <Demo label="CourseGradeCard — накоплено, 10-балльная шкала, полоса работ" wide>
          <CourseGradeCard
            title="Математический анализ"
            headline={{ label: "Накоплено", value: "7,6", suffix: "из 10", period: "1 модуль", fill: 0.76 }}
            caption="Проверено: 4 работы"
            badge={<Badge tone="neutral">ВШЭ</Badge>}
            strip={TEN}
            onOpen={() => undefined}
          />
        </Demo>
        <Demo label="CourseGradeCard — итог «зачтено», работы с чипом без числа" wide>
          <CourseGradeCard
            title="Алгоритмы и структуры данных"
            headline={{ label: "Итог", value: "зачтено", suffix: "зачёт", period: "1 модуль 2026/27" }}
            caption="Проверено: 2 работы"
            strip={MIXED}
            onOpen={() => undefined}
          />
        </Demo>
        <Demo label="CourseGradeCard — 100-балльная БРС, «баллы за курс»" wide>
          <CourseGradeCard
            title="Продуктовый менеджмент"
            headline={{ label: "Накоплено", value: "64", suffix: "из 100", period: "Осень 2026", fill: 0.64 }}
            caption="Проверено: 4 работы"
            strip={HUNDRED}
            onOpen={() => undefined}
          />
        </Demo>
        <Demo label="CourseGradeCard — длинное название (две строки), только последняя оценка" wide>
          <CourseGradeCard
            title="Методы оптимизации и исследование операций в экономике и управлении проектами"
            headline={{ label: "Последняя оценка", value: "7", suffix: "из 10" }}
            caption="Проверено: 1 работа"
            strip={[{ id: "1", fill: 0.7, text: "7 из 10" }]}
            onOpen={() => undefined}
          />
        </Demo>
      </Grid>
      <Grid>
        <Demo label="ScoreStrip — список для скринридера («Коллоквиум: 6 из 10»)">
          <ScoreStrip items={TEN} label="Последние работы" />
          <ScoreStrip items={MIXED} label="Последние работы" />
        </Demo>
        <Demo label="ScoreBar — доля одной записи: 8 из 10, 64 из 100, 0" wide>
          <div className="flex flex-col gap-3">
            <ScoreBar fill={0.8} />
            <ScoreBar fill={0.64} />
            <ScoreBar fill={0} />
          </div>
        </Demo>
      </Grid>
      <Grid>
        <Demo label="UpdateRow — новая оценка и смена статуса задания" wide>
          <Card padding="sm">
            <ul>
              <li>
                <UpdateRow
                  kind="grade"
                  title="Контрольная №2"
                  meta="ВШЭ · Математический анализ · вчера"
                  value="8 из 10"
                  onOpen={() => undefined}
                />
              </li>
              <li>
                <UpdateRow
                  kind="deadline"
                  done
                  title="Домашнее задание №3: пределы"
                  status={
                    <Badge tone="success" icon="ok">
                      Решение прикреплено
                    </Badge>
                  }
                  meta="Математический анализ · 2 дня назад"
                  onOpen={() => undefined}
                />
              </li>
              <li>
                <UpdateRow
                  kind="deadline"
                  title="Лабораторная: алгоритмы сортировки"
                  status={<Badge tone="neutral">На доработке</Badge>}
                  meta="Алгоритмы и структуры данных · вчера"
                  onOpen={() => undefined}
                />
              </li>
            </ul>
          </Card>
        </Demo>
        <Demo label="DeadlineItem — статус источника, «отмечено вами», балл проверенной работы" wide>
          <Card padding="sm">
            <ul>
              <li>
                <DeadlineItem
                  title="Домашнее задание №3: пределы"
                  courseTitle="Математический анализ"
                  dueAt={NOW - 72 * 3_600_000}
                  urgency="overdue"
                  done
                  statusText="Решение прикреплено"
                  statusNote="по вашему правилу"
                  spaceId="hse"
                  nowMs={NOW}
                  onOpen={() => undefined}
                />
              </li>
              <li>
                <DeadlineItem
                  title="Тест по теории вероятностей"
                  courseTitle="Теория вероятностей"
                  dueAt={NOW - 144 * 3_600_000}
                  urgency="overdue"
                  done
                  statusText="Проверено"
                  score="10 из 10"
                  spaceId="hse"
                  nowMs={NOW}
                  onOpen={() => undefined}
                />
              </li>
              <li>
                <DeadlineItem
                  title="Конспект лекции 5"
                  courseTitle="Data Science"
                  dueAt={NOW - 30 * 3_600_000}
                  urgency="overdue"
                  done
                  statusNote="отмечено вами"
                  spaceId="cu"
                  nowMs={NOW}
                  onOpen={() => undefined}
                />
              </li>
            </ul>
          </Card>
        </Demo>
      </Grid>
    </Section>
  );
}
