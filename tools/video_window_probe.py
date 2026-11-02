"""Proef voor Refine, deel 2: speelt het appvenster (WebKit) een origineel af dat ffmpeg tijdens het kijken omzet?

Opent een venster zoals de app (pywebview). Een kleine server op deze Mac zet stukjes van 6 seconden om
(fragmented mp4) en het venster speelt ze achter elkaar af (Media Source Extensions). Twee varianten:
  mpeg4  onze eigen ffmpeg (zit al in de app)
  h264   H.264 via VideoToolbox, met de ffmpeg die op deze Mac staat (alleen ter vergelijking)

Het venster laat zien welke codecs WebKit aanneemt en hoe lang spoelen duurt tot het beeld loopt.
Alles wat er gemeten wordt komt ook in de terminal, om te plakken.

Alleen lezen. Draaien: uv run tools/video_window_probe.py <asset-ID of pad naar een bestand>
"""
import json
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import video_probe  # noqa: E402
import webview  # noqa: E402

CHUNK = 6
PATH = None
DURATION = 0.0
AUDIO = 0
BINARIES = {}

PAGE = """<!doctype html><meta charset=utf-8><title>Videoproef</title>
<style>body{font:13px -apple-system;background:#1b1b1d;color:#ddd;margin:12px}video{width:100%;background:#000}
button,select{font:inherit;margin:2px}#log{white-space:pre;font:12px ui-monospace;max-height:220px;overflow:auto}</style>
<video id=v controls muted></video>
<div><select id=enc></select> <select id=trk></select>
<button onclick="seekTo(dur*0.1)">10%</button><button onclick="seekTo(dur*0.5)">50%</button>
<button onclick="seekTo(dur*0.9)">90%</button><button onclick="seekTo(Math.random()*dur*0.95)">willekeurig</button>
<button onclick="seekTo(v.currentTime-5)">-5 s</button><button onclick="seekTo(v.currentTime+5)">+5 s</button>
<button onclick="v.muted=!v.muted">geluid aan/uit</button></div><div id=log></div>
<script>
const info = __INFO__, dur = info.duration, v = document.getElementById('v');
const types = {mpeg4: 'video/mp4; codecs="mp4v.20.9, mp4a.40.2"', h264: 'video/mp4; codecs="avc1.640028, mp4a.40.2"'};
function log(s){ document.getElementById('log').textContent += s + "\\n"; fetch('/log', {method:'POST', body:s}); }
for (const [k,t] of Object.entries(types)) log(`WebKit ${k}: MSE ${window.MediaSource && MediaSource.isTypeSupported(t) ? 'ja' : 'nee'}, video ${v.canPlayType(t) || 'nee'}`);
const enc = document.getElementById('enc'), trk = document.getElementById('trk');
for (const k of info.encoders) enc.add(new Option(k, k));
for (let i = 0; i < Math.max(info.audio, 1); i++) trk.add(new Option('audiospoor ' + (i + 1), i));
enc.onchange = () => start(v.currentTime || dur * 0.3); trk.onchange = enc.onchange;
let ms, sb, gen = 0, target = null, next = 0, seekStart = 0, running = false, stalls = 0;
function waitIdle(){ return sb.updating ? new Promise(r => sb.addEventListener('updateend', r, {once:true})) : Promise.resolve(); }
// Alles wat de buffer aanraakt loopt in deze ene lus, in vaste volgorde: nooit twee dingen tegelijk.
function seekTo(t){
  target = Math.max(0, Math.min(t, dur - 1)); gen++; seekStart = performance.now();
  log(`spoelen naar ${target.toFixed(1)} s (${enc.value})`); kick();
}
async function kick(){
  if (running) return; running = true;
  try { await loop(); } catch (e) { log('fout: ' + e); }
  finally { running = false; }
  if (target !== null) kick();
}
async function loadChunk(my){
  const t0 = performance.now();
  const r = await fetch(`/chunk?t=${next.toFixed(3)}&d=${info.chunk}&enc=${enc.value}&a=${trk.value}`);
  const buf = await r.arrayBuffer();
  if (my !== gen) return false;
  if (!r.ok) { log('stukje mislukt: ' + new TextDecoder().decode(buf).slice(-200)); target = null; return false; }
  await waitIdle(); sb.appendBuffer(buf); await waitIdle();
  log(`  stukje ${next.toFixed(0)} s: ${(performance.now() - t0).toFixed(0)} ms (ffmpeg ${r.headers.get('X-Took')} ms, ${(buf.byteLength/1e6).toFixed(1)} MB)`);
  next += info.chunk; return true;
}
async function loop(){
  while (sb && ms.readyState === 'open') {
    if (target !== null) {
      const t = target, my = gen; target = null;
      await waitIdle();
      if (sb.buffered.length) { sb.remove(0, dur + 1); await waitIdle(); }
      sb.timestampOffset = t; next = t;
      if (!await loadChunk(my) || my !== gen) continue;
      v.currentTime = t;
      const shown = () => { if (my === gen && seekStart) { log(`  beeld na ${(performance.now() - seekStart).toFixed(0)} ms`); seekStart = 0; } };
      if (v.requestVideoFrameCallback) v.requestVideoFrameCallback(shown); else v.addEventListener('seeked', shown, {once:true});
      v.play().catch(() => {});
      continue;
    }
    const b = sb.buffered, end = b.length ? b.end(b.length - 1) : next;
    if (next >= dur || end - v.currentTime > 14) return;
    await loadChunk(gen);
  }
}
v.addEventListener('timeupdate', kick);
v.addEventListener('waiting', () => { if (!seekStart && !v.seeking) log(`  hapering ${++stalls} op ${v.currentTime.toFixed(1)} s`); });
v.addEventListener('error', () => log('videofout: ' + (v.error && v.error.message)));
function start(t){
  gen++; target = null; sb = null; ms = new MediaSource(); v.src = URL.createObjectURL(ms);
  ms.addEventListener('sourceopen', () => {
    if (!MediaSource.isTypeSupported(types[enc.value])) { log(enc.value + ' wordt niet aangenomen; kies de andere'); return; }
    ms.duration = dur;   // anders zet WebKit de afspeelpositie terug op 0
    sb = ms.addSourceBuffer(types[enc.value]); sb.mode = 'sequence'; seekTo(t);
  }, {once:true});
}
start(dur * 0.3);
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, kind="text/plain", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/log":
            print(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode(), flush=True)
        self.send(204, b"")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            info = {"duration": DURATION, "audio": AUDIO, "chunk": CHUNK, "encoders": list(BINARIES)}
            return self.send(200, PAGE.replace("__INFO__", json.dumps(info)).encode(), "text/html; charset=utf-8")
        if url.path != "/chunk":
            return self.send(404, b"")
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        enc = q.get("enc", "mpeg4")
        video = (["-c:v", "mpeg4", "-q:v", "4", "-g", "25"] if enc == "mpeg4" else
                 ["-c:v", "h264_videotoolbox", "-profile:v", "high", "-b:v", "3M", "-g", "25"])
        audio = (["-map", f"0:a:{int(q.get('a', 0))}", "-c:a", "aac", "-b:a", "128k", "-ac", "2"] if AUDIO else ["-an"])
        command = [str(BINARIES[enc]), "-v", "error", "-ss", q.get("t", "0"), "-i", str(PATH), "-t", q.get("d", str(CHUNK)),
                   "-map", "0:v:0", *audio, "-vf", "scale=960:-2", *video,
                   "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "pipe:1"]
        started = time.monotonic()
        result = subprocess.run(command, capture_output=True)
        took = f"{(time.monotonic() - started) * 1000:.0f}"
        if result.returncode:
            return self.send(500, result.stderr[-500:])
        self.send(200, result.stdout, "video/mp4", {"X-Took": took})


def main():
    global PATH, DURATION, AUDIO
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    PATH = video_probe.media_path(sys.argv[1])
    probe = subprocess.run([str(video_probe.OWN_BIN / "ffprobe"), "-v", "error", "-show_entries",
                            "stream=codec_type:format=duration", "-of", "json", str(PATH)], capture_output=True)
    info = json.loads(probe.stdout or "{}")
    DURATION = float(info.get("format", {}).get("duration") or 0)
    AUDIO = sum(s.get("codec_type") == "audio" for s in info.get("streams", []))
    BINARIES["mpeg4"] = video_probe.OWN_BIN / "ffmpeg"
    other = shutil.which("ffmpeg")
    if other and "h264_videotoolbox" in video_probe.encoders(other):
        BINARIES["h264"] = Path(other)
    print(f"Bestand: {PATH.name}, {DURATION:.0f} s, {AUDIO} audiospoor/sporen. Varianten: {', '.join(BINARIES)}")
    print("Spoel een paar keer in het venster en wissel van variant; sluit het venster als je klaar bent.\n", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    webview.create_window("Videoproef", f"http://127.0.0.1:{server.server_port}/", width=1000, height=820)
    webview.start()


if __name__ == "__main__":
    main()
