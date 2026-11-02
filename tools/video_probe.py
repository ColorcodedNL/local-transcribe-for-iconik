"""Proef voor Refine: hoe snel krijgen we beeld uit een origineel, en met welke middelen.

Alleen lezen, schrijft niets naar Iconik of de opslag. Meet:
  1. wat er in het bestand zit (videocodec, beeld, audiosporen)
  2. welke encoders onze eigen ffmpeg heeft (en een eventuele ffmpeg op deze Mac)
  3. spoelen: hoe lang één beeldje op vijf plekken in het bestand kost
  4. omzetten: hoe snel 10 seconden naar een lichte stream gaan die het appvenster kan afspelen
  5. of er een libmpv op deze Mac staat (bijvoorbeeld in Subtitle Edit)

Gebruikt het access token van de app en ververst het nooit zelf (dat doet de app). macOS kan om
toegang tot de sleutelhanger vragen; eenmalig toestaan is genoeg.

Draaien: uv run tools/video_probe.py <asset-ID of pad naar een bestand>
"""
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "app"))
import transcribe  # noqa: E402

OWN_BIN = HERE / "build" / "ffmpeg-out" / "bin"


class ReadOnlyIconik(transcribe.Iconik):
    """Het token van de app gebruiken zonder het ooit te verversen."""

    def _authorize(self, rejected=None):
        login = transcribe.load_login()
        if not login:
            sys.exit("Niet ingelogd. Log eerst in via de app.")
        if rejected is not None or login["expires_at"] - time.time() < 120:
            sys.exit("Het token van de app is (bijna) verlopen. Open de app even en draai opnieuw.")
        self.session.headers["Authorization"] = f"Bearer {login['access_token']}"


def media_path(arg):
    path = Path(arg).expanduser()
    if path.exists():
        return path
    return Path(transcribe.find_media_file(ReadOnlyIconik(), arg, interactive=False))


def run(command, timeout=120):
    started = time.monotonic()
    result = subprocess.run(command, capture_output=True, timeout=timeout)
    return time.monotonic() - started, result


def encoders(ffmpeg):
    _, result = run([str(ffmpeg), "-hide_banner", "-encoders"])
    text = result.stdout.decode(errors="replace")
    wanted = ["h264_videotoolbox", "hevc_videotoolbox", "libx264", "mpeg4", "mjpeg"]
    return [name for name in wanted if f" {name} " in text]


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = media_path(sys.argv[1])
    ffmpeg, ffprobe = OWN_BIN / "ffmpeg", OWN_BIN / "ffprobe"
    if not ffmpeg.exists():
        sys.exit("Eigen ffmpeg ontbreekt (build/ffmpeg-out); draai eerst scripts/build_ffmpeg.sh")
    print(f"Bestand: {path.name}  ({path.stat().st_size / 1e9:.1f} GB)\n")

    # 1. inhoud
    _, result = run([str(ffprobe), "-v", "error", "-show_entries",
                     "stream=index,codec_type,codec_name,profile,width,height,r_frame_rate,channels,pix_fmt"
                     ":format=duration,format_name", "-of", "json", str(path)])
    info = json.loads(result.stdout or "{}")
    duration = float(info.get("format", {}).get("duration") or 0)
    print(f"Container: {info.get('format', {}).get('format_name')}, duur {duration:.0f} s")
    audio = 0
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            print(f"Video: {s.get('codec_name')} {s.get('profile') or ''} {s.get('width')}x{s.get('height')} "
                  f"{s.get('r_frame_rate')} {s.get('pix_fmt') or ''}")
        elif s.get("codec_type") == "audio":
            audio += 1
            channels = s.get("channels")
    print(f"Audio: {audio} stream(s)" + (f", {channels} kanaal/kanalen per stream" if audio else "") + "\n")
    if not duration:
        sys.exit("Geen duur gevonden; stop.")

    # 2. encoders
    own = encoders(ffmpeg)
    print(f"Encoders in onze ffmpeg: {', '.join(own) or 'geen van de gezochte'}")
    other = shutil.which("ffmpeg")
    other_enc = encoders(other) if other else []
    if other:
        print(f"Encoders in de ffmpeg op deze Mac: {', '.join(other_enc) or 'geen van de gezochte'}")
    else:
        print("Geen andere ffmpeg op deze Mac.")
    print()

    # 3. spoelen
    print("Spoelen (één beeldje op 640 breed):")
    times = []
    for share in (0.1, 0.3, 0.5, 0.7, 0.9):
        t = duration * share
        seconds, result = run([str(ffmpeg), "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                               "-vf", "scale=640:-2", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:"])
        ok = result.returncode == 0 and len(result.stdout) > 1000
        times.append(seconds)
        print(f"  op {t:7.1f} s: {seconds * 1000:6.0f} ms" + ("" if ok else "  MISLUKT"))
    print(f"  gemiddeld {sum(times) / len(times) * 1000:.0f} ms\n")

    # 4. omzetten naar iets wat het venster afspeelt (10 s, 640 breed, met audio van stream 1)
    print("Omzetten van 10 s naar een stream voor het venster (fragmented mp4, 640 breed):")
    start = duration * 0.3
    tries = []
    if "mpeg4" in own:
        tries.append(("onze ffmpeg, mpeg4", ffmpeg, ["-c:v", "mpeg4", "-q:v", "5"]))
    if "h264_videotoolbox" in own:
        tries.append(("onze ffmpeg, h264_videotoolbox", ffmpeg, ["-c:v", "h264_videotoolbox", "-b:v", "2M"]))
    if "h264_videotoolbox" in other_enc:
        tries.append(("andere ffmpeg, h264_videotoolbox", other, ["-c:v", "h264_videotoolbox", "-b:v", "2M"]))
    audio_codec = ["-c:a", "aac", "-b:a", "128k"] if audio else ["-an"]
    with tempfile.TemporaryDirectory() as tmp:
        for label, binary, video in tries:
            out = Path(tmp) / "proef.mp4"
            seconds, result = run([str(binary), "-v", "error", "-y", "-ss", f"{start:.2f}", "-i", str(path),
                                   "-t", "10", "-map", "0:v:0", *(["-map", "0:a:0"] if audio else []),
                                   "-vf", "scale=640:-2", *video, *audio_codec,
                                   "-movflags", "frag_keyframe+empty_moov", str(out)])
            if result.returncode:
                print(f"  {label}: MISLUKT  {result.stderr.decode(errors='replace').strip()[-200:]}")
            else:
                print(f"  {label}: {seconds:.1f} s voor 10 s ({10 / seconds:.1f}x realtime), "
                      f"{out.stat().st_size / 1e6:.1f} MB")
    if not tries:
        print("  geen bruikbare encoder gevonden")
    print()

    # 5. libmpv
    found = []
    for pattern_root, pattern in ((Path("/opt/homebrew/lib"), "libmpv*.dylib"), (Path("/usr/local/lib"), "libmpv*.dylib"),
                                  (Path("/Applications"), "*/Contents/**/libmpv*.dylib")):
        if pattern_root.exists():
            found += [p for p in pattern_root.glob(pattern) if not p.is_symlink()]
    if found:
        print("libmpv gevonden:")
        for p in found[:5]:
            app = next((part for part in p.parts if part.endswith(".app")), None)
            print(f"  {p.name}" + (f"  (in {app})" if app else "  (los geïnstalleerd)"))
    else:
        print("Geen libmpv op deze Mac.")


if __name__ == "__main__":
    main()
