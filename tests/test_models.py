from app.models import BoundingBox, confidence_band


def test_confidence_bands_are_stable_at_boundaries() -> None:
    assert confidence_band(0.85) == "high"
    assert confidence_band(0.849) == "medium"
    assert confidence_band(0.65) == "medium"
    assert confidence_band(0.649) == "low"


def test_bounding_box_accepts_normalized_coordinates() -> None:
    box = BoundingBox(x1=0.1, y1=0.2, x2=0.8, y2=0.9)
    assert box.x2 == 0.8
