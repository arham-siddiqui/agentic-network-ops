import pytest

from scripts.generate_load import validate_config


@pytest.mark.parametrize(
    ("duration", "rate", "concurrency"),
    [
        (0, 10, 4),
        (601, 10, 4),
        (float("nan"), 10, 4),
        (60, 0, 4),
        (60, 51, 4),
        (60, float("inf"), 4),
        (60, 10, 0),
        (60, 10, 9),
        (60, 10, 1.5),
        (60, 10, True),
    ],
)
def test_load_bounds(duration, rate, concurrency):
    with pytest.raises(ValueError):
        validate_config(duration, rate, concurrency)


def test_load_defaults_and_maximum_are_bounded():
    validate_config(120, 10, 4)
    validate_config(600, 50, 8)
