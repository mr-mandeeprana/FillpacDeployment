from argparse import ArgumentParser
from pathlib import Path

import cv2


ROOT = Path(__file__).resolve().parent
VIDEOS_DIR = ROOT / "data" / "videos"
IMAGES_DIR = ROOT / "data" / "images"


def build_parser():
    parser = ArgumentParser(description="Extract dataset frames from FillPac videos.")
    parser.add_argument("--videos", default=str(VIDEOS_DIR), help="Video folder path.")
    parser.add_argument("--output", default=str(IMAGES_DIR), help="Image output root.")
    parser.add_argument(
        "--sample-every",
        type=int,
        default=10,
        help="Keep one frame every N frames. Default 30 is about 1 FPS for these videos.",
    )
    return parser


def choose_split(index, total):
    if total <= 1:
        return "train"

    ratio = index / total
    if ratio < 0.7:
        return "train"
    if ratio < 0.9:
        return "val"
    return "test"


def extract_video_frames(video_path, output_root, sample_every):
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    saved = {"train": 0, "val": 0, "test": 0}
    frame_index = 0
    sampled_index = 0
    stem = video_path.stem.replace(" ", "_")

    while True:
        success, frame = cap.read()
        if not success:
            break

        if frame_index % sample_every == 0:
            split = choose_split(sampled_index, max(frame_count // sample_every, 1))
            filename = f"{stem}_frame_{frame_index:05d}.jpg"
            output_path = output_root / split / filename
            cv2.imwrite(str(output_path), frame)
            saved[split] += 1
            sampled_index += 1

        frame_index += 1

    cap.release()
    return saved


def ensure_output_dirs(output_root):
    for split in ("train", "val", "test"):
        (output_root / split).mkdir(parents=True, exist_ok=True)


def main():
    args = build_parser().parse_args()
    videos_dir = Path(args.videos)
    output_root = Path(args.output)
    ensure_output_dirs(output_root)

    summary = {"train": 0, "val": 0, "test": 0}
    videos = sorted(videos_dir.glob("*.mp4"))

    if not videos:
        raise FileNotFoundError(f"No .mp4 videos found in: {videos_dir}")

    for video_path in videos:
        counts = extract_video_frames(video_path, output_root, args.sample_every)
        for split, count in counts.items():
            summary[split] += count
        print(f"{video_path.name}: train={counts['train']} val={counts['val']} test={counts['test']}")

    print(
        "TOTAL:"
        f" train={summary['train']}"
        f" val={summary['val']}"
        f" test={summary['test']}"
    )


if __name__ == "__main__":
    main()
