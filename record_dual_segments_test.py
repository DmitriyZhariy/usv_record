#!/usr/bin/env python3
import argparse
import json
import signal
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

CAMERAS = {
    "cam_lower": "/dev/video2",
    "cam_upper": "/dev/video4",
}

OUTPUT_ROOT = Path.home() / "recordings"
stop_requested = False


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def request_stop(signum, frame):
    global stop_requested
    stop_requested = True


def save_metadata(path, metadata):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=0)
    args = parser.parse_args()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    session_id = datetime.now(timezone.utc).strftime(
        "%Y%m%d_%H%M%S_%f"
    )
    session_dir = OUTPUT_ROOT / session_id
    session_dir.mkdir(parents=True, exist_ok=False)
    metadata_path = session_dir / "session_metadata.json"

    metadata = {
        "session_id": session_id,
        "started_at_utc": utc_now(),
        "status": "starting",
        "cameras": {},
    }
    processes = {}
    error = None
    reason = "unknown"

    save_metadata(metadata_path, metadata)

    try:
        for name, device in CAMERAS.items():
            if stop_requested:
                break

            video_path = session_dir / f"{name}_segment_%04d.mkv"
            log_path = session_dir / f"{name}_ffmpeg.log"

            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "info",
                "-nostats",
                "-f", "v4l2",
                "-input_format", "mjpeg",
                "-video_size", "1280x720",
                "-framerate", "60",
		"-use_wallclock_as_timestamps", "1",
                "-i", device,
                "-map", "0:v:0",
                "-c:v", "copy",
                "-f", "segment",
                "-segment_time", "10",
                "-segment_format", "matroska",
                "-reset_timestamps", "1",
                "-segment_list", str(session_dir / f"{name}_segments.csv"),
                "-segment_list_type", "csv",
                str(video_path),
            ]

            with log_path.open("wb") as log_file:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=log_file,
                    start_new_session=True,
                    bufsize=0,
                )

            processes[name] = process
            metadata["cameras"][name] = {
                "device": device,
                "requested_width": 1280,
                "requested_height": 720,
                "requested_fps": 60,
                "input_format": "mjpeg",
                "video_codec": "mjpeg",
                "encoding_mode": "stream_copy",
                "output_pattern": str(video_path),
                "segment_duration_requested_seconds": 10,
                "segment_list": str(session_dir / f"{name}_segments.csv"),
                "timestamp_source": "ffmpeg_wallclock",
                "timestamps_are_exposure_times": False,
                "segment_timestamps_reset": True,
                "ffmpeg_log": str(log_path),
                "command": command,
                "pid": process.pid,
            }
            save_metadata(metadata_path, metadata)
            print(f"{name}: процесс запущен, PID={process.pid}")

        metadata["status"] = "recording"
        save_metadata(metadata_path, metadata)

        print(f"Сессия: {session_dir}")
        print("Остановка: Ctrl+C один раз")

        started = time.monotonic()

        while True:
            if stop_requested:
                reason = "user_stop"
                break

            exited = [
                name
                for name, process in processes.items()
                if process.poll() is not None
            ]
            if exited:
                raise RuntimeError(
                    f"FFmpeg завершился до команды остановки: {exited}"
                )

            if args.seconds > 0:
                if time.monotonic() - started >= args.seconds:
                    reason = "duration_limit"
                    break

            time.sleep(0.2)

    except Exception:
        error = traceback.format_exc()
        reason = "error"
        print(error)

    finally:
        print("Останавливаю запись...")

        for name, process in processes.items():
            camera = metadata["cameras"][name]
            camera["stop_method"] = "already_exited"

            if process.poll() is None:
                camera["stop_method"] = "stdin_q"
                try:
                    process.stdin.write(b"q\n")
                except (BrokenPipeError, OSError):
                    camera["stop_method"] = "stdin_q_failed"

        for name, process in processes.items():
            camera = metadata["cameras"][name]

            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                print(f"{name}: q не помог, отправляю один SIGINT")
                camera["stop_method"] = "sigint_fallback"
                process.send_signal(signal.SIGINT)

                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    print(f"{name}: принудительная остановка SIGKILL")
                    camera["stop_method"] = "sigkill"
                    process.kill()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        camera["wait_timeout_after_kill"] = True

            if process.stdin:
                process.stdin.close()

            camera["return_code"] = process.poll()

        clean = (
            error is None
            and len(processes) == len(CAMERAS)
            and all(
                camera.get("return_code") == 0
                and camera.get("stop_method") == "stdin_q"
                for camera in metadata["cameras"].values()
            )
        )

        metadata["finished_at_utc"] = utc_now()
        metadata["stop_reason"] = reason
        metadata["status"] = "stopped" if clean else "needs_review"

        if error:
            metadata["error"] = error

        save_metadata(metadata_path, metadata)
        print(f"Статус: {metadata['status']}")
        print(f"Метаданные: {metadata_path}")


if __name__ == "__main__":
    main()

