import pytest

from cuddle.cli import build_engine, build_parser
from cuddle.core.models import Source
from cuddle.sources.mqtt_source import GatewayMqttSource


def test_mqtt_source_builds_from_cli():
    args = build_parser().parse_args(["--source", "mqtt", "--broker", "localhost:1884"])
    engine = build_engine(args)
    assert isinstance(engine.source, GatewayMqttSource)
    assert engine.source_type == Source.mqtt
    assert engine.source._broker == "localhost" and engine.source._port == 1884


def test_orchestrate_flag_with_mqtt_source_attaches_orchestrator():
    args = build_parser().parse_args(
        ["--source", "mqtt", "--broker", "localhost:1884", "--orchestrate"]
    )
    engine = build_engine(args)
    assert engine.orchestrator is not None


def test_orchestrate_flag_with_sim_source_exits():
    args = build_parser().parse_args(["--source", "sim", "--orchestrate"])
    with pytest.raises(SystemExit, match="--orchestrate requires --source mqtt"):
        build_engine(args)
