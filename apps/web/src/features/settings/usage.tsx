import {
  Badge,
  Banner,
  Card,
  countRu,
  formatDay,
  formatTime,
  ListRowBody,
  listRowClass,
  SkeletonText,
  useNow,
} from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { humanMessage, isApiError } from "@/api/errors";
import { meQuery } from "@/api/queries";
import {
  limitKnown,
  type PoolUsage,
  poolUsageQuery,
  quotaDayKey,
  sumPool,
  sumUsageDays,
  todayUsage,
  type Usage,
  type UsageDay,
  usageQuery,
} from "@/api/usage";
import { formatTokens, formatUsd } from "@/lib/money";

const TIME_ZONE = "Europe/Moscow";

/**
 * «Расход и лимиты» в настройках (settings, usage/router.py): сегодня и последние семь дней —
 * токены и стоимость по настоящим числам сервера; владельцу — расход общего ключа по людям.
 * Проценты и прогнозы клиент не считает: только «N из M токенов» и суммы.
 */
export function UsageSection() {
  const nowMs = useNow(60_000);
  const usage = useQuery(usageQuery());
  const me = useQuery(meQuery);
  const owner = me.data?.user.isOwner === true;
  const pool = useQuery(poolUsageQuery(owner));

  return (
    <section aria-labelledby="usage-title" className="flex flex-col gap-3">
      <h2 id="usage-title" className="type-heading text-fg">
        Расход и лимиты
      </h2>
      <UsageCard usage={usage.data} loading={usage.isPending} error={usage.error} nowMs={nowMs} />
      {owner && <PoolCard pool={pool.data} loading={pool.isPending} error={pool.error} nowMs={nowMs} />}
    </section>
  );
}

function UsageCard({
  usage,
  loading,
  error,
  nowMs,
}: {
  usage: Usage | undefined;
  loading: boolean;
  error: unknown;
  nowMs: number;
}) {
  const days = usage?.lastDays ?? [];
  const totals = sumUsageDays(days);
  const today = usage ? todayUsage(days, nowMs, TIME_ZONE) : null;
  const limit = usage ? limitKnown(usage.tokensLimit) : false;
  const resetMs = usage ? Date.parse(usage.resetsAt) : Number.NaN;
  return (
    <Card padding="sm" data-part="usage-card">
      {loading ? (
        <div className="p-3">
          <SkeletonText lines={4} label="Загрузка расхода" />
        </div>
      ) : error ? (
        <div className="p-3">
          <Banner tone="warning" icon="offline" live>
            Расход не загрузился: {humanMessage(error)} Обновление идёт в фоне.
          </Banner>
        </div>
      ) : usage ? (
        <div className="flex flex-col gap-4 p-3">
          <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <p className="type-subheading text-fg">Мои токены</p>
            <Badge tone="neutral">сегодня</Badge>
          </div>
          <p className="type-ui text-fg" data-numeric="">
            {limit
              ? `${formatTokens(usage.tokensToday)} из ${formatTokens(usage.tokensLimit)}`
              : formatTokens(usage.tokensToday)}
          </p>
          {Number.isFinite(resetMs) && (
            <p className="type-caption text-fg-muted">
              Лимит сбросится {formatDay(resetMs, nowMs, TIME_ZONE)}, {formatTime(resetMs, TIME_ZONE)}
            </p>
          )}
          {today ? (
            <p className="type-caption text-fg-muted">Стоимость сегодня: {formatUsd(today.costUsd)}</p>
          ) : (
            usage.tokensToday > 0 && (
              <p className="type-caption text-fg-subtle">
                Разбивка по дням ещё не пришла — видно только сегодняшние токены.
              </p>
            )
          )}
          <DaysList days={days} totals={totals} nowMs={nowMs} />
        </div>
      ) : (
        <p className="p-3 type-ui text-fg-muted">Данных о расходе пока нет.</p>
      )}
    </Card>
  );
}

function DaysList({
  days,
  totals,
  nowMs,
}: {
  days: readonly UsageDay[];
  totals: ReturnType<typeof sumUsageDays>;
  nowMs: number;
}) {
  if (days.length === 0) {
    return (
      <p className="type-ui text-fg-muted" data-part="usage-empty">
        За последние дни расхода не было — здесь появятся токены, когда агент ответит.
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <p className="type-subheading text-fg">Последние дни</p>
        <span className="type-caption text-fg-muted">{countRu(totals.days, ["день", "дня", "дней"])}</span>
      </div>
      <ul aria-label="Расход по дням">
        {days.map((day) => {
          const at = Date.parse(`${day.day}T12:00:00Z`);
          const key = quotaDayKey(nowMs, TIME_ZONE) === day.day;
          return (
            <li key={day.day} className={listRowClass}>
              <ListRowBody
                icon="today"
                title={Number.isFinite(at) ? formatDay(at, nowMs, TIME_ZONE) : day.day}
                meta={
                  <>
                    {key && <Badge tone="success">сегодня</Badge>}
                    <span data-numeric="">{formatTokens(day.tokens)}</span>
                    <span data-numeric="">{formatUsd(day.costUsd)}</span>
                  </>
                }
              />
            </li>
          );
        })}
      </ul>
      <p className="type-caption text-fg-subtle" data-part="usage-total">
        За {countRu(totals.days, ["день", "дня", "дней"])}: {formatTokens(totals.tokens)} ·{" "}
        {formatUsd(totals.costUsd)}
      </p>
    </div>
  );
}

function PoolCard({
  pool,
  loading,
  error,
  nowMs,
}: {
  pool: PoolUsage | undefined;
  loading: boolean;
  error: unknown;
  nowMs: number;
}) {
  const users = pool?.users ?? [];
  const totals = sumPool(users);
  const forbidden = isApiError(error) && error.status === 403;
  const resetMs = pool ? Date.parse(pool.resetsAt) : Number.NaN;
  return (
    <Card padding="sm" data-part="usage-pool">
      <div className="flex flex-col gap-4 p-3">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          <p className="type-subheading text-fg">Общий ключ</p>
          {pool && (
            <span className="type-caption text-fg-muted">
              {limitKnown(pool.tokensLimit)
                ? `лимит ${formatTokens(pool.tokensLimit)} в день`
                : "лимит не задан"}
            </span>
          )}
        </div>
        {loading ? (
          <SkeletonText lines={3} label="Загрузка расхода общего ключа" />
        ) : error ? (
          <Banner tone={forbidden ? "info" : "warning"} icon="offline" live>
            {forbidden
              ? "Расход общего ключа видит только владелец."
              : `Расход общего ключа не загрузился: ${humanMessage(error)} Обновление идёт в фоне.`}
          </Banner>
        ) : users.length === 0 ? (
          <p className="type-ui text-fg-muted">По общему ключу пока никто не расходовал токены.</p>
        ) : (
          <>
            <ul aria-label="Расход по людям">
              {users.map((user) => (
                <li key={user.userId} className={listRowClass}>
                  <ListRowBody
                    icon="user"
                    title={user.displayName}
                    meta={
                      <>
                        <span className="break-all">{user.email}</span>
                        <span data-numeric="">
                          сегодня: {formatTokens(user.tokensToday)} · {formatUsd(user.costUsdToday)}
                        </span>
                        <span data-numeric="">
                          за неделю: {formatTokens(user.tokensWeek)} · {formatUsd(user.costUsdWeek)}
                        </span>
                      </>
                    }
                  />
                </li>
              ))}
            </ul>
            <p className="type-caption text-fg-subtle" data-part="usage-pool-total">
              Всего сегодня: {formatTokens(totals.tokensToday)} · {formatUsd(totals.costUsdToday)}
            </p>
          </>
        )}
        {pool && Number.isFinite(resetMs) && (
          <p className="type-caption text-fg-subtle">
            Лимит сбросится {formatDay(resetMs, nowMs, TIME_ZONE)}, {formatTime(resetMs, TIME_ZONE)}
          </p>
        )}
      </div>
    </Card>
  );
}
