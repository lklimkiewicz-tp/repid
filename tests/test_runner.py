from __future__ import annotations

from typing import Any

import pytest

from repid import (
    Config,
    Connection,
    HealthCheckServer,
    HealthCheckStatus,
    InMemoryMessageBroker,
    Job,
    ParametersT,
    Queue,
    RoutingKeyT,
    default_retry_policy_factory,
)
from repid._runner import _Runner
from repid.actor import ActorData
from repid.connections.in_memory.consumer import _InMemoryConsumer


class FaultyConsumer(_InMemoryConsumer):
    async def consume(self) -> tuple[RoutingKeyT, str, ParametersT]:
        result = await super().consume()
        if result[0].topic == "fail":
            raise RuntimeError("I'm a faulty consumer.")
        return result


def connection_with_faulty_consumer() -> Connection:
    broker = InMemoryMessageBroker()
    broker.CONSUMER_CLASS = FaultyConsumer  # type: ignore[misc]
    return Connection(broker)


@pytest.fixture
async def seed_faulty_consumer() -> Connection:
    conn = connection_with_faulty_consumer()
    await Queue(_connection=conn).declare()
    await Job("test", _connection=conn).enqueue()
    await Job("fail", _connection=conn).enqueue()
    return conn


async def do_nothing() -> None:
    return


RUN_ONE_QUEUE_CONFIG: dict[str, Any] = {
    "queue_name": "default",
    "topics": ["test", "fail"],
    "actors": {
        "test": ActorData(
            fn=do_nothing,
            name="test",
            queue="default",
            retry_policy=default_retry_policy_factory(),
            converter=Config.CONVERTER(fn=do_nothing),
        ),
        "fail": ActorData(
            fn=do_nothing,
            name="fail",
            queue="default",
            retry_policy=default_retry_policy_factory(),
            converter=Config.CONVERTER(fn=do_nothing),
        ),
    },
}


async def test_failing_consumer(
    caplog: pytest.LogCaptureFixture,
    seed_faulty_consumer: Connection,
) -> None:
    runner = _Runner(
        max_tasks=2,
        tasks_concurrency_limit=1,
        _connection=seed_faulty_consumer,
    )
    await runner.run_one_queue(**RUN_ONE_QUEUE_CONFIG)

    assert any(
        (
            all(
                (
                    "CRITICAL" in x,
                    "Error while running consumer on queue 'default'." in x,
                ),
            )
            for x in caplog.text.splitlines()
        ),
    )
    assert "RuntimeError: I'm a faulty consumer." in caplog.text.splitlines()


async def test_failing_consumer_signals_to_health_check_server(
    seed_faulty_consumer: Connection,
) -> None:
    health_check_server = HealthCheckServer()

    assert health_check_server.health_status == HealthCheckStatus.OK

    runner = _Runner(
        max_tasks=2,
        tasks_concurrency_limit=1,
        health_check_server=health_check_server,
        _connection=seed_faulty_consumer,
    )
    await runner.run_one_queue(**RUN_ONE_QUEUE_CONFIG)

    assert health_check_server.health_status == HealthCheckStatus.UNHEALTHY
