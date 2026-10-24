#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import signal
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

import av

stop_requested = False


def stop(signum, frame):
    global stop_requested
    stop_requested = True


def save(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="/dev/video2")
    parser.add_argument(
        "--camera-id",
        choices=("cam_lower", "cam_upper"),
        default="cam_lower",
    )
    parser.add_argument("--seconds", type=float, default=35)
    parser.add_argument("--segment-seconds", type=float, default=10)
    args = parser.parse_args()
    if args.seconds <= 0 or args.segment_seconds <= 0:
        parser.error("Durations must be positive")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    folder = Path.home() / "recordings" / datetime.now(timezone.utc).strftime("live_pyav_test_%Y%m%d_%H%M%S_%f")
    folder.mkdir(parents=True, exist_ok=False)
    report_path = folder / "session_metadata.json"
    report = {
        "status": "starting", "device": args.device, "camera_id": args.camera_id,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "requested_mode": {"format": "mjpeg", "width": 1280, "height": 720, "fps": 60},
        "segment_seconds_requested": args.segment_seconds,
        "video_timestamp_source": "host_monotonic_receive_quantized_down_to_ms",
        "index_timestamp_source": "host_monotonic_receive_ns_and_host_utc_receive_ns",
        "source_pts_policy": "preserved_in_index_not_used_as_output_pts",
        "timestamp_is_exposure_time": False,
        "implementation": "single_camera_synchronous_test_no_application_queue",
        "segments": [],
    }
    save(report_path, report)
    output = None
    expected = []
    count = 0
    reason = "unknown"
    packet = None
    demuxer = None
    origin_ns = None

    try:
        options = {"input_format": "mjpeg", "video_size": "1280x720", "framerate": "60"}
        with av.open(args.device, format="v4l2", options=options) as camera:
            stream = camera.streams.video[0]
            if stream.codec_context.name != "mjpeg":
                raise RuntimeError("Expected MJPEG input")
            if (stream.codec_context.width, stream.codec_context.height) != (1280, 720):
                raise RuntimeError("Unexpected input resolution")
            report["status"] = "recording"
            save(report_path, report)
            deadline = time.monotonic() + args.seconds
            demuxer = camera.demux(stream)

            try:
                with (folder / "frames.csv").open("w", newline="", encoding="utf-8") as index:
                    writer = csv.writer(index)
                    writer.writerow([
                        "frame_id", "segment_id", "frame_in_segment", "segment_file",
                        "source_pts", "source_dts", "source_time_base",
                        "host_receive_monotonic_ns", "host_receive_utc_ns",
                        "segment_origin_monotonic_ns", "requested_local_pts_ms",
                        "packet_size", "packet_sha256",
                    ])

                    for packet in demuxer:
                        mono_ns = time.monotonic_ns()
                        utc_ns = time.time_ns()
                        if stop_requested:
                            reason = "user_stop"
                            break
                        if packet.size == 0:
                            continue
                        if time.monotonic() >= deadline:
                            reason = "duration_limit"
                            break

                        source_pts, source_dts = packet.pts, packet.dts
                        source_tb = str(Fraction(packet.time_base or stream.time_base))
                        size = packet.size
                        sha = hashlib.sha256(bytes(packet)).hexdigest()

                        if output is None or mono_ns - origin_ns >= args.segment_seconds * 1_000_000_000:
                            if output is not None:
                                output.close()
                                output = None
                            segment_id = len(expected)
                            origin_ns = mono_ns
                            filename = f"{args.camera_id}_segment_{segment_id:04d}.mkv"
                            output = av.open(str(folder / filename), mode="w", format="matroska")
                            out_stream = output.add_stream(template=stream)
                            out_stream.time_base = Fraction(1, 1000)
                            expected.append([])
                            report["segments"].append({
                                "file": filename, "origin_host_monotonic_ns": origin_ns,
                            })
                            print(f"Opened {filename}", flush=True)

                        local_pts = (mono_ns - origin_ns) // 1_000_000
                        packet.stream = out_stream
                        packet.time_base = Fraction(1, 1000)
                        packet.pts = local_pts
                        packet.dts = local_pts
                        output.mux(packet)
                        writer.writerow([
                            count, segment_id, len(expected[segment_id]), filename,
                            source_pts, source_dts, source_tb, mono_ns, utc_ns,
                            origin_ns, local_pts, size, sha,
                        ])
                        expected[segment_id].append((sha, local_pts))
                        count += 1
                        packet = None
                    else:
                        raise RuntimeError("Camera stream ended unexpectedly")
            finally:
                packet = None
                if demuxer is not None:
                    demuxer.close()
                if output is not None:
                    closing = output
                    output = None
                    closing.close()

        all_ok = count > 0
        for i, segment in enumerate(report["segments"]):
            actual = []
            with av.open(str(folder / segment["file"])) as check:
                video = check.streams.video[0]
                for packet in check.demux(video):
                    if packet.size == 0:
                        continue
                    stamp = None if packet.pts is None else packet.pts * Fraction(packet.time_base or video.time_base)
                    actual.append((hashlib.sha256(bytes(packet)).hexdigest(), stamp))
            wanted = expected[i]
            payload_ok = len(actual) == len(wanted) and all(a[0] == b[0] for a, b in zip(actual, wanted))
            pts_ok = len(actual) == len(wanted) and all(a[1] == Fraction(b[1], 1000) for a, b in zip(actual, wanted))
            segment.update({"written_packets": len(wanted), "read_packets": len(actual),
                            "payload_sha256_match": payload_ok, "output_pts_match": pts_ok})
            all_ok = all_ok and payload_ok and pts_ok
            print(f"{segment['file']}: packets={len(actual)}/{len(wanted)}, payload={payload_ok}, PTS={pts_ok}")

        report.update({"status": "verified" if all_ok else "needs_review",
                       "stop_reason": reason, "written_packets": count})
    except Exception as exc:
        report.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}",
                       "written_packets": count})
        raise
    finally:
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save(report_path, report)
        print(f"Status: {report['status']}")
        print(f"Output: {folder}")

    return 0 if report["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
