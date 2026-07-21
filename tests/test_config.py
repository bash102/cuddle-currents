from cuddle.core.config import load_config


def test_orchestrator_defaults_present_without_app_yaml(tmp_path):
    # A config path that doesn't exist -> pure defaults, no app.yaml on disk.
    cfg = load_config(tmp_path / "does-not-exist.yaml")
    assert cfg["orchestrator"]["enabled"] is False
    assert cfg["orchestrator"]["report_debounce"] == 0.5
    assert cfg["orchestrator"]["reconcile_interval"] == 5.0
    assert cfg["orchestrator"]["pending_ttl"] == 8.0
    assert cfg["orchestrator"]["coverage_ttl"] == 60.0
    assert cfg["orchestrator"]["rebalance_cooldown"] == 10.0
    assert cfg["orchestrator"]["evict_cooldown"] == 10.0


def test_orchestrator_defaults_from_real_app_yaml():
    cfg = load_config()
    assert cfg["orchestrator"]["enabled"] is False
    assert cfg["orchestrator"]["report_debounce"] == 0.5
    assert cfg["orchestrator"]["reconcile_interval"] == 5.0
    assert cfg["orchestrator"]["pending_ttl"] == 8.0
    assert cfg["orchestrator"]["coverage_ttl"] == 60.0
    assert cfg["orchestrator"]["rebalance_cooldown"] == 10.0
    assert cfg["orchestrator"]["evict_cooldown"] == 10.0
