"""Build a silent 60-second Korean-captioned demo video from real app screenshots.

Usage (from the academic edition root):
  python tools/build_demo_video.py --ffmpeg C:/path/to/ffmpeg.exe

Place ``storyboard.json`` and PNG screenshots in ``deliverables/demo_video``.
Each scene is ``{"file": "01.png", "title": "...", "lines": [...],
"duration": 12}``. The source screenshots are never modified. The script
creates temporary fitted frames, a silent 1920x1080 H.264 MP4, subtitles.srt,
and video_metadata.json. It is intentionally separate from product dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
VIDEO_DIR = ROOT / "deliverables" / "demo_video"
WIDTH, HEIGHT, FPS = 1920, 1080, 30
NAVY = (10, 25, 45)
WHITE = (245, 248, 252)
MUTED = (174, 194, 214)
ACCENT = (67, 181, 157)


def font(size: int, bold: bool = False):
    candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / ("malgunbd.ttf" if bold else "malgun.ttf"),
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeui.ttf",
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    whole, millis = divmod(total_ms, 1000)
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d},{millis:03d}"


def wrap_text(draw: ImageDraw.ImageDraw, text: str, text_font, max_width: int) -> list[str]:
    words = str(text).split()
    if not words:
        return [""]
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and draw.textbbox((0, 0), candidate, font=text_font)[2] > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def render(source: Path, destination: Path, title: str, lines: list[str], step: int, total: int) -> None:
    with Image.open(source) as raw:
        image = raw.convert("RGB")
        image.thumbnail((1450, 970), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (WIDTH, HEIGHT), NAVY)
        x = (1450 - image.width) // 2
        y = (HEIGHT - image.height) // 2
        canvas.paste(image, (x, y))
        draw = ImageDraw.Draw(canvas)
        panel_x = 1475
        draw.text((panel_x, 90), "SleepWell", fill=WHITE, font=font(42, True))
        draw.text((panel_x, 145), "연구자용 소프트웨어", fill=ACCENT, font=font(25, True))
        draw.text((panel_x, 245), f"{step:02d} / {total:02d}", fill=MUTED, font=font(22, True))
        title_font = font(30, True)
        body_font = font(22)
        top = 300
        for wrapped in wrap_text(draw, title, title_font, 420):
            draw.text((panel_x, top), wrapped, fill=WHITE, font=title_font, spacing=8)
            top += 42
        top += 48
        for line in lines:
            for wrapped in wrap_text(draw, line, body_font, 420):
                draw.text((panel_x, top), wrapped, fill=MUTED, font=body_font, spacing=7)
                top += 32
            top += 18
        draw.text((panel_x, 900), "실제 실행 화면 편집본 · 무음", fill=ACCENT, font=font(18))
        draw.text((panel_x, 932), "50일 합성 샘플 · 실제 연구 성능 아님", fill=ACCENT, font=font(16))
    canvas.save(destination, format="PNG", optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--storyboard", type=Path, default=VIDEO_DIR / "storyboard.json")
    parser.add_argument("--output", type=Path, default=VIDEO_DIR / "SleepWell_60s_demo_ko.mp4")
    args = parser.parse_args()
    ffmpeg = args.ffmpeg.resolve()
    if not ffmpeg.is_file():
        raise FileNotFoundError(f"ffmpeg not found: {ffmpeg}")
    storyboard = json.loads(args.storyboard.read_text(encoding="utf-8"))
    scenes = storyboard.get("scenes", storyboard) if isinstance(storyboard, dict) else storyboard
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("storyboard.json must contain a non-empty scenes array")
    durations = [float(scene["duration"]) for scene in scenes]
    if any(value <= 0 for value in durations) or abs(sum(durations) - 60.0) > 1e-6:
        raise ValueError(f"scene durations must sum exactly 60 seconds: {sum(durations)}")
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sleepwell-video-") as temporary:
        temp = Path(temporary)
        concat = temp / "frames.txt"
        entries = []
        elapsed = 0.0
        subtitles = []
        for index, scene in enumerate(scenes, 1):
            source = (args.storyboard.parent / scene["file"]).resolve()
            if not source.is_file():
                raise FileNotFoundError(source)
            rendered = temp / f"scene_{index:02d}.png"
            lines = [str(line) for line in scene.get("lines", [])]
            render(source, rendered, str(scene.get("title", "")), lines, index, len(scenes))
            safe_path = rendered.as_posix().replace("'", "'\\''")
            entries.append(f"file '{safe_path}'\nduration {durations[index - 1]:.6f}")
            start, end = timestamp(elapsed), timestamp(elapsed + durations[index - 1] - 0.001)
            text = " / ".join([str(scene.get("title", "")), *lines]).strip(" /")
            subtitles.append(f"{index}\n{start} --> {end}\n{text}\n")
            elapsed += durations[index - 1]
        # concat requires the final frame repeated to honor the final duration.
        entries.append(entries[-1].split("\nduration")[0])
        concat.write_text("\n".join(entries) + "\n", encoding="utf-8")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        command = [str(ffmpeg), "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
                   "-an", "-r", str(FPS), "-frames:v", str(60 * FPS), "-c:v", "libx264", "-preset", "fast",
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(args.output)]
        diagnostic = subprocess.run(command, check=True, capture_output=True, text=True)
    srt = args.output.with_name("subtitles.srt")
    srt.write_text("\n".join(subtitles) + "\n", encoding="utf-8")
    probe = subprocess.run([str(ffmpeg), "-hide_banner", "-i", str(args.output)], capture_output=True, text=True)
    probe_text = probe.stderr
    duration_match = re.search(r"Duration:\s+(\d+):(\d+):(\d+\.\d+)", probe_text)
    video_match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5}).*?(\d+(?:\.\d+)?) fps", probe_text, re.S)
    measured_duration = None
    measured_width = measured_height = measured_fps = None
    if duration_match:
        h, m, s = duration_match.groups()
        measured_duration = int(h) * 3600 + int(m) * 60 + float(s)
    if video_match:
        measured_width, measured_height, measured_fps = int(video_match.group(1)), int(video_match.group(2)), float(video_match.group(3))
    metadata = {"output": str(args.output), "duration_seconds": 60, "measured_duration_seconds": measured_duration,
                "width": WIDTH, "height": HEIGHT, "measured_width": measured_width,
                "measured_height": measured_height, "fps": FPS, "measured_fps": measured_fps,
                "codec": "H.264/libx264", "audio": False,
                "scenes": scenes, "ffmpeg": str(ffmpeg)}
    args.output.with_name("video_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    smoke = subprocess.run([str(ffmpeg), "-v", "error", "-i", str(args.output), "-f", "null", "-"], capture_output=True, text=True)
    if smoke.returncode:
        raise RuntimeError(smoke.stderr)
    frame_check = subprocess.run([str(ffmpeg), "-hide_banner", "-i", str(args.output), "-vf", "showinfo", "-an", "-f", "null", "-"], capture_output=True, text=True)
    measured_frames = len(re.findall(r"showinfo.*\bn:\s*\d+", frame_check.stderr))
    metadata["measured_frames"] = measured_frames
    args.output.with_name("video_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    if measured_duration is None or abs(measured_duration - 60.0) > 0.05 or measured_width != WIDTH or measured_height != HEIGHT or measured_frames != 60 * FPS:
        raise RuntimeError(f"output validation failed: duration={measured_duration}, size={measured_width}x{measured_height}")
    print(json.dumps(metadata, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
