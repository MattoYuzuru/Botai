import { Badge, Banner, Button, Card, formatDuration, IconButton } from "@botai/design";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, call } from "@/api/client";
import type { Login } from "@/features/agent-blocks/types";
import {
  type ClientMessage,
  keyMessage,
  type NeedsKind,
  type ServerMessage,
  toRemotePoint,
} from "./protocol";
import { RemoteSession, type SessionStatus, type SocketLike } from "./remote-session";

const STATUS_TEXT: Record<SessionStatus, string> = {
  connecting: "Открываю вход",
  live: "Вход идёт на сайте вуза. Нажмите на экран, чтобы ввести данные.",
  reconnecting: "Связь прервалась, подключаюсь снова",
  ended: "Экран закрыт",
};

const NEEDS_TEXT: Record<NeedsKind, string> = {
  captcha: "Нужна проверка «я не робот». Пройдите её сами: обходить такие проверки мы не будем.",
  mfa: "Вуз просит код подтверждения. Введите его прямо на экране.",
  choice: "Вуз просит выбрать вариант. Выберите его на экране.",
  input: "Вуз просит ввести данные. Введите их на экране.",
};

/** Рисует кадр на canvas и забывает его: кадры не хранятся ни в состоянии, ни на диске. */
function paintFrame(canvas: HTMLCanvasElement | null, message: Extract<ServerMessage, { t: "frame" }>) {
  const context = canvas?.getContext?.("2d");
  if (!canvas || !context) return;
  const image = new Image();
  image.onload = () => {
    if (canvas.width !== message.w || canvas.height !== message.h) {
      canvas.width = message.w;
      canvas.height = message.h;
    }
    context.drawImage(image, 0, 0, message.w, message.h);
  };
  image.src = `data:${message.mime};base64,${message.data}`;
}

export interface RemoteScreenProps {
  block: Login;
  /** «Сейчас» для оставшегося времени; секунды не объявляются скринридером. */
  nowMs: number;
  createSocket?: (url: string) => SocketLike;
  /** Паузы переподключения (в тестах — нули). */
  retryDelays?: readonly number[];
}

/**
 * Запасной экран входа (`login`): кадры удалённого браузера во всю ширину без прокрутки, касание и клавиатура,
 * статус соединения. Основной путь — зеркало формы (`SecretField`); сюда попадают капча и нестандартные шаги.
 * Вход делает человек; капча и MFA — «нужен человек», обходить их мы не будем. Кадры не записываются.
 */
export function RemoteScreen({ block, nowMs, createSocket, retryDelays }: RemoteScreenProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const keyboardRef = useRef<HTMLTextAreaElement>(null);
  const sessionRef = useRef<RemoteSession | null>(null);
  const frameSize = useRef({ w: 390, h: 640 });
  const pointer = useRef<{
    x: number;
    y: number;
    lastY: number;
    scrolled: boolean;
    id: number;
    touch: boolean;
  } | null>(null);
  const lastMove = useRef(0);
  /** Последний жест был прокруткой: клавиатуру после неё не открываем. */
  const wasScroll = useRef(false);

  const [status, setStatus] = useState<SessionStatus>("connecting");
  const [needs, setNeeds] = useState<{ what: NeedsKind; hint: string } | null>(null);
  const [page, setPage] = useState<{ host: string; loading: boolean } | null>(null);
  const [result, setResult] = useState<"success" | { message: string } | null>(null);
  const [finishing, setFinishing] = useState(false);

  const send = useCallback((message: ClientMessage) => sessionRef.current?.send(message) ?? false, []);

  useEffect(() => {
    const session = new RemoteSession({
      origin: window.location.origin,
      first: { wsUrl: block.wsUrl, ticket: block.ticket },
      renew: async () => {
        const next = await call(
          api.POST("/api/v1/connections/{connection_id}/login-session", {
            params: { path: { connection_id: block.connectionId } },
          }),
        );
        return { wsUrl: next.wsUrl, ticket: next.ticket };
      },
      createSocket,
      retryDelays,
      onStatus: (next) => {
        setStatus(next);
        if (next === "live") {
          const width = Math.round(wrapRef.current?.clientWidth || 390);
          const height = Math.round(Math.min(Math.max(window.innerHeight * 0.6, 420), 760));
          session.send({ t: "resize", w: width, h: height });
        }
      },
      onMessage: (message) => {
        switch (message.t) {
          case "frame":
            frameSize.current = { w: message.w, h: message.h };
            paintFrame(canvasRef.current, message);
            session.send({ t: "ack", seq: message.seq });
            break;
          case "state": {
            let host = message.url;
            try {
              host = new URL(message.url).host;
            } catch {
              /* оставляем как есть */
            }
            setPage({ host, loading: message.loading });
            break;
          }
          case "needs":
            setNeeds({ what: message.what, hint: message.hint });
            break;
          case "success":
            setResult("success");
            break;
          case "expired":
            setResult({ message: "Время входа истекло, экран закрыт. Начните вход заново." });
            break;
          case "error":
            setResult({ message: message.detail || "Запасной экран не открылся. Начните вход заново." });
            break;
        }
      },
    });
    sessionRef.current = session;
    session.start();
    return () => {
      session.close();
      sessionRef.current = null;
    };
  }, [block.wsUrl, block.ticket, block.connectionId, createSocket, retryDelays]);

  const point = (event: { clientX: number; clientY: number }) => {
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return toRemotePoint({ x: event.clientX, y: event.clientY }, rect, frameSize.current);
  };

  const onPointerDown = (event: React.PointerEvent<HTMLCanvasElement>) => {
    event.currentTarget.setPointerCapture?.(event.pointerId);
    const touch = event.pointerType !== "mouse";
    pointer.current = {
      x: event.clientX,
      y: event.clientY,
      lastY: event.clientY,
      scrolled: false,
      id: event.pointerId,
      touch,
    };
    if (!touch)
      send({ t: "mouse", kind: "down", ...point(event), button: event.button === 2 ? "right" : "left" });
    wasScroll.current = false;
  };

  const onPointerMove = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const current = pointer.current;
    if (!current) return;
    if (current.touch) {
      // Касание: короткое — клик, длинное движение — прокрутка удалённой страницы (свайп дублирует колесо).
      if (!current.scrolled && Math.hypot(event.clientX - current.x, event.clientY - current.y) > 8)
        current.scrolled = true;
      if (current.scrolled) {
        send({ t: "scroll", ...point(event), dx: 0, dy: Math.round(current.lastY - event.clientY) });
        current.lastY = event.clientY;
      }
      return;
    }
    const time = event.timeStamp;
    if (time - lastMove.current < 25) return;
    lastMove.current = time;
    send({ t: "mouse", kind: "move", ...point(event), button: "left" });
  };

  const onPointerUp = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const current = pointer.current;
    pointer.current = null;
    if (!current) return;
    wasScroll.current = current.scrolled;
    if (current.touch) {
      if (!current.scrolled)
        send({
          t: "mouse",
          kind: "click",
          ...point({ clientX: current.x, clientY: current.y }),
          button: "left",
        });
    } else {
      send({ t: "mouse", kind: "up", ...point(event), button: event.button === 2 ? "right" : "left" });
    }
  };

  // Клавиатура открывается из `click`: iOS Safari показывает её только для фокуса из жеста пользователя.
  const onClick = () => {
    if (!wasScroll.current) keyboardRef.current?.focus({ preventScroll: true });
  };

  const onWheel = (event: React.WheelEvent<HTMLCanvasElement>) => {
    send({ t: "scroll", ...point(event), dx: Math.round(event.deltaX), dy: Math.round(event.deltaY) });
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    const message = keyMessage(event);
    if (!message) return;
    event.preventDefault();
    send(message);
  };

  // Экранные клавиатуры шлют текст через beforeinput, а не через keydown с настоящей клавишей.
  const onBeforeInput = (event: React.FormEvent<HTMLTextAreaElement>) => {
    const native = event.nativeEvent as InputEvent;
    if (native.inputType === "insertText" && native.data) {
      event.preventDefault();
      send({ t: "key", kind: "char", text: native.data });
    } else if (native.inputType === "deleteContentBackward") {
      event.preventDefault();
      send({ t: "key", kind: "down", code: "Backspace" });
    }
  };

  const finish = () => {
    setFinishing(true);
    send({ t: "done" });
  };

  const leftMs = Date.parse(block.expiresAt) - nowMs;
  const ended = result !== null || status === "ended";

  return (
    <Card className="flex flex-col gap-3" data-remote-screen={status} aria-label="Запасной экран входа">
      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="type-subheading text-fg">Вход на сайте вуза</h3>
          <Badge tone={status === "live" ? "success" : status === "reconnecting" ? "warning" : "neutral"} dot>
            {STATUS_TEXT[status].split(".")[0]}
          </Badge>
        </div>
        <p className="type-caption text-fg-muted">
          Экран и ввод проходят через сервер BotAI. Кадры не записываются.
          {leftMs > 0 && !ended ? ` Экран закроется через ${formatDuration(leftMs)}.` : ""}
        </p>
        {block.hint && <p className="type-ui text-fg">{block.hint}</p>}
      </div>

      {needs && !ended && (
        <Banner tone="warning" icon="help" live>
          {NEEDS_TEXT[needs.what]}
          {needs.hint ? ` ${needs.hint}` : ""}
        </Banner>
      )}

      <div ref={wrapRef} className="overflow-hidden rounded-lg border border-border bg-bg-subtle">
        <canvas
          ref={canvasRef}
          width={390}
          height={640}
          aria-label={page ? `Страница вуза ${page.host}` : "Страница вуза"}
          role="img"
          data-testid="remote-canvas"
          className="block h-auto w-full touch-none select-none"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onClick={onClick}
          // Нажатие мыши по канвасу не должно снимать фокус со скрытого поля ввода.
          onMouseDown={(event) => event.preventDefault()}
          onPointerCancel={() => {
            pointer.current = null;
          }}
          onWheel={onWheel}
        />
        {/* design-lint-ignore: скрытое поле, через которое экранная клавиатура вводит символы в удалённую страницу */}
        <textarea
          ref={keyboardRef}
          aria-label="Ввод с клавиатуры на странице вуза"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          tabIndex={-1}
          className="pointer-events-none absolute size-px opacity-0 type-body"
          onKeyDown={onKeyDown}
          onBeforeInput={onBeforeInput}
        />
      </div>

      {status === "live" && !ended && (
        <p className="type-caption text-fg-muted">
          {page?.loading ? "Страница загружается. " : ""}Нажмите на экран, чтобы ввести данные.
        </p>
      )}

      {result === "success" && (
        <Banner tone="success" icon="ok" live>
          Вход выполнен. Продолжаю подключение.
        </Banner>
      )}
      {result && result !== "success" && (
        <Banner tone="warning" icon="warning" live>
          {result.message}
        </Banner>
      )}
      {status === "reconnecting" && (
        <Banner tone="info" icon="sync" live>
          {STATUS_TEXT.reconnecting}. Ваш вход на сайте вуза не потерян.
        </Banner>
      )}

      {!ended && (
        <div className="flex flex-wrap items-center gap-2">
          <IconButton
            icon="keyboard"
            label="Показать клавиатуру"
            variant="secondary"
            onClick={() => keyboardRef.current?.focus()}
          />
          <Button
            variant="primary"
            loading={finishing}
            disabled={status !== "live"}
            onClick={finish}
            className="flex-1 xs:flex-none"
          >
            Я вошёл
          </Button>
        </div>
      )}
    </Card>
  );
}
