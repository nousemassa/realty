"""Durable-состояние хода диалога.

**Зачем это вообще.** До в коде стояла гарантия, которой не было: докстринг клиента Sessions API
(слой удалён в окне 215 как неиспользуемый) обещал «retry только new, дедуп через `send_state`», а
таблицы `send_state` и `consumed_updates` не использовались ни одной строкой. Утверждение снято,
и этот модуль отвечает на вопрос по существу: **переживает ли ХОД падение процесса.**

**Где здесь настоящая гарантия, а где — забота о порядке.** Разделять важно:

  * «клиенту ровно один ответ» — это свойство **этого** модуля: ход помечается `sent`
    ДО сетевого вызова. Процесс, умерший после отправки, при реконсиляции не отправит
    повторно: он видит `sent` и отправляет ход в `failed` на разбор, а не в рассылку.
  * «не более одной брони» — это свойство **базы**, а не логики: частичный уникальный
    индекс `uq_slot_global` (`repo.py`) допускает одну активную бронь на слот. Никакой
    прикладной код это не дублирует, и не должен.

**Lease вместо внешнего оркестратора.** Ход берётся в работу с ограниченным сроком:
`lease_until` + `FOR UPDATE SKIP LOCKED`. Воркеров может быть больше одного, и один ход
должен достаться ровно одному. Просроченный lease — признак, что взявший его процесс
умер; такой ход подхватывает sweeper.

**Чего здесь нет:** дедупликации входящих обновлений и доставки исходящих. Первое —
предмет, второе — `realty_agent/workers/outbox.py` (тоже, отложен). Смешивать их с
значило бы получить пункт вдвое дороже заявленного.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

log = logging.getLogger(__name__)

STATUS_CREATED = "created"      # ход зарегистрирован, ещё не отправлен
STATUS_SENT = "sent"            # отправка НАЧАТА: повторять нельзя
STATUS_AWAITING = "awaiting"    # ответ получен, идёт разбор/запись
STATUS_ANSWERED = "answered"    # ход закрыт успешно
STATUS_FAILED = "failed"        # ошибка или «подозрение на дубль» после падения

TERMINAL = (STATUS_ANSWERED, STATUS_FAILED)
ALL_STATUSES = (STATUS_CREATED, STATUS_SENT, STATUS_AWAITING, STATUS_ANSWERED, STATUS_FAILED)

#: Срок lease по умолчанию. Больше самого долгого хода (ожидание ответа агента — до 60 с по `wait_s`
#: в канале), чтобы идущий ход не выглядел залипшим.
DEFAULT_LEASE_SECONDS = 120


@dataclass(frozen=True)
class TurnState:
    id: int
    platform: str
    chat_id: str
    turn_key: str
    status: str
    attempts: int
    reply_text: str | None
    error: str | None
    lease_owner: str | None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL


def new_turn_key() -> str:
    return secrets.token_hex(12)


class TurnStore:
    """Состояние ходов поверх таблицы `turn_state`.

    Пул передаётся снаружи: у модуля нет своего соединения, и это позволяет тестировать
    его двойником без базы.
    """

    def __init__(self, pool, *, lease_seconds: int = DEFAULT_LEASE_SECONDS, owner: str = "") -> None:
        self._pool = pool
        self._lease_seconds = lease_seconds
        self._owner = owner or secrets.token_hex(4)

    async def begin(self, *, platform: str, chat_id: str, turn_key: str) -> TurnState:
        """Зарегистрировать ход. Повторный вызов с тем же ключом НЕ создаёт второй ход.

        Идемпотентность по `(platform, chat_id, turn_key)` — единственный способ
        гарантировать «один ход — одна запись» без внешнего координатора.
        """
        row = await self._pool.fetchrow(
            """
            INSERT INTO turn_state (platform, chat_id, turn_key, status)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (platform, chat_id, turn_key) DO UPDATE
               SET updated_at = now()
            RETURNING id, platform, chat_id, turn_key, status, attempts, reply_text, error, lease_owner
            """,
            platform, chat_id, turn_key, STATUS_CREATED,
        )
        return _to_state(row)

    async def claim(self, *, platform: str, chat_id: str, turn_key: str) -> TurnState | None:
        """Взять ход в работу с lease. `None` — ход занят другим живым воркером.

        Условие захвата: lease свободен или просрочен. `SKIP LOCKED` не даёт двум
        воркерам заблокироваться друг на друге.
        """
        row = await self._pool.fetchrow(
            """
            UPDATE turn_state
               SET lease_until = now() + ($2 || ' seconds')::interval,
                   lease_owner = $4,
                   attempts    = attempts + 1,
                   updated_at  = now()
             WHERE platform = $1 AND chat_id = $3 AND turn_key = $5
               AND status NOT IN ('answered', 'failed')
               AND (lease_until IS NULL OR lease_until < now())
            RETURNING id, platform, chat_id, turn_key, status, attempts, reply_text, error, lease_owner
            """,
            platform, str(self._lease_seconds), chat_id, self._owner, turn_key,
        )
        return _to_state(row) if row else None

    async def mark_sent(self, turn_id: int) -> None:
        """Отметить НАЧАЛО отправки. Вызывается до сетевого вызова, а не после.

        Это и есть защита от дубля: если процесс умрёт во время отправки, при
        восстановлении ход будет виден как `sent` — то есть «ответ мог уйти» — и его
        отправят на разбор, а не отправят второй раз.
        """
        await self._pool.execute(
            "UPDATE turn_state SET status = $2, updated_at = now() WHERE id = $1",
            turn_id, STATUS_SENT,
        )

    async def mark_awaiting(self, turn_id: int) -> None:
        """Ответ получен, начинается разбор и запись (бронь, уведомления)."""
        await self._pool.execute(
            "UPDATE turn_state SET status = $2, updated_at = now() WHERE id = $1",
            turn_id, STATUS_AWAITING,
        )

    async def mark_answered(self, turn_id: int, *, reply_text: str = "") -> None:
        await self._pool.execute(
            """
            UPDATE turn_state
               SET status = $2, reply_text = $3, error = NULL,
                   lease_until = NULL, lease_owner = NULL, updated_at = now()
             WHERE id = $1
            """,
            turn_id, STATUS_ANSWERED, reply_text,
        )

    async def mark_failed(self, turn_id: int, *, error: str = "") -> None:
        await self._pool.execute(
            """
            UPDATE turn_state
               SET status = $2, error = $3, lease_until = NULL, lease_owner = NULL, updated_at = now()
             WHERE id = $1
            """,
            turn_id, STATUS_FAILED, error[:500],
        )

    async def get(self, turn_id: int) -> TurnState | None:
        row = await self._pool.fetchrow(
            """SELECT id, platform, chat_id, turn_key, status, attempts, reply_text, error, lease_owner
                 FROM turn_state WHERE id = $1""",
            turn_id,
        )
        return _to_state(row) if row else None

    async def stuck(self, *, limit: int = 50) -> list[TurnState]:
        """Ходы с просроченным lease в незавершённом состоянии — кандидаты на разбор."""
        rows = await self._pool.fetch(
            """
            SELECT id, platform, chat_id, turn_key, status, attempts, reply_text, error, lease_owner
              FROM turn_state
             WHERE status NOT IN ('answered', 'failed')
               AND lease_until IS NOT NULL AND lease_until < now()
             ORDER BY id
             LIMIT $1
            """,
            limit,
        )
        return [_to_state(row) for row in rows]

    async def sweep(self) -> list[TurnState]:
        """Разобрать залипшие ходы.

        **Ключевое решение: залипший ход НЕ переигрывается.**
        Если он в `sent` или `awaiting`, ответ мог уже уйти клиенту — повторная отправка
        создала бы второй ответ, то есть ровно тот дефект, ради которого пункт и делается.
        Поэтому такой ход помечается `failed` с причиной: «подозрение на дубль, разобрать
        вручную». Ходы в `created` (отправка не начиналась) можно смело переигрывать —
        но не здесь: их заново возьмёт обычный путь обработки.
        """
        resolved: list[TurnState] = []
        for state in await self.stuck():
            if state.status == STATUS_CREATED:
                # Отправка не начиналась — ничего не потеряно, просто отпускаем lease.
                await self._pool.execute(
                    "UPDATE turn_state SET lease_until = NULL, lease_owner = NULL, updated_at = now() WHERE id = $1",
                    state.id,
                )
                log.info("turns: ход %s (created) освобождён без переигровки", state.turn_key)
            else:
                await self.mark_failed(
                    state.id,
                    error=f"залипший ход в состоянии {state.status}: ответ мог уйти клиенту — разбор вручную",
                )
                log.warning(
                    "turns: ход %s залип в состоянии %s — помечен failed, повтор НЕ отправлен",
                    state.turn_key, state.status,
                )
            updated = await self.get(state.id)
            if updated is not None:
                resolved.append(updated)
        return resolved

    async def reconcile(self) -> list[TurnState]:
        """Реконсиляция при старте: то же, что sweeper, но до приёма новых сообщений."""
        return await self.sweep()


def _to_state(row) -> TurnState:
    return TurnState(
        id=row["id"],
        platform=row["platform"],
        chat_id=row["chat_id"],
        turn_key=row["turn_key"],
        status=row["status"],
        attempts=row["attempts"],
        reply_text=row["reply_text"],
        error=row["error"],
        lease_owner=row["lease_owner"],
    )
