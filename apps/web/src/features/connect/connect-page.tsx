import { Banner, Button, Field, Icon, Input, RadioGroup, SkeletonText } from "@botai/design";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useRouter } from "@tanstack/react-router";
import { type FormEvent, useEffect, useState } from "react";
import { humanMessage } from "@/api/errors";
import { universitiesQuery } from "@/api/queries";
import { Screen } from "@/app/screen";
import { startConnection } from "./start-connection";

const OWN = "\u0000own";

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
  const [choice, setChoice] = useState<string>("");
  const [ownTitle, setOwnTitle] = useState("");
  const [url, setUrl] = useState("");
  const [errors, setErrors] = useState<{ choice?: string; title?: string; url?: string }>({});
  const [failure, setFailure] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(query.trim()), 200);
    return () => window.clearTimeout(id);
  }, [query]);

  // Пока ищется новый запрос, прежний список остаётся на экране: без мигания скелетоном на каждую букву.
  const universities = useQuery({ ...universitiesQuery(debounced), placeholderData: keepPreviousData });
  const selected = universities.data?.find((university) => university.id === choice) ?? null;
  const own = choice === OWN;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
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
    <Screen title="Подключение вуза" leading="back" onBack={() => router.history.back()}>
      <form onSubmit={submit} className="flex max-w-prose flex-col gap-6" noValidate>
        <p className="type-body text-fg-muted">
          Назовите вуз. Агент сам найдёт личный кабинет и будет показывать каждый шаг. Логин и пароль вы
          введёте в полях BotAI; пароль хранится зашифрованным, и его можно удалить в любой момент.
        </p>

        {failure !== null && (
          <Banner tone="danger" icon="error" live>
            {humanMessage(failure)}
          </Banner>
        )}

        <Field label="Вуз" hint="Если вашего вуза нет в списке, выберите «Другой вуз»." error={errors.choice}>
          <Input
            leadingIcon="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Название или сокращение"
            autoComplete="off"
            enterKeyHint="search"
          />
        </Field>

        {universities.isPending ? (
          <SkeletonText lines={3} label="Загружаю список вузов" />
        ) : (
          <RadioGroup
            aria-label="Вузы"
            value={choice}
            onValueChange={setChoice}
            disabled={busy}
            items={[
              ...(universities.data ?? []).map((university) => ({
                value: university.id,
                label: university.title,
                description: university.shortTitle,
              })),
              { value: OWN, label: "Другой вуз", description: "Агент выучит его с нуля" },
            ]}
          />
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

        <p className="flex items-start gap-2 type-caption text-fg-muted">
          <Icon name="encrypted" size={16} />
          Ввод при входе проходит через сервер BotAI. Капчу и коды подтверждения вы проходите сами.
        </p>

        <div className="flex flex-col gap-2 xs:flex-row">
          <Button type="submit" variant="primary" loading={busy} className="xs:min-w-48">
            Начать подключение
          </Button>
          <Button variant="ghost" asChild>
            <Link to="/welcome">Как это работает</Link>
          </Button>
        </div>
      </form>
    </Screen>
  );
}
