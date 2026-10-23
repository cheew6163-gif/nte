"""Render the independently calibrated target next to detector evidence, without input."""
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
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args=parser.parse_args()
    calibration=json.loads((ROOT/"tests/frames999/target_calibration.json").read_text(encoding="utf-8"))
    frame=read_image(ROOT/"tests/frames999"/calibration["reference_frame"])
    found=Detector999(ROOT/"assets999").detect(frame)
    expected=tuple(calibration["reference_target"])
    target=found.targets.get("bonfire")
    if target is None or max(abs(a-b) for a,b in zip(target,expected)) > 1:
        raise SystemExit("Detector did not agree with the independent calibration")
    result=frame.copy()
    cv2.rectangle(result,(560,290),(920,650),(225,185,55),2)
    cv2.circle(result,expected,19,(70,245,105),3)
    cv2.drawMarker(result,expected,(70,245,105),cv2.MARKER_CROSS,42,2)
    cv2.line(result,(expected[0]+22,expected[1]-12),(985,280),(70,245,105),2)
    cv2.rectangle(result,(370,8),(1488,110),(18,22,28),-1)
    labels=["Independent user target: (746, 424) - green cross",
            "Road/building context: cyan box; player/text/rings masked",
            "Baseline zoom only | match %.3f | second %.3f | local %.3f" %
            (found.scores["map_context"],found.scores["map_context_second"],found.scores["map_context_parts"])]
    for row,label in enumerate(labels):
        cv2.putText(result,label,(390,36+row*29),cv2.FONT_HERSHEY_SIMPLEX,.62,(240,245,245),1,cv2.LINE_AA)
    save_image(args.output,result,redact=True,scene="地图")
    print(json.dumps({"manual_target":expected,"detected_target":target,"output":str(args.output)},ensure_ascii=False))


if __name__ == "__main__":
    main()
