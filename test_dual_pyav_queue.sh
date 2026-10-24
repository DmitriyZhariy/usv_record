#!/usr/bin/env bash
set -u

PY="$HOME/recorder/venv-pyav14/bin/python"
RECORDER="$HOME/recorder/record_camera_pyav_queue.py"
WRAPPER="$HOME/recorder/record_dual_session.py"

if [[ ! -x "$PY" || ! -f "$RECORDER" || ! -f "$WRAPPER" ]]; then
    echo "Не найден интерпретатор, рекордер или wrapper записи двух камер."
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

WRAPPER_LOG=$(mktemp "${TMPDIR:-/tmp}/usv_dual_queue_XXXXXX") || exit 1

# Wrapper сам обрабатывает SIGINT/SIGTERM и завершает оба рекордера.
trap '' INT TERM
"$PY" "$WRAPPER" --seconds 10 --segment-seconds 10 2>&1 | tee "$WRAPPER_LOG"
WRAPPER_CODE=${PIPESTATUS[0]}
trap - INT TERM

SESSION=$(awk '/^Output: /{line=substr($0, 9)} END{print line}' "$WRAPPER_LOG")
if [[ -z "$SESSION" || ! -d "$SESSION" ]]; then
    echo "Не удалось определить каталог сессии из строки Output:."
    echo "Код wrapper: $WRAPPER_CODE; лог wrapper: $WRAPPER_LOG"
    exit 1
fi

echo "Сессия: $SESSION"
echo "Код wrapper: $WRAPPER_CODE"
echo "Лог wrapper: $WRAPPER_LOG"

for CAMERA in cam-left cam-right; do
    echo "===== $CAMERA: ЛОГ ====="
    if [[ -f "$SESSION/$CAMERA.log" ]]; then
        cat "$SESSION/$CAMERA.log"
    else
        echo "Нет файла $SESSION/$CAMERA.log"
    fi
done

"$PY" - "$SESSION" <<'PY'
# -*- coding: utf-8 -*-
import csv
import json
import statistics
import sys
from pathlib import Path

session = Path(sys.argv[1])
failed = False
for camera in ("cam-left", "cam-right"):
    print(f"===== {camera}: СВОДКА =====")
    folder = session / camera
    try:
        metadata = json.loads(
            (folder / "session_metadata.json").read_text(encoding="utf-8"))
        with (folder / "frames.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        stamps = [int(row["host_receive_monotonic_ns"]) for row in rows]
        gaps = [(right - left) / 1e6
                for left, right in zip(stamps, stamps[1:])]
        span = (stamps[-1] - stamps[0]) / 1e9 if len(stamps) >= 2 else 0
        if metadata.get("status") != "verified":
            failed = True
        if not rows:
            failed = True
        summary = {
            "session": str(folder),
            "status": metadata.get("status"),
            "accepted_packets": metadata.get("accepted_packets"),
            "written_packets": metadata.get("writer", {}).get("written_packets"),
            "indexed_packets": len(rows),
            "receive_span_s": span,
            "receive_rate": (len(stamps) - 1) / span if span > 0 else None,
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
        failed = True
        print(f"Не удалось построить сводку: {type(exc).__name__}: {exc}")

sys.exit(1 if failed else 0)
PY
SUMMARY_CODE=$?

if [[ "$WRAPPER_CODE" -ne 0 ]]; then
    echo "Wrapper завершился с кодом $WRAPPER_CODE; см. метаданные и логи."
    exit 1
fi
if [[ "$SUMMARY_CODE" -ne 0 ]]; then
    echo "Одна или обе записи не прошли проверку; см. сводку и метаданные."
    exit 1
fi
echo "Обе камеры записаны и проверены: $SESSION"
