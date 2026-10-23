"""Render the label-based target next to the user-confirmed point, without input."""
from pathlib import Path
import argparse
import json
import sys
import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from yihuan.nte999 import Detector999
from yihuan.vision import read_image, save_image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    calibration = json.loads(
        (ROOT / "tests/frames999/target_calibration.json").read_text(encoding="utf-8"))
    frame = read_image(ROOT / "tests/frames999" / calibration["reference_frame"])
    detector = Detector999(ROOT / "assets999")
    found = detector.detect(frame)
    expected = tuple(calibration["reference_target"])
    target = found.targets.get("bonfire")
    if target is None:
        raise SystemExit("Detector did not locate the label-based bonfire")
    if max(abs(a - b) for a, b in zip(target, expected)) > 15:
        raise SystemExit("Detector target is too far from the independent calibration")
    location = detector.locator.locate(frame)
    result = frame.copy()
    if location.text_box is not None:
        x, y, w, h = location.text_box
        cv2.rectangle(result, (x, y), (x + w, y + h), (80, 220, 80), 2)
    if location.predicted is not None:
        cv2.drawMarker(result, tuple(round(v) for v in location.predicted),
                       (0, 220, 255), cv2.MARKER_CROSS, 22, 2)
    cv2.circle(result, tuple(round(v) for v in expected), 18, (70, 245, 105), 3)
    cv2.drawMarker(result, tuple(round(v) for v in expected),
                   (70, 245, 105), cv2.MARKER_CROSS, 38, 2)
    cv2.circle(result, tuple(round(v) for v in target), 26, (60, 60, 235), 3)
    cv2.drawMarker(result, tuple(round(v) for v in target),
                   (60, 60, 235), cv2.MARKER_CROSS, 34, 2)
    cv2.rectangle(result, (0, 0), (1499, 54), (18, 22, 28), -1)
    labels = [
        "cansi label %.3f (second %.3f); target (%.1f, %.1f)" % (
            location.text_score, location.text_second, target[0], target[1]),
        "green: user-confirmed (%.1f, %.1f); yellow: baseline offset; red: detected icon" % expected,
    ]
    for row, label in enumerate(labels):
        cv2.putText(result, label, (14, 22 + row * 24), cv2.FONT_HERSHEY_SIMPLEX,
                    .62, (240, 245, 245), 1, cv2.LINE_AA)
    save_image(args.output, result, redact=True, scene="地图")
    print(json.dumps({"manual_target": expected, "detected_target": target,
                      "label_score": location.text_score,
                      "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
