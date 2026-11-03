import re

import yaml

from scripts.demo import SCENARIOS


def test_demo_scenarios_match_checked_in_alert_rules():
    with open("monitoring/alerts.yml") as stream:
        rules = {r["alert"]: r for g in yaml.safe_load(stream)["groups"] for r in g["rules"]}
    for spec in SCENARIOS.values():
        rule = rules[spec["alert"]]
        # The demo's pass/fail signal must be the alert's own expression and threshold.
        expression = " ".join(rule["expr"].split())
        assert " ".join(spec["signal"].split()) in expression.replace("( ", "(").replace(" )", ")")
        assert re.search(rf">\s*{spec['threshold']:.2f}".rstrip("0"), expression)


def test_network_scenarios_outlast_detection_and_recovery():
    for name in ("latency", "timeout"):
        # 20 s baseline + 60 s fault + up to 45 s recovery must fit inside the load window.
        assert SCENARIOS[name]["load_seconds"] >= 20 + 60 + 45
