"""Mappen (stap 6b en 6c): een gesleepte map als groep, wat er in een map meetelt, gevolgde mappen (watch folders) die
nieuwe bestanden pas oppakken als ze stilstaan en de Mac ruimte heeft, en wat het venster daarvan te zien krijgt.

Draaien: uv run --no-project --with pynacl --with requests --with numpy python -B tests/test_folders.py
"""
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, "app")
_kr = types.ModuleType("keyring")
_kr.get_password = lambda *a: None
_kr.set_password = lambda *a: None
_kr.delete_password = lambda *a: None
sys.modules["keyring"] = _kr
for name in ("mlx", "mlx_whisper", "torch", "webview"):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)
import transcribe  # noqa: E402

transcribe.L = lambda nl, en: nl
tmp = Path(tempfile.mkdtemp())
transcribe.CONFIG_PATH = tmp / "support" / "config.json"
transcribe.TRANSCRIPTS_DIR = tmp / "support" / "transcripts"
transcribe.WAVES_DIR = tmp / "support" / "waves"
transcribe.SIDECARS_FILE = tmp / "support" / "sidecars.json"
transcribe.LOCAL_INDEX = tmp / "support" / "local-files.json"

import jobs as job_list  # noqa: E402
import local_files  # noqa: E402
import serve  # noqa: E402
import settings  # noqa: E402

serve.jobs_path = lambda: tmp / "support" / "jobs.json"
serve.print = lambda *a, **k: None
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


def make(path, size=2000):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))
    return path


def drain():
    """De rij leeg, zodat de volgende stap niet denkt dat er nog iets loopt."""
    while not serve.work.empty():
        job = serve.work.get_nowait()
        job.set(status="klaar")


# --- wat er in een map meetelt ---
shoot = tmp / "Opnames dag 2"
make(shoot / "interview.mxf")
make(shoot / "camera B" / "sfeer.mov")
make(shoot / "geluid" / "zender.wav")
make(shoot / ".verborgen.mov")
make(shoot / "._interview.mxf")
make(shoot / "notities.txt")
make(shoot / "project.fcpbundle" / "render.mov")
make(shoot / ".cache" / "proxy.mov")
names = sorted(p.name for p in local_files.media_in(shoot))
check("map: video en audio, ook in submappen", names == ["interview.mxf", "sfeer.mov", "zender.wav"], names)
check("map: zonder submappen alleen het bovenste niveau", [p.name for p in local_files.media_in(shoot, subfolders=False)] == ["interview.mxf"])
check("map: zonder audio geen .wav", "zender.wav" not in [p.name for p in local_files.media_in(shoot, audio=False)])
check("map: een map die er niet is geeft niets", local_files.media_in(tmp / "bestaat-niet") == [])

# --- een gesleepte map wordt een groep ---
result = serve.add_local([str(shoot)])
check("slepen: drie bestanden uit de map", result == {"added": 3, "skipped": 0}, result)
rows = {j.title: j for j in serve.jobs}
check("slepen: elke regel hoort bij de map en weet zijn bestand",
      all(rows[n].group == str(shoot) and rows[n].path.endswith(n) for n in ("interview.mxf", "sfeer.mov", "zender.wav")))
state = serve.app_state()
by_title = {j["title"]: j for j in state["jobs"]}
check("venster: lokaal, met de submap op de tweede regel", by_title["sfeer.mov"]["local"] and by_title["sfeer.mov"]["where"] == "camera B"
      and by_title["interview.mxf"]["where"] == "", by_title["sfeer.mov"])
check("venster: de map als groep, nog niet gevolgd", state["folders"] == [{"path": str(shoot), "name": "Opnames dag 2", "watched": False, "paused": False, "new": []}],
      state["folders"])
check("venster: alleen namen, geen paden in de regels", all(str(tmp) not in j["where"] for j in state["jobs"]))
drain()
settings.put("files.audio", False)
other = tmp / "Tweede map"
make(other / "a.mov")
make(other / "b.wav")
result = serve.add_local([str(other)])
check("slepen: zonder Ook alleen audio geen .wav uit een map", result == {"added": 1, "skipped": 0}, result)
loose = make(tmp / "los.wav")
result = serve.add_local([str(loose)])
check("slepen: een los audiobestand gaat altijd mee", result == {"added": 1, "skipped": 0}, result)
check("slepen: een los bestand heeft geen groep, wel de map eronder", [j for j in serve.app_state()["jobs"] if j["title"] == "los.wav"][0]["where"]
      == tmp.name)
settings.put("files.audio", True)
drain()

# --- volgen ---
check("volgen: de hele schijf niet", serve.set_watch("/")["ok"] is False and serve.set_watch("")["ok"] is False)
check("volgen: een map die er niet is niet", serve.set_watch(str(tmp / "weg"))["ok"] is False)
check("volgen: de map van de groep", serve.set_watch(str(shoot))["ok"] and serve.watched() == {str(shoot): {"subfolders": True, "paused": False}})
check("volgen: het venster ziet de groep als gevolgd", serve.app_state()["folders"][0]["watched"] if serve.app_state()["folders"][0]["path"] == str(shoot)
      else [f for f in serve.app_state()["folders"] if f["path"] == str(shoot)][0]["watched"])
serve.set_watch(str(shoot), True, subfolders=False)
check("volgen: submappen uit, blijft gevolgd", serve.watched()[str(shoot)] == {"subfolders": False, "paused": False})
serve.set_watch(str(shoot), True)
check("volgen: nog eens aan houdt de keuze voor submappen", serve.watched()[str(shoot)] == {"subfolders": False, "paused": False})
serve.set_watch(str(shoot), True, subfolders=True)

# --- een ronde langs de gevolgde mappen ---
clock = [10_000_000.0]
now = lambda: clock[0]   # noqa: E731
room = [True]
serve.meter.check = lambda limits=None: (room[0], "" if room[0] else "cpu", {"cpu": 80.0})
serve.meter.start = lambda: None
# Wat al gesleept is, heeft nog geen transcript: die tellen dus ook mee. Eerst klaar maken, zodat alleen nieuw telt.
for media in local_files.media_in(shoot):
    (media.parent / (media.stem + local_files.SUFFIX)).write_text("{}")
check("ronde: alles heeft een transcript, niets te doen", serve.watch_round(now) is None and serve.watch_state["waiting"] == 0)
fresh = make(shoot / "camera B" / "nieuw.mov")
old_time = clock[0] - 3600
os.utime(fresh, (old_time, old_time))
check("ronde: een nieuw bestand de eerste keer gezien, nog niet opgepakt", serve.watch_round(now) is None and serve.work.empty())
check("ronde: de strook zegt meteen dat er iets nieuws is, met de naam", serve.app_state()["watch"]["settling"] == 1
      and serve.app_state()["watch"]["settling_names"] == ["nieuw.mov"] and serve.watch_state["waiting"] == 0)
clock[0] += 10
check("ronde: nog geen 15 seconden onveranderd", serve.watch_round(now) is None)
clock[0] += 40
room[0] = False
check("ronde: geen ruimte, dan wacht hij met de reden", serve.watch_round(now) is None and serve.watch_state["ready"] is False
      and serve.watch_state["reason"] == "cpu" and serve.watch_state["waiting"] == 1)
check("venster: de strook ziet 1 nieuw en waarom het wacht", serve.app_state()["watch"]["waiting"] == 1
      and serve.app_state()["watch"]["reason"] == "cpu")
newer = make(shoot / "nieuwer.mov")
os.utime(newer, (old_time + 60, old_time + 60))
serve.watch_round(now)   # nieuwer.mov de eerste keer gezien, nog steeds geen ruimte
clock[0] += 70
room[0] = True
taken = serve.watch_round(now)
check("ronde: met ruimte het nieuwste eerst", taken == str(newer), taken)
job = max(serve.jobs, key=lambda j: j.n)
check("ronde: automatisch, in de groep van de map", job.auto and job.group == str(shoot) and job.path == str(newer))
check("ronde: één tegelijk, niet terwijl er iets loopt", serve.watch_round(now) is None and serve.watch_state["reason"] == "busy")
drain()
(newer.parent / ("nieuwer" + local_files.SUFFIX)).write_text("{}")

room[0] = False
growing = make(shoot / "groeit.mov")
os.utime(growing, (old_time + 30, old_time + 30))
serve.watch_round(now)   # de eerste keer gezien
clock[0] += 70
make(growing, 4000)      # nog aan het kopiëren: groter geworden
os.utime(growing, (old_time + 30, old_time + 30))
serve.watch_round(now)
check("ronde: een bestand dat nog groeit telt nog niet", serve.watch_state["waiting"] == 1, serve.watch_state)
clock[0] += 70
serve.watch_round(now)
check("ronde: 15 seconden onveranderd, dan telt het", serve.watch_state["waiting"] == 2, serve.watch_state)
local_files.skip(growing)   # alsof de opdracht mislukte
room[0] = True
check("ronde: een bestand dat mislukte, niet opnieuw", serve.watch_round(now) == str(fresh))
drain()
(fresh.parent / ("nieuw" + local_files.SUFFIX)).write_text("{}")
clock[0] += 70
check("ronde: daarna niets meer", serve.watch_round(now) is None and serve.watch_state["waiting"] == 0)
make(growing, 5000)
os.utime(growing, (old_time + 30, old_time + 30))
serve.watch_round(now)
clock[0] += 70
check("ronde: wel weer als het veranderd is", serve.watch_round(now) == str(growing))
drain()

# --- pauze bij een gevolgde map ---
drain()
first, second = make(shoot / "pauze1.mov"), make(shoot / "pauze2.mov")
serve.add_local([str(first), str(second)], group=str(shoot))
held = [j for j in serve.jobs if j.path in (str(first), str(second))]
check("pauze: alleen bij een gevolgde map", serve.pause_watch(str(other))["ok"] is False)
check("pauze: aan, bewaard bij de map", serve.pause_watch(str(shoot))["ok"] and serve.watched()[str(shoot)]["paused"])
check("pauze: de wachtende van de groep staan gepauzeerd", len(held) == 2 and all(j.status == serve.PAUSED for j in held),
      [j.status for j in held])
state = serve.app_state()
check("pauze: het venster ziet de groep gepauzeerd en de strook telt hem",
      [f for f in state["folders"] if f["path"] == str(shoot)][0]["paused"] and state["watch"]["paused"] == 1)
third = make(shoot / "pauze3.mov")
clock[0] += 100
check("pauze: de map wordt niet bekeken", serve.watch_round(now) is None and serve.watch_state["settling"] == 0
      and serve.watch_state["waiting"] == 0, serve.watch_state)
check("pauze: nog eens slepen geeft geen tweede regel", serve.add_local([str(first)], group=str(shoot))["added"] == 0)
serve.add_local([str(third)], group=str(shoot))
late = [j for j in serve.jobs if j.path == str(third)][0]
check("pauze: wat er nu in de groep bij komt, start ook niet", late.status == serve.PAUSED, late.status)
serve.set_watch(str(shoot), True, subfolders=True)
check("pauze: Ook submappen veranderen houdt de pauze", serve.watched()[str(shoot)]["paused"])
serve.save_jobs()
fresh = job_list.JobList()
fresh.load(serve.jobs_path())
kept = [j for j in fresh if j.path in (str(first), str(second), str(third))]
check("pauze: na een herstart nog gepauzeerd", len(kept) == 3 and all(j.status == serve.PAUSED for j in kept), [j.status for j in kept])
fresh.clear_finished()
check("pauze: Wis afgeronde laat ze staan", len([j for j in fresh if j.status == serve.PAUSED]) == 3)
check("hervat: de pauze is weg", serve.pause_watch(str(shoot), on=False)["ok"] and not serve.watched()[str(shoot)]["paused"])
check("hervat: de opdrachten wachten weer en staan in de rij", all(j.status == "wachtend" for j in held + [late])
      and {j.n for j in held + [late]} <= {j.n for j in list(serve.work.queue)})
drain()

serve.set_watch(str(shoot), on=False)
check("niet meer volgen: weg uit de lijst, de opdrachten blijven", serve.watched() == {} and any(j.group == str(shoot) for j in serve.jobs))
check("niet meer volgen: de groep staat er nog, niet meer gevolgd",
      [f for f in serve.app_state()["folders"] if f["path"] == str(shoot)][0]["watched"] is False)

# --- weggehaald: een gevolgde map als inbox (erin, transcriberen, eruit) ---
done = max((j for j in serve.jobs if j.path == str(newer)), key=lambda j: j.n)
done.set(status="klaar")
newer.rename(tmp / "elders.mov")
serve.check_gone()
row = [j for j in serve.app_state()["jobs"] if j["n"] == done.n][0]
check("weggehaald: de regel weet dat het bestand weg is", row["gone"] is True)
waiting = serve.jobs.add("local:" + "f" * 32, title="wacht.mov", group=str(shoot), path=str(tmp / "bestaat-niet.mov"))
serve.check_gone()
row = [j for j in serve.app_state()["jobs"] if j["n"] == waiting.n][0]
check("weggehaald: een opdracht die nog wacht telt niet", row["gone"] is False)
waiting.set(status="klaar")
serve.set_watch(str(shoot))
serve.watch_state["settling_by_folder"] = {str(shoot): ["vers.mov"]}
check("nieuw gezien: per gevolgde map in het venster", [f for f in serve.app_state()["folders"] if f["path"] == str(shoot)][0]["new"] == ["vers.mov"])
serve.watch_state["settling_by_folder"] = {}
serve.set_watch(str(shoot), on=False)
check("weggehaald: een opslag die weg is, maakt niets weggehaald", serve._volume_there("/Volumes/BestaatNiet/a.mov") is False
      and serve._volume_there(str(tmp / "a.mov")) is True)

# --- een gevolgde map die niet bereikbaar is ---
gone = tmp / "Volumes" / "Opslag" / "Opnames"
gone.mkdir(parents=True)
serve.set_watch(str(gone))
serve.system.mount_reachable = lambda path: False
serve.watch_round(now)
check("onbereikbaar: de strook noemt de map", serve.app_state()["watch"]["offline"] == ["Opnames"])

# --- kan niet schrijven (stap 6d) ---
serve.system.mount_reachable = lambda path: True
card = tmp / "kaart"
for name in ("a.mov", "b.mov"):
    make(card / "DCIM" / name)
real_can_write = local_files.can_write
locked = [str(card)]
local_files.can_write = lambda directory: "readonly" if any(str(directory).startswith(p) for p in locked) else None
drain()
result = serve.add_local([str(card)])
stuck = [j for j in serve.jobs if j.group == str(card)]
check("vooraf: de bestanden staan in de lijst, wachtend op een plek, niet in de rij",
      result["added"] == 2 and all(j.status == serve.PLACE for j in stuck) and serve.work.empty(), [j.status for j in stuck])
check("vooraf: met de reden", "alleen-lezen" in stuck[0].message, stuck[0].message)
check("vooraf: nog eens slepen geeft geen tweede regel", serve.add_local([str(card)]) == {"added": 0, "skipped": 0})
serve.set_watch(str(card))
for media in local_files.media_in(card):
    os.utime(media, (old_time, old_time))
serve.watch_round(now)
clock[0] += 30
check("vooraf: een gevolgde map voegt ze ook niet opnieuw toe", serve.watch_round(now) is None and serve.watch_state["waiting"] == 0)
serve.set_watch(str(card), on=False)
check("venster: telt mee als niet afgerond", all(j["status"] == "plek" for j in serve.app_state()["jobs"] if j["group"] == str(card)))
check("bereik: zonder /Volumes en zonder gevolgde map is het de map van het bestand",
      local_files.scope_of(card / "DCIM" / "a.mov") == str(card / "DCIM"))
elsewhere = tmp / "Transcripten elders"
asked = []
serve.system.ask_folder = lambda prompt, start=None: asked.append(prompt) or str(elsewhere)
result = serve.choose_place(stuck[0].n)
check("kies een andere plek: onthouden voor het bereik", result["ok"] and local_files.elsewhere() == {str(card / "DCIM"): str(elsewhere / "DCIM")}, result)
check("kies een andere plek: de opdrachten gaan de rij in", all(j.status == "wachtend" for j in stuck) and serve.work.qsize() == 2)
check("kies een andere plek: daarheen, met de naam van de map ervoor", local_files.directory_for(card / "DCIM" / "a.mov") == elsewhere / "DCIM")
drain()
check("vergeet: de plek is weg", serve.forget_place(str(card / "DCIM"))["ok"] and local_files.elsewhere() == {})
serve.system.ask_folder = lambda prompt, start=None: str(card)
locked_can_write = local_files.can_write
local_files.can_write = lambda directory: "denied" if Path(directory) == card / "DCIM" else None
stuck = [j for j in serve.jobs if j.group == str(card)]
for job in stuck:
    job.set(status=serve.PLACE)
result = serve.choose_place(stuck[0].n)
check("de map erboven gekozen: een eigen map ernaast, de schrijftest op die map",
      result["ok"] and local_files.elsewhere() == {str(card / "DCIM"): str(card / "DCIM transcripten")}
      and local_files.directory_for(card / "DCIM" / "a.mov") == card / "DCIM transcripten", (result, local_files.elsewhere()))
drain()
serve.forget_place(str(card / "DCIM"))
local_files.can_write = locked_can_write
make(card / "DCIM" / "c.mov")
local_files.can_write = lambda directory: "denied" if str(card) in str(directory) or "ook op slot" in str(directory) else None
serve.add_local([str(card / "DCIM" / "c.mov")])
c_job = max(serve.jobs, key=lambda j: j.n)
check("geen rechten: wacht op een plek, met die reden", c_job.status == serve.PLACE and "schrijfrechten" in c_job.message, c_job.message)
serve.system.ask_folder = lambda prompt, start=None: str(tmp / "ook op slot")
refused = serve.choose_place(c_job.n)
check("een plek die ook niet kan: geweigerd met de reden, niets onthouden", "ook niet" in refused.get("error", "")
      and local_files.elsewhere() == {} and c_job.status == serve.PLACE, refused)
check("annuleer: de wachtende uit de lijst", serve.cancel_place(c_job.n)["ok"] and not any(j.n == c_job.n for j in serve.jobs))
local_files.can_write = real_can_write
serve.system.mount_reachable = lambda path: False   # terug zoals in het stuk over onbereikbaar
serve.watch_round(now)

# --- Instellingen › Bestanden ---
data = serve.settings_data("files")
check("instellingen: de tab kent de plaats, audio, de gevolgde mappen en de andere plekken",
      {s["key"] for s in data["settings"]} >= {"files.transcript_place", "files.transcript_folder", "files.audio", "files.watched",
                                               "files.elsewhere"} and data["extra"]["elsewhere"] == []
      and data["extra"]["watched"][0]["name"] == "Opnames" and data["extra"]["watched"][0]["offline"])

print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
