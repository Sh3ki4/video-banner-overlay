#!/usr/bin/env python3
"""Пакетное наложение рекламного видео поверх локальных MP4."""

from __future__ import print_function

import argparse
import json
import os
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = APP_DIR / "INPUT"
DEFAULT_OUTPUT = APP_DIR / "OUTPUT"
DEFAULT_BANNER = APP_DIR / "banner.mp4"


class AppError(RuntimeError):
    pass


def configure_console():
    """По возможности включает корректный вывод кириллицы в новых версиях Python."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def executable_candidates(name):
    exe_name = name + ".exe" if os.name == "nt" else name
    env_name = "FFMPEG_PATH" if name == "ffmpeg" else "FFPROBE_PATH"
    candidates = []

    env_value = os.environ.get(env_name)
    if env_value:
        env_path = Path(env_value).expanduser()
        candidates.append(env_path / exe_name if env_path.is_dir() else env_path)

    candidates.extend(
        [
            APP_DIR / exe_name,
            APP_DIR / "ffmpeg" / "bin" / exe_name,
            APP_DIR / "tools" / "ffmpeg" / "bin" / exe_name,
        ]
    )

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        program_data = os.environ.get("ProgramData")
        program_files = os.environ.get("ProgramFiles")
        if local_app_data:
            candidates.append(Path(local_app_data) / "Microsoft" / "WinGet" / "Links" / exe_name)
        if program_data:
            candidates.append(Path(program_data) / "chocolatey" / "bin" / exe_name)
        if program_files:
            candidates.append(Path(program_files) / "ffmpeg" / "bin" / exe_name)

    from_path = shutil.which(name)
    if from_path:
        candidates.append(Path(from_path))
    return candidates


def find_ffmpeg_tools():
    ffmpeg = next((p.resolve() for p in executable_candidates("ffmpeg") if p.is_file()), None)
    ffprobe = next((p.resolve() for p in executable_candidates("ffprobe") if p.is_file()), None)

    if ffmpeg and not ffprobe:
        sibling = ffmpeg.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if sibling.is_file():
            ffprobe = sibling.resolve()
    if ffprobe and not ffmpeg:
        sibling = ffprobe.with_name("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if sibling.is_file():
            ffmpeg = sibling.resolve()

    if not ffmpeg or not ffprobe:
        raise AppError(
            "Не найдены FFmpeg и FFprobe. Положите ffmpeg.exe и ffprobe.exe рядом "
            "с ЗАПУСТИТЬ.bat (или в папку ffmpeg\\bin), либо добавьте FFmpeg в PATH."
        )
    return ffmpeg, ffprobe


def run_json(command):
    completed = subprocess.run(
        [str(item) for item in command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or "неизвестная ошибка FFprobe"
        raise AppError(message)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AppError("FFprobe вернул некорректные данные: {}".format(exc))


def positive_float(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def stream_duration(stream, format_info):
    direct = positive_float(stream.get("duration"))
    if direct:
        return direct
    duration_ts = positive_float(stream.get("duration_ts"))
    time_base = stream.get("time_base")
    if duration_ts and time_base and time_base != "0/0":
        try:
            calculated = duration_ts * float(Fraction(time_base))
            if calculated > 0:
                return calculated
        except (ValueError, ZeroDivisionError):
            pass
    return positive_float(format_info.get("duration"))


def rotation_of(stream):
    try:
        rotation = int(float(stream.get("tags", {}).get("rotate", 0)))
    except (TypeError, ValueError):
        rotation = 0
    for side_data in stream.get("side_data_list", []):
        if "rotation" in side_data:
            try:
                rotation = int(float(side_data["rotation"]))
            except (TypeError, ValueError):
                pass
    return rotation % 360


def normalized_fps(stream):
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key)
        if not raw or raw == "0/0":
            continue
        try:
            fps = Fraction(raw)
            if 0 < float(fps) <= 240:
                return "{}/{}".format(fps.numerator, fps.denominator)
        except (ValueError, ZeroDivisionError):
            continue
    return "30/1"


def probe_media(ffprobe, path):
    data = run_json(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-print_format",
            "json",
            path,
        ]
    )
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise AppError("В файле нет видеопотока: {}".format(path.name))
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = stream_duration(video, data.get("format", {}))
    if not duration:
        raise AppError("Не удалось определить длительность: {}".format(path.name))

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise AppError("Не удалось определить разрешение: {}".format(path.name))
    if rotation_of(video) in (90, 270):
        width, height = height, width

    # H.264/yuv420p требует чётные размеры. При нечётном исходнике добавляется 1 пиксель.
    width += width % 2
    height += height % 2
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": normalized_fps(video),
        "has_audio": audio is not None,
    }


def number(value):
    return "{:.6f}".format(value).rstrip("0").rstrip(".")


def silent_audio(duration, output):
    return (
        "anullsrc=r=48000:cl=stereo,atrim=duration={duration},"
        "asetpts=PTS-STARTPTS[{output}]"
    ).format(duration=number(duration), output=output)


def even_floor(value):
    return max(2, int(value) // 2 * 2)


def overlay_dimensions(source, banner, width_percent, height_percent):
    max_width = even_floor(source["width"] * width_percent / 100.0)
    max_height = even_floor(source["height"] * height_percent / 100.0)
    scale = min(max_width / float(banner["width"]), max_height / float(banner["height"]))
    return even_floor(banner["width"] * scale), even_floor(banner["height"] * scale)


def build_filter(
    source,
    banner,
    position,
    width_percent,
    height_percent,
    bottom_percent,
    background_volume,
):
    source_duration = source["duration"]
    start = source_duration * position
    active_duration = min(banner["duration"], source_duration - start)
    if active_duration <= 0:
        raise AppError("Для баннера не осталось времени в исходном ролике.")
    end = start + active_duration
    width, height, fps = source["width"], source["height"], source["fps"]
    overlay_width, overlay_height = overlay_dimensions(
        source, banner, width_percent, height_percent
    )
    overlay_bottom = int(height * bottom_percent / 100.0)
    overlay_y = max(0, min(height - overlay_height, overlay_bottom - overlay_height))

    parts = [
        "[0:v:0]trim=duration={duration},setpts=PTS-STARTPTS,fps={fps},"
        "scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
        "format=yuv420p,settb=AVTB[base_v]".format(
            duration=number(source_duration),
            fps=fps,
            width=width,
            height=height,
        )
    ]
    parts.append(
        "[1:v:0]trim=duration={duration},setpts=PTS-STARTPTS+{start}/TB,"
        "fps={fps},scale={overlay_width}:{overlay_height}:"
        "force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,"
        "format=yuv420p,settb=AVTB[banner_v]".format(
            duration=number(active_duration),
            start=number(start),
            fps=fps,
            overlay_width=overlay_width,
            overlay_height=overlay_height,
        )
    )
    parts.append(
        "[base_v][banner_v]overlay=x=(W-w)/2:y={y}:eof_action=pass:repeatlast=0:"
        "enable='between(t,{start},{end})',format=yuv420p[vout]".format(
            y=overlay_y,
            start=number(start),
            end=number(end),
        )
    )

    needs_audio = source["has_audio"] or banner["has_audio"]
    if needs_audio:
        if source["has_audio"]:
            parts.append(
                "[0:a:0]aresample=48000:async=1:first_pts=0,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                "apad=whole_dur={duration},atrim=duration={duration},"
                "asetpts=PTS-STARTPTS,"
                "volume='if(between(t,{start},{end}),{volume},1)':eval=frame[a_bg]".format(
                    duration=number(source_duration),
                    start=number(start),
                    end=number(end),
                    volume=number(background_volume),
                )
            )
        else:
            parts.append(silent_audio(source_duration, "a_bg"))

        if banner["has_audio"]:
            delay_ms = max(0, int(round(start * 1000)))
            parts.append(
                "[1:a:0]atrim=duration={active_duration},"
                "aresample=48000:async=1:first_pts=0,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                "apad=whole_dur={active_duration},atrim=duration={active_duration},"
                "asetpts=PTS-STARTPTS,adelay={delay}:all=1,"
                "apad=whole_dur={source_duration},atrim=duration={source_duration}[a_banner]".format(
                    active_duration=number(active_duration),
                    delay=delay_ms,
                    source_duration=number(source_duration),
                )
            )
            parts.append(
                "[a_bg][a_banner]amix=inputs=2:duration=first:dropout_transition=0:"
                "normalize=0,alimiter=limit=0.95[aout]"
            )
        else:
            parts.append("[a_bg]anull[aout]")

    return (
        ";".join(parts),
        needs_audio,
        start,
        end,
        overlay_width,
        overlay_height,
        overlay_y,
    )


def print_progress(process, expected_duration):
    last_percent = -1
    while True:
        line = process.stdout.readline()
        if not line:
            if process.poll() is not None:
                break
            continue
        line = line.strip()
        if line.startswith("out_time_ms="):
            try:
                seconds = int(line.split("=", 1)[1]) / 1000000.0
                percent = min(100, int(seconds * 100 / expected_duration))
            except (ValueError, ZeroDivisionError):
                continue
            if percent >= last_percent + 2:
                print("\r    Прогресс: {:3d}%".format(percent), end="", flush=True)
                last_percent = percent
    print("\r    Прогресс: 100%")


def encode(ffmpeg, source_path, banner_path, temp_path, filter_graph, with_audio, expected):
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        "-y",
        "-fflags",
        "+genpts",
        "-i",
        str(source_path),
        "-i",
        str(banner_path),
        "-filter_complex",
        filter_graph,
        "-map",
        "[vout]",
    ]
    if with_audio:
        command.extend(["-map", "[aout]"])
    command.extend(
        [
            "-map_metadata",
            "0",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
        ]
    )
    if with_audio:
        command.extend(["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"])
    command.extend(
        [
            "-max_muxing_queue_size",
            "2048",
            "-movflags",
            "+faststart",
            "-metadata:s:v:0",
            "rotate=0",
            "-t",
            number(expected),
            str(temp_path),
        ]
    )

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    try:
        print_progress(process, expected)
        stderr = process.stderr.read()
        return_code = process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        raise
    if return_code != 0:
        raise AppError(stderr.strip() or "FFmpeg завершился с кодом {}".format(return_code))


def validate_result(ffprobe, output_path, expected_duration, expected_width, expected_height, audio_expected):
    result = probe_media(ffprobe, output_path)
    tolerance = max(0.75, expected_duration * 0.02)
    if abs(result["duration"] - expected_duration) > tolerance:
        raise AppError(
            "Проверка результата не пройдена: ожидалась длительность около {:.2f} с, получено {:.2f} с."
            .format(expected_duration, result["duration"])
        )
    if (result["width"], result["height"]) != (expected_width, expected_height):
        raise AppError("Проверка результата не пройдена: изменилось разрешение.")
    if audio_expected and not result["has_audio"]:
        raise AppError("Проверка результата не пройдена: отсутствует аудиодорожка.")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Накладывает banner.mp4 поверх каждого MP4 из папки INPUT."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="папка с исходными MP4")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="папка для результатов")
    parser.add_argument("--banner", type=Path, default=DEFAULT_BANNER, help="путь к рекламному MP4")
    parser.add_argument(
        "--position-percent",
        type=float,
        default=50.0,
        help="момент появления баннера в процентах от исходника (по умолчанию 50)",
    )
    parser.add_argument(
        "--banner-width-percent",
        type=float,
        default=94.0,
        help="максимальная ширина баннера в процентах кадра (по умолчанию 94)",
    )
    parser.add_argument(
        "--banner-height-percent",
        type=float,
        default=24.0,
        help="максимальная высота баннера в процентах кадра (по умолчанию 24)",
    )
    parser.add_argument(
        "--banner-bottom-percent",
        type=float,
        default=72.0,
        help="нижняя граница баннера в процентах высоты кадра (по умолчанию 72)",
    )
    parser.add_argument(
        "--background-volume-percent",
        type=float,
        default=35.0,
        help="громкость исходника во время баннера (по умолчанию 35)",
    )
    return parser.parse_args()


def main():
    configure_console()
    print("=== VIDEO BANNER OVERLAY 2.1 ===")
    print("Режим: видео идёт на фоне, баннер показывается ЦЕЛИКОМ без обрезки.")
    args = parse_args()
    input_dir = args.input.expanduser().resolve()
    output_dir = args.output.expanduser().resolve()
    banner_path = args.banner.expanduser().resolve()

    if not 5 <= args.position_percent <= 95:
        raise AppError("--position-percent должен быть от 5 до 95.")
    if not 10 <= args.banner_width_percent <= 100:
        raise AppError("--banner-width-percent должен быть от 10 до 100.")
    if not 5 <= args.banner_height_percent <= 60:
        raise AppError("--banner-height-percent должен быть от 5 до 60.")
    if not 20 <= args.banner_bottom_percent <= 100:
        raise AppError("--banner-bottom-percent должен быть от 20 до 100.")
    if not 0 <= args.background_volume_percent <= 100:
        raise AppError("--background-volume-percent должен быть от 0 до 100.")
    position = args.position_percent / 100.0
    background_volume = args.background_volume_percent / 100.0

    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    if not banner_path.is_file():
        alternate = input_dir / "banner.mp4"
        if alternate.is_file():
            banner_path = alternate.resolve()
        else:
            raise AppError("Не найден banner.mp4 рядом с программой или в папке INPUT.")

    ffmpeg, ffprobe = find_ffmpeg_tools()
    print("FFmpeg найден: {}".format(ffmpeg))
    print("Проверяю рекламный ролик...")
    banner = probe_media(ffprobe, banner_path)
    if not 2.5 <= banner["duration"] <= 5.0:
        print(
            "Внимание: длительность banner.mp4 — {:.2f} с (рекомендуется 3–4 с).".format(
                banner["duration"]
            )
        )

    videos = sorted(
        (
            path
            for path in input_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".mp4" and path.resolve() != banner_path
        ),
        key=lambda item: item.name.lower(),
    )
    if not videos:
        raise AppError("В папке INPUT нет исходных MP4.")

    print("Найдено исходных видео: {}".format(len(videos)))
    succeeded = 0
    failed = []
    for index, source_path in enumerate(videos, 1):
        output_path = output_dir / source_path.name
        temp_path = output_dir / ("." + source_path.stem + ".processing.mp4")
        print("\n[{}/{}] {}".format(index, len(videos), source_path.name))
        try:
            source = probe_media(ffprobe, source_path)
            if source["duration"] < 0.5:
                raise AppError("Исходный ролик слишком короткий.")
            (
                filter_graph,
                with_audio,
                start,
                end,
                overlay_width,
                overlay_height,
                overlay_y,
            ) = build_filter(
                source,
                banner,
                position,
                args.banner_width_percent,
                args.banner_height_percent,
                args.banner_bottom_percent,
                background_volume,
            )
            expected = source["duration"]
            print(
                "    Баннер: {:.2f}–{:.2f} с | размер: {}x{} | позиция Y: {}".format(
                    start, end, overlay_width, overlay_height, overlay_y
                )
            )
            print(
                "    Фоновый звук во время баннера: {:.0f}% | длительность не меняется".format(
                    args.background_volume_percent
                )
            )
            if temp_path.exists():
                temp_path.unlink()
            encode(
                ffmpeg,
                source_path,
                banner_path,
                temp_path,
                filter_graph,
                with_audio,
                expected,
            )
            validate_result(
                ffprobe,
                temp_path,
                expected,
                source["width"],
                source["height"],
                with_audio,
            )
            os.replace(str(temp_path), str(output_path))
            print("    Готово: {}".format(output_path.name))
            succeeded += 1
        except KeyboardInterrupt:
            if temp_path.exists():
                temp_path.unlink()
            print("\nОстановлено пользователем.")
            return 130
        except Exception as exc:
            if temp_path.exists():
                temp_path.unlink()
            failed.append((source_path.name, str(exc)))
            print("    ОШИБКА: {}".format(exc))

    print("\n" + "=" * 58)
    print("Готово: {} из {}. Результаты: {}".format(succeeded, len(videos), output_dir))
    if failed:
        print("Не обработано: {}".format(len(failed)))
        for filename, message in failed:
            print("  - {}: {}".format(filename, message))
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AppError as exc:
        configure_console()
        print("\nОШИБКА: {}".format(exc), file=sys.stderr)
        sys.exit(1)
