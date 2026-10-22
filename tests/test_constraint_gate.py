"""Гейт диалога «только условие?» — без сети и без моделей.

Решатель подставляется двойником, поэтому здесь проверяется ровно то, что принадлежит нам:
чтение уровня, полярность ответа и деградация в сторону ПРЕЖНЕГО поведения.
"""
from __future__ import annotations

import pytest

from agent_core.decisions.client import Decision, SolverBadAnswer, SolverUnavailable
from agent_core.decisions.ladder import LEVEL_CLOUD, LEVEL_SOLVER
from realty_agent.local_close import gate as constraint_gate


def answering(answer: str, probability: float):
    """Двойник решателя: отвечает заданным вариантом с заданной уверенностью."""

    async def ask(state: str, question: str, options: dict) -> Decision:
        assert question == constraint_gate.QUESTION, "вопрос гейта один на живой путь и на замер"
        assert set(options) == set(constraint_gate.OPTIONS), "варианты те же, что в контракте гейта"
        assert state, "текст клиента обязан уходить решателю (иначе судили бы не то сообщение)"
        return Decision(answer=answer, top_probability=probability, probabilities={answer: probability})

    return ask


async def broken(state: str, question: str, options: dict) -> Decision:
    raise SolverUnavailable("соединение отклонено (тест)")


async def bad_answer(state: str, question: str, options: dict) -> Decision:
    raise SolverBadAnswer("ответ не разобран (тест)")


@pytest.mark.asyncio
async def test_confident_answer_opens_the_band():
    step = await constraint_gate.ask_gate("подбери трёшки дешевле 13000000",
                                          ask=answering("да", 0.93), threshold=0.75)

    assert step.level == LEVEL_SOLVER
    assert constraint_gate.only_constraint(step) is True


@pytest.mark.asyncio
async def test_negative_answer_keeps_the_band_closed():
    step = await constraint_gate.ask_gate("а какие документы нужны для сделки?",
                                          ask=answering("нет", 0.95), threshold=0.75)

    assert constraint_gate.only_constraint(step) is False


@pytest.mark.asyncio
async def test_unsure_answer_does_not_open_the_band():
    """Уверенность ниже порога — идём к агенту, как сегодня: полосу открывает только уверенность."""
    step = await constraint_gate.ask_gate("подбери что-нибудь",
                                          ask=answering("да", 0.60), threshold=0.75)

    assert step.level == LEVEL_CLOUD
    assert constraint_gate.only_constraint(step) is False


@pytest.mark.asyncio
async def test_solver_unavailable_keeps_the_previous_behaviour():
    """«Не знаю» — не «да»: при сбое решателя ход остаётся у агента, а гейт не бросает."""
    step = await constraint_gate.ask_gate("подбери двушки дешевле 8000000",
                                          ask=broken, threshold=0.75)

    assert step.level == LEVEL_CLOUD
    assert constraint_gate.only_constraint(step) is False


@pytest.mark.asyncio
async def test_bad_answer_keeps_the_previous_behaviour():
    step = await constraint_gate.ask_gate("подбери двушки дешевле 8000000",
                                          ask=bad_answer, threshold=0.75)

    assert constraint_gate.only_constraint(step) is False


@pytest.mark.asyncio
async def test_judge_returns_both_views():
    """Замеру нужен и шаг, и сам ответ: по одному шагу не отличить «не пустил» от «не ответил»."""
    step, decision = await constraint_gate.judge("подбери трёшки", ask=answering("да", 0.91),
                                                 threshold=0.75)

    assert step.closed_locally is True
    assert decision is not None and decision.answer == "да"


@pytest.mark.asyncio
async def test_judge_returns_no_decision_on_failure():
    step, decision = await constraint_gate.judge("подбери трёшки", ask=broken, threshold=0.75)

    assert step.level == LEVEL_CLOUD and decision is None
