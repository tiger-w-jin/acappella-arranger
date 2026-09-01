#!/usr/bin/env bash
# One-time setup: create the virtualenv and install everything.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv
.venv/bin/pip install --upgrade pip

# Everything except basic-pitch, which needs special handling.
.venv/bin/pip install \
  fastapi "uvicorn[standard]" python-multipart \
  music21 pretty_midi numpy librosa soundfile \
  onnxruntime mir_eval resampy pytest pyphen

# basic-pitch declares tensorflow<2.15.1, which has no wheel for Python 3.11+.
# The wheel ships an ONNX copy of the model, and onnxruntime (installed above)
# runs it, so install the package itself without its dependency list.
.venv/bin/pip install --no-deps basic-pitch==0.4.0

echo
echo "Setup complete. Start the app with:  ./run.sh"
