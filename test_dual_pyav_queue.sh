#!/usr/bin/env bash
set -u

PY="$HOME/recorder/venv-pyav14/bin/python"
RECORDER="$HOME/recorder/record_camera_pyav_queue.py"

if [[ ! -x "$PY" || ! -f "$RECORDER" ]]; then
    echo "Не найден интерпретатор или скрипт регистратора."
    exit 1
fi

for DEVICE in /dev/video2 /dev/video4; do
    if ! CONTROL=$(v4l2-ctl -d "$DEVICE" --get-ctrl=exposure_dynamic_framerate 2>&1); then
        echo "$CONTROL"
        exit 1
    fi
    echo "$DEVICE: $CONTROL"
    if [[ "$CONTROL" != *"exposure_dynamic_framerate: 0"* ]]; then
        echo "Нужно отключить exposure_dynamic_framerate для $DEVICE. Запись не запущена."
        exit 1
    fi
done

TESTLOG=$(mktemp -d "$HOME/camera_tests/dual_queue_logs_XXXXXX") || exit 1

"$PY" "$RECORDER" --camera-id cam_lower --device /dev/video2     --seconds 10 --segment-seconds 10 > "$TESTLOG/cam_lower.log" 2>&1 &
LOWER_PID=$!
"$PY" "$RECORDER" --camera-id cam_upper --device /dev/video4     --seconds 10 --segment-seconds 10 > "$TESTLOG/cam_upper.log" 2>&1 &
UPPER_PID=$!

stop_children() {
    trap '' INT TERM
    echo "Запрошена остановка обеих камер..."
    kill -TERM "$LOWER_PID" "$UPPER_PID" 2>/dev/null || true
    wait "$LOWER_PID" 2>/dev/null || true
    wait "$UPPER_PID" 2>/dev/null || true
    echo "Логи: $TESTLOG"
    exit 130
}
trap stop_children INT TERM

echo "Запущены обе камеры. Ожидайте запись и проверку файлов."
echo "Логи: $TESTLOG"
wait "$LOWER_PID"
LOWER_CODE=$?
wait "$UPPER_PID"
UPPER_CODE=$?
trap - INT TERM

echo "Код нижней камеры: $LOWER_CODE"
echo "Код верхней камеры: $UPPER_CODE"
for CAMERA in cam_lower cam_upper; do
    echo "===== $CAMERA: ЛОГ ====="
    cat "$TESTLOG/$CAMERA.log"
done

"$PY" - "$TESTLOG" <<'PY'
import csv
import json
import statistics
import sys
from pathlib import Path

logs = Path(sys.argv[1])
for camera in ("cam_lower", "cam_upper"):
    print(f"===== {camera}: СВОДКА =====")
    try:
        lines = (logs / f"{camera}.log").read_text(encoding="utf-8").splitlines()
        paths = [line[len("Output: "):] for line in lines if line.startswith("Output: ")]
        folder = Path(paths[-1])
        metadata = json.loads((folder / "session_metadata.json").read_text(encoding="utf-8"))
        with (folder / "frames.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        stamps = [int(row["host_receive_monotonic_ns"]) for row in rows]
        gaps = [(right-left)/1e6 for left, right in zip(stamps, stamps[1:])]
        span = (stamps[-1]-stamps[0])/1e9 if len(stamps) >= 2 else 0
        summary = {
            "session": str(folder), "status": metadata.get("status"),
            "accepted_packets": metadata.get("accepted_packets"),
            "written_packets": metadata.get("writer", {}).get("written_packets"),
            "indexed_packets": len(rows),
            "receive_span_s": span,
            "receive_rate": (len(stamps)-1)/span if span > 0 else None,
            "median_host_gap_ms": statistics.median(gaps) if gaps else None,
            "max_host_gap_ms": max(gaps) if gaps else None,
            "host_gaps_over_25_ms": sum(gap > 25 for gap in gaps),
            "host_nonpositive_gaps": sum(gap <= 0 for gap in gaps),
            "queue_overflows": metadata.get("queue_overflows"),
            "queue_high_water_items_observed": metadata.get("queue_high_water_items_observed"),
            "queue_high_water_payload_bytes": metadata.get("queue_high_water_compressed_payload_bytes"),
            "max_writer_queue_delay_ms": metadata.get("writer", {}).get("max_writer_queue_delay_ms"),
            "max_mux_call_ms": metadata.get("writer", {}).get("max_mux_call_ms"),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"Не удалось построить сводку: {type(exc).__name__}: {exc}")
PY

if [[ "$LOWER_CODE" -ne 0 || "$UPPER_CODE" -ne 0 ]]; then
    exit 1
fi
