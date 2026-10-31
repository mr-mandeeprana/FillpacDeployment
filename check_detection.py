"""
FillPac AI - detection diagnostic (run from the project folder).

Runs the model on the FULL frame (like your direct test), then reports:
  - real video size
  - how many frames have a detection, and the confidence spread
  - how many detections would survive the app's confidence filters
  - where the boxes are relative to detection_roi / jam ROI
  - saves annotated full-size frames to check_out/ with the config ROIs drawn

Usage:  python check_detection.py
"""
import os
import statistics

import cv2
import yaml
from ultralytics import YOLO

CONFIG = "config.yaml"
VIDEO = "data/videos/Video Project.mp4"
STEP = 5            # test every 5th frame
MAX_FRAMES = 2000   # stop early
MAX_SAVED = 12      # annotated images to save
OUT_DIR = "check_out"

with open(CONFIG, encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

cam = cfg["cameras"][0]
det_roi = cam["model"]["detection_roi"]
jam_roi = cam["jam_detection"]["roi"]
line = cam["roi"]
app_conf = cam["model"]["bag_confidence"]
allowed = cam["model"].get("allowed_classes")

model = YOLO(cfg["model"]["path"])
print("Model classes:", model.names)
print("allowed_classes in config:", allowed)

cap = cv2.VideoCapture(VIDEO)
W, H = int(cap.get(3)), int(cap.get(4))
print(f"Video size: {W}x{H}  frames: {int(cap.get(7))}  fps: {cap.get(5):.1f}")

os.makedirs(OUT_DIR, exist_ok=True)


def inside(cx, cy, r):
    return r["x1"] <= cx <= r["x2"] and r["y1"] <= cy <= r["y2"]


tested = with_det = saved = 0
confs = []
in_det_roi = in_jam_roi = above_app_conf = 0
idx = -1

while idx < MAX_FRAMES:
    ok, frame = cap.read()
    if not ok:
        break
    idx += 1
    if idx % STEP:
        continue
    tested += 1

    res = model.predict(frame, conf=0.10, imgsz=640, verbose=False)[0]
    if len(res.boxes) == 0:
        continue
    with_det += 1

    vis = frame.copy()
    for r, color in ((det_roi, (255, 128, 0)), (jam_roi, (0, 255, 0))):
        cv2.rectangle(vis, (r["x1"], r["y1"]), (r["x2"], r["y2"]), color, 2)
    cv2.line(vis, (line["x1"], line["y1"]), (line["x2"], line["y2"]),
             (0, 255, 255), 2)

    for b in res.boxes:
        c = float(b.conf)
        confs.append(c)
        x1, y1, x2, y2 = b.xyxy[0].tolist()
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        in_det_roi += inside(cx, cy, det_roi)
        in_jam_roi += inside(cx, cy, jam_roi)
        above_app_conf += c >= app_conf
        cv2.rectangle(vis, (int(x1), int(y1)), (int(x2), int(y2)),
                      (0, 0, 255), 3)
        cv2.putText(vis, f"{c:.2f}", (int(x1), max(int(y1) - 8, 15)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)

    if saved < MAX_SAVED:
        cv2.imwrite(os.path.join(OUT_DIR, f"frame_{idx:05d}.png"), vis)
        saved += 1

cap.release()

print("\n===== SUMMARY =====")
print(f"Frames tested: {tested}, frames with a detection: {with_det} "
      f"({100 * with_det / max(tested, 1):.0f}%)")
if confs:
    print(f"Detections: {len(confs)}  "
          f"conf min/median/max: {min(confs):.2f} / "
          f"{statistics.median(confs):.2f} / {max(confs):.2f}")
    print(f"Detections with conf >= {app_conf} (app bag_confidence): "
          f"{above_app_conf}")
    print(f"Centers inside detection_roi: {in_det_roi}")
    print(f"Centers inside jam/slider ROI: {in_jam_roi}")
print(f"Annotated frames saved in ./{OUT_DIR}/")