#!/usr/bin/env bash
# Downloads WFHelper's OCR models (MIT) into models/, pinned to one commit.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REV="06fa689715b0488eb6345b3b1649425cd5a268c8"
BASE="https://raw.githubusercontent.com/WFHelper/WFHelper/$REV/resources/riven-ocr"
mkdir -p "$ROOT/models/paddle" "$ROOT/models/yolo"
for f in paddle/ch_PP-OCRv3_rec_infer.onnx paddle/ch_dict.txt yolo/stat_line_detector.onnx; do
  [ -s "$ROOT/models/$f" ] || curl -sSfL -o "$ROOT/models/$f" "$BASE/$f"
done
ls -l "$ROOT/models"/*
