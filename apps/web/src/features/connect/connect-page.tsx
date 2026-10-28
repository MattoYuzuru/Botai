import { AccentScope, Banner, Button, Card, Field, Icon, Input, useNow } from "@botai/design";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useRouter } from "@tanstack/react-router";
import { type FormEvent, useEffect, useState } from "react";
import { humanMessage } from "@/api/errors";
import { universitiesQuery } from "@/api/queries";
import type { University } from "@/api/types";
import { useActiveSpaceStore } from "@/app/active-space";
import { Screen } from "@/app/screen";
import { describeConnection } from "@/features/data/connection-status";
import { useReconnect } from "@/features/data/use-reconnect";
import { useSpaceDirectory } from "@/features/data/use-space-directory";
import { startConnection } from "./start-connection";
import { UniversityPicker } from "./university-picker";

/** Адрес кабинета необязателен; если он есть, это ссылка http(s). */
export function validateCabinetUrl(value: string): string | null {
  const text = value.trim();
  if (!text) return null;
  try {
    const url = new URL(text);
    return url.protocol === "https:" || url.protocol === "http:"
      ? null
      : "Нужна ссылка вида https://lk.example.ru";
  } catch {
    return "Нужна ссылка вида https://lk.example.ru";
  }
}

/** Выбор вуза и начало подключения. Дальше — `ProgressWall` с потоком задания. */
export function ConnectPage() {
  const router = useRouter();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [selected, setSelected] = useState<University | null>(null);
  const [own, setOwn] = useState(false);
  const [ownTitle, setOwnTitle] = useState("");
  const [url, setUrl] = useState("");
  const [errors, setErrors] = useState<{ choice?: string; title?: string; url?: string }>({});
  const [failure, setFailure] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [addingSource, setAddingSource] = useState(false);
  const directory = useSpaceDirectory();
  const activeId = useActiveSpaceStore((state) => state.id);
  const reconnect = useReconnect();
  const nowMs = useNow(30_000);

  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(query.trim()), 200);
    return () => window.clearTimeout(id);
  }, [query]);

  // Пока ищется новый запрос, прежний список остаётся на экране: без мигания скелетоном на каждую букву.
  const universities = useQuery({ ...universitiesQuery(debounced), placeholderData: keepPreviousData });
  const existing = directory.spaces.find((space) => selected && space.universityId === selected.id);
  const hue = selected?.accentHue ?? directory.byId.get(activeId)?.hue ?? directory.spaces[0]?.hue ?? null;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if ((existing && !addingSource) || busy) return;
    const found: typeof errors = {};
    if (!selected && !own) found.choice = "Выберите вуз из списка или укажите свой";
    if (own && !ownTitle.trim()) found.title = "Как называется вуз?";
    const urlError = validateCabinetUrl(url);
    if (urlError) found.url = urlError;
    setErrors(found);
    setFailure(null);
    if (Object.keys(found).length > 0) return;

    setBusy(true);
    try {
      const started = await startConnection(queryClient, { university: selected, title: ownTitle, url });
      await navigate({ to: "/connect/$jobId", params: { jobId: started.jobId } });
    } catch (error) {
      setFailure(error);
      setBusy(false);
    }
  };

  return (
    <AccentScope hue={hue} className="min-h-dvh bg-bg text-fg">
      <Screen title="Подключение вуза" leading="back" onBack={() => router.history.back()}>
        <div className="grid grid-cols-1 items-start gap-8 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
          <Card padding="lg" className="min-w-0">
            <form onSubmit={submit} className="flex flex-col gap-6" noValidate>
              <div className="flex flex-col gap-2">
                <span className="flex items-center gap-2 type-label text-accent-fg">
                  <Icon name="plus" size={16} /> Новый вуз
                </span>
                <h2 className="type-display">Где вы учитесь?</h2>
                <p className="type-body text-fg-muted">
                  Назовите вуз. Агент сам найдёт личный кабинет и будет показывать каждый шаг.
                </p>
              </div>

              {failure !== null && (
                <Banner tone="danger" icon="error" live>
                  {humanMessage(failure)}
                </Banner>
              )}

              <UniversityPicker
                query={query}
                onQuery={setQuery}
                items={universities.data ?? []}
                selected={selected}
                onSelect={(value) => {
                  setSelected(value);
                  setOwn(false);
                  setAddingSource(false);
                }}
                own={own}
                onOwn={() => {
                  setSelected(null);
                  setOwn(true);
                  setOwnTitle(query.trim());
                  setAddingSource(false);
                }}
                busy={busy}
                loading={universities.isFetching || query.trim() !== debounced}
                error={errors.choice}
              />

              {universities.isError && (
                <p role="status" className="type-caption text-fg-muted">
                  Список вузов недоступен. Вы можете указать название сами.
                </p>
              )}

              {selected?.thirdPartyWarning && (
                <Banner tone="warning" icon="warning">
                  {selected.thirdPartyWarning}
                </Banner>
              )}

              {own && (
                <Field label="Название вуза" error={errors.title}>
                  <Input
                    value={ownTitle}
                    onChange={(event) => setOwnTitle(event.target.value)}
                    autoComplete="organization"
                    disabled={busy}
                  />
                </Field>
              )}

              {existing && !addingSource ? (
                <Banner tone="info" icon="university">
                  <p>
                    Этот вуз уже добавлен. Войти снова или изменить подключение можно в разделе «Ваши
                    подключения».
                  </p>
                  <Button
                    variant="ghost"
                    className="h-auto min-h-11 max-w-full whitespace-normal"
                    onClick={() => setAddingSource(true)}
                  >
                    Добавить другой источник
                  </Button>
                </Banner>
              ) : (
                <Field
                  label="Адрес личного кабинета"
                  optional
                  hint="Откуда вы обычно заходите. Если не знаете, агент найдёт сам."
                  error={errors.url}
                >
                  <Input
                    type="url"
                    inputMode="url"
                    value={url}
                    onChange={(event) => setUrl(event.target.value)}
                    placeholder="https://"
                    autoCapitalize="off"
                    autoCorrect="off"
                    spellCheck={false}
                    disabled={busy}
                  />
                </Field>
              )}

              <p className="flex items-start gap-2 type-caption text-fg-muted">
                <Icon name="encrypted" size={16} />
                Логин и пароль вы введёте в защищённых полях BotAI; пароль хранится зашифрованным, и его можно
                удалить в любой момент. Ввод при входе проходит через сервер BotAI. Капчу и коды подтверждения
                вы проходите сами.
              </p>

              <div className="flex flex-col gap-2 xs:flex-row">
                <Button
                  type="submit"
                  variant="primary"
                  loading={busy}
                  disabled={Boolean(existing) && !addingSource}
                  className="h-auto min-h-11 whitespace-normal xs:min-w-48"
                >
                  {existing && addingSource ? "Подключить источник" : "Начать подключение"}
                </Button>
                <Button variant="ghost" asChild className="h-auto min-h-11 whitespace-normal">
                  <Link to="/welcome">Как это работает</Link>
                </Button>
              </div>
            </form>
          </Card>

          <aside className="flex min-w-0 flex-col gap-6">
            <section aria-labelledby="your-connections" className="flex flex-col gap-3">
              <h2 id="your-connections" className="type-heading">
                Ваши подключения
              </h2>
              <p className="type-ui text-fg-muted">
                Уже добавили вуз? Здесь можно войти снова или открыть настройки его источников.
              </p>
              {directory.loaded && directory.none && (
                <p className="rounded-lg bg-accent-subtle p-4 type-ui text-accent-fg">
                  Пока нет подключённых вузов. Начните с названия вуза.
                </p>
              )}
              {directory.spaces.map((space) => (
                <Card key={space.id} padding="md" className="flex flex-col gap-3">
                  <h3 className="flex items-center gap-2 type-subheading">
                    <Icon name="university" size={20} />
                    {space.title}
                  </h3>
                  {space.connections
                    .filter((connection) => connection.status !== "disabled")
                    .map((connection) => {
                      const view = describeConnection(connection, nowMs, { shortTitle: space.shortTitle });
                      return (
                        <div
                          key={connection.id}
                          className="flex flex-wrap items-center justify-between gap-2"
                        >
                          <div className="min-w-0">
                            <p className="type-ui">{connection.title}</p>
                            <p className="type-caption text-fg-muted">{view.label}</p>
                          </div>
                          <Button
                            variant="secondary"
                            loading={
                              reconnect.isPending && reconnect.variables?.connectionId === connection.id
                            }
                            disabled={busy || reconnect.isPending}
                            onClick={() =>
                              reconnect.mutate({
                                connectionId: connection.id,
                                spaceId: space.id,
                                spaceTitle: space.shortTitle,
                              })
                            }
                          >
                            Войти снова
                          </Button>
                        </div>
                      );
                    })}
                  <Button
                    variant="ghost"
                    asChild
                    className="h-auto min-h-11 max-w-full self-start whitespace-normal"
                  >
                    <Link to="/study/$kind" params={{ kind: "connections" }} search={{ space: space.id }}>
                      Настроить источники
                      <Icon name="chevron-right" size={16} />
                    </Link>
                  </Button>
                </Card>
              ))}
            </section>
            <div className="flex flex-col gap-3 border-t border-border pt-6">
              <h2 className="type-heading">Что будет дальше</h2>
              <ol className="flex list-decimal flex-col gap-3 pl-5 type-ui text-fg-muted">
                <li>Агент найдёт сайт и предложит способ входа.</li>
                <li>Вы введёте данные и пройдёте проверку вуза.</li>
                <li>Проверите найденное расписание и источники.</li>
              </ol>
            </div>
          </aside>
        </div>
      </Screen>
    </AccentScope>
  );
}
