"""Трассировка и метрики — без сети и без коллектора.

Проверяется ровно то, что легко сломать и трудно заметить: **деградация** (нет адреса или нет
пакетов — сервис работает как раньше), **ПДн** (в атрибуты спана не уходит `chat_id` как есть) и
**честность метрик** (недоступная база — это `bridge_db_up 0`, а не нулевые расходы).
"""
from __future__ import annotations

import logging

import pytest

from agent_core import log_trace, metrics, telemetry

telemetry.register_roles("dialog", "visit")


class _Settings:
    """Минимум настроек: то, что читают telemetry и метрики."""

    def __init__(self, **overrides) -> None:
        self.otel_endpoint = ""
        self.otel_api_key = ""
        self.otel_workflow_id = "wf-1"
        self.otel_workflow_slug = "realtor-bot"
        self.platform_api_key = "key"
        for name, value in overrides.items():
            setattr(self, name, value)


def test_span_attributes_hash_the_client_id():
    """В атрибуты идёт хеш, а не идентификатор клиента: трейс не должен нести ПДн."""
    attributes = telemetry.span_attributes(workflow_id="wf-1", workflow_slug="realtor-bot",
                                           chat_id="8959870386", step="listing")

    assert attributes["ag.references.workflow.id"] == "wf-1"
    assert attributes["ag.references.workflow.slug"] == "realtor-bot"
    assert attributes["ag.data.step"] == "listing"
    assert "8959870386" not in str(attributes), "идентификатор клиента в открытом виде не уходит"
    assert attributes["ag.session.id"]


def test_span_without_tracer_is_a_no_op():
    """Нет трейсера — спаны не создаются, и вызывающий код об этом не думает."""
    with telemetry.span(None, "turn", {"a": 1}) as span:
        assert span is None


def test_trace_id_is_empty_outside_a_span():
    """Вне спана идентификатор пуст, а не ноль: «трейса нет» и «трейс нулевой» — разные состояния."""
    assert telemetry.trace_id() == ""


def test_setup_telemetry_without_endpoint_does_not_require_network():
    """Пустой адрес — штатное состояние: трейсер есть, экспортёра нет, сеть не трогается."""
    tracer = telemetry.setup_telemetry(_Settings(), agent="dialog")

    assert tracer is not None
    with telemetry.span(tracer, "turn") as span:
        # Провайдера нет ⇒ спан не записывается, контекст недействителен, исключений нет.
        assert span is None or not span.get_span_context().is_valid


def test_setup_telemetry_is_idempotent():
    """Повторная сборка трейсера не пересобирает провайдера — иначе досылка спанов уходит не туда.

    `set_tracer_provider` разрешён в процессе ОДИН раз: второй вызов создаёт провайдера, который
    нигде не регистрируется (`Overriding of current TracerProvider is not allowed`), и
    `flush_telemetry` досылает именно его — то есть не тот, куда легли спаны (их спасал бы только
    таймер батча). Инвариант: один адрес ⇒ один и тот же трейсер, сколько бы раз сборку ни звали.
    """
    settings = _Settings()
    first = telemetry.setup_telemetry(settings, agent="dialog")
    second = telemetry.setup_telemetry(settings, agent="dialog")

    assert first is second, "трейсер пересобирается на каждый вызов — досылка спанов осиротеет"


def test_log_filter_and_formatter_add_trace_id_only_when_present():
    record = logging.LogRecord("bridge.tg", logging.INFO, __file__, 1, "сообщение", None, None)

    assert log_trace.TraceContext().filter(record) is True
    assert record.trace_id == ""

    formatter = log_trace.TraceFormatter("%(message)s")
    assert formatter.format(record) == "сообщение", "без трейса строка выглядит как раньше"

    record.trace_id = "abc123"
    assert formatter.format(record).endswith("trace_id=abc123")


def test_metrics_render_shows_process_and_hides_money_when_db_is_down():
    """Недоступная база — `bridge_db_up 0` и НЕТ бизнес-чисел: ноль читался бы как «всё бесплатно»."""
    down = metrics.render(prefix="bridge", started_at=0, telegram_running=True, stats={"updates_processed": 5}, day=None)

    assert "bridge_up 1" in down
    assert "bridge_db_up 0" in down
    assert "bridge_updates_processed 5" in down
    assert "bridge_day_cost_usd" not in down


def test_metrics_render_includes_day_numbers():
    day = {"turns": 3, "with_agent": 2, "local_closed": 1, "cost_usd": 0.39, "bookings": 1}
    text = metrics.render(prefix="bridge", started_at=0, telegram_running=False, stats={}, day=day)

    assert "bridge_db_up 1" in text
    assert "bridge_day_cost_usd 0.39" in text
    assert "bridge_day_local_closes 1" in text
    assert "bridge_telegram_up 0" in text


def test_metrics_render_shows_outbox_numbers():
    """Числа очереди видны в `/metrics`: лаг — пик за день, остальные — счётчики."""
    text = metrics.render(
        prefix="bridge",
        started_at=0, telegram_running=False, stats={}, day={},
        outbox={"outbox_sent": 12.0, "outbox_failed": 2.0, "outbox_retries": 3.0, "outbox_lag_s": 42.0},
    )

    assert "bridge_outbox_sent 12.0" in text
    assert "bridge_outbox_failed 2.0" in text
    assert "bridge_outbox_retries 3.0" in text
    assert "bridge_outbox_lag_seconds_max 42.0" in text


def test_metrics_render_hides_outbox_when_unknown():
    """Неизвестная очередь — числа НЕ печатаем: ноль читался бы как «всё доставлено».

    Тот же принцип, что у недоступной базы: «нет данных» и «ноль» — разные состояния.
    """
    text = metrics.render(prefix="bridge", started_at=0, telegram_running=False, stats={}, day={})

    assert "bridge_outbox_sent" not in text
    assert "bridge_outbox_lag_seconds_max" not in text


@pytest.mark.asyncio
async def test_metrics_route_survives_db_failure():
    """Ручка метрик не падает при недоступной базе: мониторинг не должен терять признаки жизни."""
    import realty_agent.bridge_main as main_module

    class _BrokenDb:
        async def turn_report(self, *, days: int = 1) -> dict:
            raise RuntimeError("база недоступна (тест)")

    saved = getattr(main_module.app.state, "db", None)
    main_module.app.state.db = _BrokenDb()
    try:
        response = await main_module.prometheus_metrics()
    finally:
        main_module.app.state.db = saved

    body = response.body.decode()
    assert "bridge_up 1" in body and "bridge_db_up 0" in body
