import argparse
import os.path
import os
import shutil
import subprocess
import sys

def emptyFolder(folder):
    for filename in os.listdir(folder):
        file_path = os.path.join(folder, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            print('Failed to delete %s. Reason: %s' % (file_path, e))
    try:
        os.rmdir(folder)
    except Exception as e:
        print('Failed to delete %s. Reason: %s' % (folder, e))


parser = argparse.ArgumentParser(description='blah')
parser.add_argument('--input_file', type=str,  help='the script')
parser.add_argument('--keep_frames', type=str,  help='do you want to keep the thousands of frame still images, or delete them?')
parser.add_argument('--aspect', type=str, default='16:9', help='16:9 (default) or 9:16; frames already define the size')
parser.add_argument('--cover', type=str, default='', help='full-bleed title-card still prepended as the first clip (no character overlay)')
parser.add_argument('--cover_seconds', type=float, default=5, help='how long the title-card still plays (default 5)')
parser.add_argument('--frames_dir', type=str, default='', help='folder of f######.png (aspect-specific)')
parser.add_argument('--output', type=str, default='', help='aspect-specific mp4 path (script_final_16x9.mp4 / script_final_9x16.mp4)')
args = parser.parse_args()
INPUT_FILE = args.input_file
KEEP_FRAMES = args.keep_frames
ASPECT = (args.aspect or "16:9").strip().lower().replace("x", ":")
if ASPECT in ("9:16", "portrait", "vertical"):
    COVER_W, COVER_H = 1080, 1920
else:
    COVER_W, COVER_H = 1920, 1080
COVER_SECONDS = args.cover_seconds if args.cover_seconds and args.cover_seconds > 0 else 5
COVER_MS = int(round(COVER_SECONDS * 1000))
cover_path = (args.cover or "").strip() or (INPUT_FILE + "_cover.png")
FRAMES_DIR = (args.frames_dir or "").strip() or (INPUT_FILE + "_frames")
OUTPUT_FILE = (args.output or "").strip() or (INPUT_FILE + "_final.mp4")
cover_video = os.path.splitext(cover_path)[0] + ".mp4"
COVER_VIDEO = os.path.isfile(cover_video) and os.path.getsize(cover_video) > 1000
if not os.path.isfile(cover_path):
    print(
        f"ERROR: title-card cover missing at {cover_path}. "
        "Refusing to invent a black 5s clip. Generate the aspect cover first "
        "(script_cover_16x9.png or script_cover_9x16.png).",
        file=sys.stderr,
    )
    sys.exit(1)

wav_path = INPUT_FILE + ".wav"
if not os.path.isfile(wav_path):
    wav_path = INPUT_FILE + ".mp3"

if COVER_VIDEO:
    print(f"Prepending {COVER_SECONDS:g}s title-card clip (no character overlay): {cover_video}")
else:
    print(f"Prepending {COVER_SECONDS:g}s title-card still (no character overlay): {cover_path}")

def _cover_inputs():
    if COVER_VIDEO:
        return ["-stream_loop", "-1", "-t", str(COVER_SECONDS), "-i", cover_video]
    return ["-loop", "1", "-framerate", "30", "-t", str(COVER_SECONDS), "-i", cover_path]

def _resolve_ffmpeg() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    from studio.ffmpeg_bin import resolve_ffmpeg
    return resolve_ffmpeg()

def _mux(audio_filter):
    # Ensure repo root is importable before studio.* imports.
    _resolve_ffmpeg()
    from studio.deps_health import detect_video_encoder

    enc = detect_video_encoder()
    encoder = enc.get("encoder") or "libx264"
    preset = enc.get("preset") or "veryfast"
    # CPU guard (Ely 2026-09-23): cap ffmpeg at ~1 core so the VPS stays under Hostinger CPU limit.
    # Override with env BUBBLEPOD_FFMPEG_THREADS (e.g. "2") without a code change.
    _threads = os.environ.get("BUBBLEPOD_FFMPEG_THREADS", "1")
    # Expose for parent pipeline job status
    try:
        import os as _os

        _os.environ["BUBBLEPOD_LAST_VIDEO_ENCODER"] = str(encoder)
        _os.environ["BUBBLEPOD_LAST_VIDEO_PRESET"] = str(preset)
    except Exception:
        pass
    vf = (
        f"[0:v]scale={COVER_W}:{COVER_H}:force_original_aspect_ratio=increase,"
        f"crop={COVER_W}:{COVER_H},fps=30,format=yuv420p,setsar=1,setpts=PTS-STARTPTS[cover];"
        f"[1:v]fps=30,format=yuv420p,setsar=1,setpts=PTS-STARTPTS[main];"
        f"[cover][main]concat=n=2:v=1:a=0[v];"
        f"{audio_filter}"
    )
    command = [
        _resolve_ffmpeg(), "-y",
        "-threads", _threads, "-filter_threads", _threads,
        *_cover_inputs(),
        "-framerate", "30",
        "-i", os.path.join(FRAMES_DIR, "f%06d.png"),
        "-i", wav_path,
        "-filter_complex", vf,
        "-map", "[v]", "-map", "[a]",
        "-c:v", encoder,
    ]
    if encoder == "h264_nvenc":
        command.extend(["-preset", preset, "-b:v", "4M", "-pix_fmt", "yuv420p"])
    else:
        command.extend(["-preset", preset, "-pix_fmt", "yuv420p", "-b:v", "4M"])
    command.extend(
        [
            "-c:a", "aac",
            "-shortest",
            OUTPUT_FILE,
        ]
    )
    try:
        subprocess.check_call(command)
    except subprocess.CalledProcessError:
        if encoder != "libx264":
            # Fall back to CPU encode if NVENC fails at runtime.
            print(f"{encoder} failed; falling back to libx264", file=sys.stderr)
            command = [
                _resolve_ffmpeg(), "-y",
        "-threads", _threads, "-filter_threads", _threads,
                *_cover_inputs(),
                "-framerate", "30",
                "-i", os.path.join(FRAMES_DIR, "f%06d.png"),
                "-i", wav_path,
                "-filter_complex", vf,
                "-map", "[v]", "-map", "[a]",
                "-vcodec", "libx264",
                "-preset", "veryfast",
                "-pix_fmt", "yuv420p",
                "-b:v", "4M",
                "-c:a", "aac",
                "-shortest",
                OUTPUT_FILE,
            ]
            try:
                import os as _os

                _os.environ["BUBBLEPOD_LAST_VIDEO_ENCODER"] = "libx264"
                _os.environ["BUBBLEPOD_LAST_VIDEO_PRESET"] = "veryfast"
            except Exception:
                pass
            subprocess.check_call(command)
        else:
            raise

try:
    _mux(f"[2:a]adelay={COVER_MS}:all=1,asetpts=PTS-STARTPTS[a]")
except subprocess.CalledProcessError:
    print("ffmpeg adelay=all failed; retrying stereo adelay", file=sys.stderr)
    _mux(f"[2:a]adelay={COVER_MS}|{COVER_MS},asetpts=PTS-STARTPTS[a]")

if KEEP_FRAMES == "F":
    emptyFolder(FRAMES_DIR)
