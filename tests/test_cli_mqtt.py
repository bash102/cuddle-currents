from cuddle.cli import build_engine, build_parser
from cuddle.core.models import Source
from cuddle.sources.mqtt_source import GatewayMqttSource


def test_mqtt_source_builds_from_cli():
    args = build_parser().parse_args(["--source", "mqtt", "--broker", "localhost:1884"])
    engine = build_engine(args)
    assert isinstance(engine.source, GatewayMqttSource)
    assert engine.source_type == Source.mqtt
    assert engine.source._broker == "localhost" and engine.source._port == 1884
