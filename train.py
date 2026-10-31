from argparse import ArgumentParser
from pathlib import Path
import os

import yaml
from ultralytics import YOLO


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
DATA_PATH = ROOT / "models" / "data.yaml"
LOCAL_YOLO_CONFIG_DIR = ROOT / ".ultralytics"
DEFAULT_PROJECT_DIR = ROOT / "runs" / "train"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def build_parser():
    parser = ArgumentParser(description="Train or resume FillPac AI YOLO26n model.")
    parser.add_argument("--data", default=str(DATA_PATH), help="Path to YOLO dataset yaml.")
    parser.add_argument("--model", default=None, help="Model checkpoint to train from.")
    parser.add_argument("--epochs", type=int, default=50, help="Training epochs.")
    parser.add_argument("--imgsz", type=int, default=None, help="Training image size.")
    parser.add_argument("--batch", type=int, default=8, help="Batch size.")
    parser.add_argument("--device", default=None, help="Training device, for example cpu or 0.")
    parser.add_argument(
        "--project",
        default=str(DEFAULT_PROJECT_DIR),
        help="Ultralytics project directory.",
    )
    parser.add_argument("--name", default="fillpac_yolo26n", help="Ultralytics run name.")
    parser.add_argument("--resume", action="store_true", help="Resume from the last checkpoint.")
    return parser


def resolve_model_path(args, config):
    default_model = config.get("model", {}).get("path", "models/yolo26n.pt")

    if args.resume:
        last_checkpoint = Path(args.project) / args.name / "weights" / "last.pt"
        if not last_checkpoint.exists():
            raise FileNotFoundError(
                f"Resume requested but checkpoint was not found: {last_checkpoint}"
            )
        return str(last_checkpoint)

    return args.model or default_model


def main():
    os.environ["YOLO_CONFIG_DIR"] = str(LOCAL_YOLO_CONFIG_DIR)
    LOCAL_YOLO_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    parser = build_parser()
    args = parser.parse_args()
    config = load_config()

    model_path = resolve_model_path(args, config)
    imgsz = args.imgsz or config.get("model", {}).get("image_size", 640)
    device = args.device or config.get("model", {}).get("device", "cpu")
    project_dir = Path(args.project).resolve()
    data_path = Path(args.data).resolve()
    project_dir.mkdir(parents=True, exist_ok=True)

    model = YOLO(model_path)

    if args.resume:
        model.train(resume=True)
        return

    model.train(
        data=str(data_path),
        epochs=args.epochs,
        imgsz=imgsz,
        batch=args.batch,
        device=device,
        project=str(project_dir),
        name=args.name,
    )


if __name__ == "__main__":
    main()
