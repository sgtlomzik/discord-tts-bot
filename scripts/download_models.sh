#!/usr/bin/env bash
# Download the Piper voice models the bot uses by default into ./models/.
# Idempotent: existing files are skipped.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p models

BASE="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU"

fetch() {
    local url="$1" dest="$2"
    if [ -s "$dest" ]; then
        echo "skip $dest (already exists)"
        return
    fi
    echo "downloading $dest ..."
    curl -fL --progress-bar -o "$dest" "$url"
}

fetch "$BASE/ruslan/medium/ru_RU-ruslan-medium.onnx"       models/ru_RU-ruslan-medium.onnx
fetch "$BASE/ruslan/medium/ru_RU-ruslan-medium.onnx.json"  models/ru_RU-ruslan-medium.onnx.json
fetch "$BASE/irina/medium/ru_RU-irina-medium.onnx"         models/ru_RU-irina-medium.onnx
fetch "$BASE/irina/medium/ru_RU-irina-medium.onnx.json"    models/ru_RU-irina-medium.onnx.json

echo "done: $(du -sh models | cut -f1) in ./models"
