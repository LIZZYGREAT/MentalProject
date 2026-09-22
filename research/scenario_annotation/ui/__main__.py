from __future__ import annotations

import argparse

import uvicorn

from .server import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description="MindFlow loopback-only Stage 1 annotation UI")
    parser.add_argument("--mode", choices=("annotation", "adjudication"), default="annotation")
    parser.add_argument("--round", dest="round_name", choices=("calibration",), required=True)
    parser.add_argument("--annotator", choices=("Human",), default="Human")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        app = create_app(mode=args.mode, round_name=args.round_name, annotator_id=args.annotator)
    except (RuntimeError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 2
    uvicorn.run(app, host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
