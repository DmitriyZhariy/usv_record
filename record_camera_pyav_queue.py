#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import queue
import signal
import threading
import time
import traceback
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

import av

stop_requested = False


def request_stop(signum, frame):
    global stop_requested
    stop_requested = True


def save_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class PayloadQueue:
    def __init__(self, max_items, max_bytes):
        self.queue = queue.Queue(maxsize=max_items)
        self.lock = threading.Lock()
        self.max_bytes = max_bytes
        self.current_bytes = 0
        self.high_bytes = 0
        self.high_items = 0

    def put(self, item):
        size = len(item["payload"])
        with self.lock:
            if self.current_bytes + size > self.max_bytes:
                raise queue.Full("Compressed-payload byte limit reached")
            self.queue.put_nowait(item)
            self.current_bytes += size
            self.high_bytes = max(self.high_bytes, self.current_bytes)
            self.high_items = max(self.high_items, self.queue.qsize())

    def get(self):
        item = self.queue.get(timeout=0.2)
        with self.lock:
            self.current_bytes -= len(item["payload"])
        return item


def write_packets(buffer, capture_done, writer_failed, template, folder, args, result):
    output = None
    segment_id = -1
    origin_ns = None
    fields = [
        "frame_id", "segment_id", "frame_in_segment", "segment_file",
        "source_pts", "source_dts", "source_time_base", "source_duration",
        "host_receive_monotonic_ns", "host_receive_utc_ns",
        "segment_origin_monotonic_ns", "requested_local_pts_ms",
        "packet_size", "packet_sha256", "source_is_keyframe", "source_is_corrupt",
        "writer_queue_delay_ms", "mux_call_ms",
    ]
    try:
        with (folder / "frames.csv").open("w", newline="", encoding="utf-8") as handle:
            index = csv.DictWriter(handle, fieldnames=fields)
            index.writeheader()
            while True:
                try:
                    item = buffer.get()
                except queue.Empty:
                    if capture_done.is_set():
                        break
                    continue

                writer_started_ns = time.monotonic_ns()
                mono_ns = item["host_receive_monotonic_ns"]
                if output is None or mono_ns - origin_ns >= args.segment_seconds * 1e9:
                    if output is not None:
                        closing = output
                        output = None
                        closing.close()
                    segment_id += 1
                    origin_ns = mono_ns
                    filename = f"{args.camera_id}_segment_{segment_id:04d}.mkv"
                    output = av.open(str(folder / filename), mode="w", format="matroska")
                    out_stream = output.add_stream_from_template(template)
                    out_stream.time_base = Fraction(1, 1000)
                    result["segments"].append({
                        "file": filename,
                        "origin_host_monotonic_ns": origin_ns,
                        "written_packets": 0,
                    })
                    print(f"Opened {filename}", flush=True)

                local_pts = (mono_ns - origin_ns) // 1_000_000
                packet = av.Packet(item["payload"])
                packet.stream = out_stream
                packet.time_base = Fraction(1, 1000)
                packet.pts = local_pts
                packet.dts = local_pts
                packet.is_keyframe = item["source_is_keyframe"]
                packet.is_corrupt = item["source_is_corrupt"]
                sha = hashlib.sha256(item["payload"]).hexdigest()
                mux_start = time.monotonic_ns()
                output.mux(packet)
                mux_ms = (time.monotonic_ns() - mux_start) / 1e6
                queue_ms = (writer_started_ns - mono_ns) / 1e6
                current_segment = result["segments"][-1]
                index.writerow({
                    "frame_id": item["frame_id"], "segment_id": segment_id,
                    "frame_in_segment": current_segment["written_packets"],
                    "segment_file": filename, "source_pts": item["source_pts"],
                    "source_dts": item["source_dts"],
                    "source_time_base": item["source_time_base"],
                    "source_duration": item["source_duration"],
                    "host_receive_monotonic_ns": mono_ns,
                    "host_receive_utc_ns": item["host_receive_utc_ns"],
                    "segment_origin_monotonic_ns": origin_ns,
                    "requested_local_pts_ms": local_pts,
                    "packet_size": len(item["payload"]), "packet_sha256": sha,
                    "source_is_keyframe": int(item["source_is_keyframe"]),
                    "source_is_corrupt": int(item["source_is_corrupt"]),
                    "writer_queue_delay_ms": queue_ms, "mux_call_ms": mux_ms,
                })
                current_segment["written_packets"] += 1
                result["written_packets"] += 1
                result["max_writer_queue_delay_ms"] = max(result["max_writer_queue_delay_ms"], queue_ms)
                result["max_mux_call_ms"] = max(result["max_mux_call_ms"], mux_ms)
                if result["written_packets"] % 60 == 0:
                    handle.flush()
                packet = None
                item = None
    except BaseException:
        result["error"] = traceback.format_exc()
        writer_failed.set()
    finally:
        if output is not None:
            try:
                output.close()
            except BaseException:
                result["close_error"] = traceback.format_exc()
                writer_failed.set()


def verify(folder, segments):
    results = []
    with (folder / "frames.csv").open(newline="", encoding="utf-8") as handle:
        rows = iter(csv.DictReader(handle))
        verified = 0
        for segment_id, segment in enumerate(segments):
            actual_count = 0
            payload_ok = True
            pts_ok = True
            keyflags_ok = True
            with av.open(str(folder / segment["file"])) as check:
                stream = check.streams.video[0]
                for packet in check.demux(stream):
                    if packet.size == 0:
                        continue
                    row = next(rows, None)
                    if row is None:
                        raise RuntimeError("More output packets than index rows")
                    if int(row["segment_id"]) != segment_id or int(row["frame_in_segment"]) != actual_count:
                        raise RuntimeError("Output/index order mismatch")
                    if int(row["frame_id"]) != verified:
                        raise RuntimeError("Non-contiguous accepted packet IDs")
                    payload_ok = payload_ok and hashlib.sha256(bytes(packet)).hexdigest() == row["packet_sha256"]
                    stamp = None if packet.pts is None else packet.pts * Fraction(packet.time_base or stream.time_base)
                    pts_ok = pts_ok and stamp == Fraction(int(row["requested_local_pts_ms"]), 1000)
                    keyflags_ok = keyflags_ok and packet.is_keyframe == bool(int(row["source_is_keyframe"]))
                    actual_count += 1
                    verified += 1
            counts_ok = actual_count == segment["written_packets"]
            info = {
                "file": segment["file"], "read_packets": actual_count,
                "written_packets": segment["written_packets"],
                "payload_match": payload_ok, "pts_match": pts_ok,
                "keyflags_match": keyflags_ok, "counts_match": counts_ok,
            }
            results.append(info)
            print(f"{segment['file']}: packets={actual_count}/{segment['written_packets']}, "
                  f"payload={payload_ok}, PTS={pts_ok}, keyframes={keyflags_ok}", flush=True)
        if next(rows, None) is not None:
            raise RuntimeError("Some index rows have no output packet")
    return verified, results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="/dev/video2")
    parser.add_argument("--camera-id", choices=("cam_lower", "cam_upper"), default="cam_lower")
    parser.add_argument("--seconds", type=float, default=35)
    parser.add_argument("--segment-seconds", type=float, default=10)
    parser.add_argument("--queue-frames", type=int, default=120)
    parser.add_argument("--queue-mib", type=int, default=32)
    args = parser.parse_args()
    if min(args.seconds, args.segment_seconds, args.queue_frames, args.queue_mib) <= 0:
        parser.error("All limits must be positive")
    if not hasattr(av.container.OutputContainer, "add_stream_from_template"):
        raise RuntimeError("Run this script with the tested PyAV 14 environment")
    probe = av.Packet(b"test")
    probe.is_keyframe = True
    probe.is_corrupt = False
    del probe
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    folder = Path.home() / "recordings" / datetime.now(timezone.utc).strftime(
        f"queued_{args.camera_id}_%Y%m%d_%H%M%S_%f"
    )
    folder.mkdir(parents=True, exist_ok=False)
    report = {
        "status": "starting", "camera_id": args.camera_id, "device": args.device,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "pyav_version": av.__version__, "ffmpeg_libraries": av.library_versions,
        "requested_mode": {"format": "mjpeg", "width": 1280, "height": 720, "fps": 60},
        "timestamp_policy": "host_receive_monotonic_ns_in_index; local_video_pts_floor_to_ms",
        "timestamp_is_exposure_time": False,
        "source_timestamp_policy": "preserve_original_pts_dts_time_base_in_index",
        "segment_seconds_requested": args.segment_seconds,
        "queue_limits": {"frames": args.queue_frames, "compressed_payload_bytes": args.queue_mib * 1024**2},
        "queue_overflows": 0, "observed_packets": 0, "accepted_packets": 0,
        "implementation": "capture_main_thread_writer_thread_owned_payload_copies",
    }
    report_path = folder / "session_metadata.json"
    save_json(report_path, report)
    buffer = PayloadQueue(args.queue_frames, args.queue_mib * 1024**2)
    capture_done = threading.Event()
    writer_failed = threading.Event()
    result = {"segments": [], "written_packets": 0, "max_writer_queue_delay_ms": 0, "max_mux_call_ms": 0}
    worker = None
    reason = "unknown"
    capture_error = None

    try:
        options = {"input_format": "mjpeg", "video_size": "1280x720", "framerate": "60"}
        with av.open(args.device, format="v4l2", options=options) as camera:
            stream = camera.streams.video[0]
            context = stream.codec_context
            if context.name != "mjpeg" or (context.width, context.height) != (1280, 720):
                raise RuntimeError("Camera negotiated an unexpected mode")
            worker = threading.Thread(
                target=write_packets,
                args=(buffer, capture_done, writer_failed, stream, folder, args, result),
                name="mkv-writer", daemon=True,
            )
            worker.start()
            report["status"] = "recording"
            save_json(report_path, report)
            print(f"Recording {args.camera_id}: {args.device}", flush=True)
            print(f"Output: {folder}", flush=True)
            deadline = time.monotonic() + args.seconds
            demuxer = camera.demux(stream)
            packet = None
            try:
                for packet in demuxer:
                    mono_ns = time.monotonic_ns()
                    utc_ns = time.time_ns()
                    if writer_failed.is_set():
                        raise RuntimeError("Writer failed; see metadata")
                    if stop_requested:
                        reason = "user_stop"
                        break
                    if time.monotonic() >= deadline:
                        reason = "duration_limit"
                        break
                    if packet.size == 0:
                        continue
                    report["observed_packets"] += 1
                    item = {
                        "frame_id": report["accepted_packets"],
                        "payload": bytes(packet), "source_pts": packet.pts,
                        "source_dts": packet.dts,
                        "source_time_base": str(Fraction(packet.time_base or stream.time_base)),
                        "source_duration": packet.duration,
                        "host_receive_monotonic_ns": mono_ns, "host_receive_utc_ns": utc_ns,
                        "source_is_keyframe": bool(packet.is_keyframe),
                        "source_is_corrupt": bool(packet.is_corrupt),
                    }
                    packet = None
                    try:
                        buffer.put(item)
                    except queue.Full:
                        report["queue_overflows"] += 1
                        report["rejected_frame_id"] = item["frame_id"]
                        raise RuntimeError("Queue overflow: recording stopped, no silent dropping")
                    report["accepted_packets"] += 1
                    item = None
                else:
                    raise RuntimeError("Camera stream ended unexpectedly")
            finally:
                packet = None
                demuxer.close()
                capture_done.set()
                worker.join(timeout=30)
                if worker.is_alive():
                    raise RuntimeError("Writer did not drain within 30 seconds; files may be incomplete")
    except BaseException:
        capture_error = traceback.format_exc()
        print(capture_error, flush=True)
    finally:
        capture_done.set()
        if worker is not None and worker.is_alive():
            worker.join(timeout=1)
        alive = worker is not None and worker.is_alive()
        report["writer_thread_still_alive"] = alive
        report["stop_reason"] = reason
        report["queue_high_water_compressed_payload_bytes"] = buffer.high_bytes
        report["queue_high_water_items_observed"] = buffer.high_items
        if capture_error:
            report["capture_error"] = capture_error
        if not alive:
            report["writer"] = result
        else:
            report["writer_result_unavailable"] = True
        report["status"] = "failed"
        save_json(report_path, report)

    clean = (
        capture_error is None and not writer_failed.is_set()
        and worker is not None and not worker.is_alive()
        and report["queue_overflows"] == 0
        and report["accepted_packets"] == result["written_packets"] > 0
    )
    if clean:
        try:
            verified, checks = verify(folder, result["segments"])
            report["verification"] = checks
            all_ok = verified == report["accepted_packets"] and all(
                item["payload_match"] and item["pts_match"] and item["keyflags_match"] and item["counts_match"]
                for item in checks
            )
            report["status"] = "verified" if all_ok else "needs_review"
        except BaseException:
            report["verification_error"] = traceback.format_exc()
            print(report["verification_error"], flush=True)
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    save_json(report_path, report)
    print(f"Accepted: {report['accepted_packets']}; written: {result['written_packets']}")
    print(f"Queue overflows: {report['queue_overflows']}")
    print(f"Queue high water: {buffer.high_items} items; {buffer.high_bytes} payload bytes")
    print(f"Max writer queue delay: {result['max_writer_queue_delay_ms']:.3f} ms")
    print(f"Max mux call: {result['max_mux_call_ms']:.3f} ms")
    print(f"Status: {report['status']}")
    print(f"Output: {folder}")
    return 0 if report["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
