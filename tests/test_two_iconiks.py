"""Meerdere Iconiks op één Mac (stap 5e): twee nagemaakte Iconik-omgevingen met elk een eigen rij, opslag, naam van
deze Mac en instellingen, en een derde koppeling naar dezelfde omgeving als de eerste (besloten 25 november: mag,
maar wat Macs delen doet alleen de eerste).

Draaien: uv run --no-project --with pynacl --with requests python -B tests/test_two_iconiks.py
"""
import http.client
import json
import sys
import tempfile
import threading
import time
import types
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, "app")
sys.path.insert(0, "tools")
# Nooit de echte sleutelhanger: een nep-keyring zonder inlog (anders vraagt macOS om het wachtwoord).
_kr = types.ModuleType("keyring")
_kr.get_password = lambda *a: None
_kr.set_password = lambda *a: None
_kr.delete_password = lambda *a: None
sys.modules["keyring"] = _kr
for name in ("numpy", "mlx", "mlx_whisper", "torch", "webview"):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)
import transcribe, serve, worker, iconik, iconik_queue, items, connectors  # noqa: E402,E401
import settings  # noqa: E402
from fake_queue_iconik import Fake  # noqa: E402

tmp = Path(tempfile.mkdtemp())
transcribe.CONFIG_PATH = tmp / "config.json"   # nooit de echte voorkeuren aanraken
transcribe.print = lambda *a, **k: None
serve.find_icon = lambda host: serve.icons.__setitem__(host, ("", time.time()))   # geen netwerk voor het icoon
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("OK   " if cond else "FOUT ") + name + (f"  [{detail}]" if detail and not cond else ""))


A, B, C = "iconik-aaaaaa", "iconik-bbbbbb", "iconik-cccccc"
DA, DB = "aaaaaaaa-1111-2222-3333-444444444444", "bbbbbbbb-1111-2222-3333-444444444444"
(tmp / "opslag-a").mkdir()
transcribe.CONFIG_PATH.write_text(json.dumps({
    "version": 2, "general": {}, "transcription": {}, "refine": {},
    "integrations": {
        # A is gekoppeld vóór stap 5 (geen tijd): de eerste. B daarna.
        A: {"type": "iconik", "host": "https://a.example", "worker_enabled": True, "machine_name": "Montage A",
            "mounts": {"sa": str(tmp / "opslag-a")}, "system_domain_id": DA},
        B: {"type": "iconik", "added": 1000.0, "host": "https://b.example", "worker_enabled": True, "machine_name": "Montage B",
            "mounts": {"sb": str(tmp / "opslag-b-weg")}, "system_domain_id": DB},
    }}))
clients = {A: Fake(), B: Fake()}
iconik.client = lambda instance: clients.get(instance)
connectors.start_all(serve.AppHost())
for inst in (A, B):
    worker.set_machine_alias(settings.get("iconik.machine_name", inst), inst)
added = []


def capture(key, **kwargs):
    added.append((key, kwargs))


def round_once():
    added.clear()
    return worker.round_once(serve.worker_may_take, capture, log=lambda *a: None)


check("opzet: twee Iconiks, op volgorde van koppelen", serve.iconiks() == [A, B])
check("opzet: elk een eigen adres", transcribe.api_host(A) == "https://a.example" and transcribe.api_host(B) == "https://b.example")

# --- de rij: elk een eigen rij, één opdracht tegelijk op deze Mac ---
clients[A].values["a1"] = {iconik_queue.FIELD: iconik_queue.REQUESTED}
clients[B].values["b1"] = {iconik_queue.FIELD: iconik_queue.REQUESTED}
taken = round_once()
check("rij: één opdracht per rondje, eerst de eerste Iconik", taken == f"{A}:a1" and added == [("a1", {"remote": 1, "auto": False, "source": A})], added)
check("rij: het asset staat in de rij van A op bezig, met de naam van deze Mac in A",
      clients[A].values["a1"][iconik_queue.FIELD] == iconik_queue.BUSY and "Montage A" in clients[A].values["a1"][iconik_queue.INFO])
check("rij: B is dit rondje niet aangeraakt", clients[B].values["b1"][iconik_queue.FIELD] == iconik_queue.REQUESTED)
iconik_queue.current.clear()   # de opdracht van A is klaar
taken = round_once()
check("rij: het volgende rondje komt B aan bod, met zijn eigen bron", taken == f"{B}:b1" and added[0][1]["source"] == B, added)
check("rij: in B staat de naam van deze Mac in B", "Montage B" in clients[B].values["b1"][iconik_queue.INFO]
      and "Montage A" not in clients[B].values["b1"][iconik_queue.INFO])
check("rij: de connector weet dat de lopende opdracht van B is", iconik_queue.current.get("instance") == B)
iconik_queue.current.clear()

# --- pauze, fout en status per Iconik ---
iconik_queue.pause(reason="opslag (ISG) weg", instance=A)
clients[A].values["a2"] = {iconik_queue.FIELD: iconik_queue.REQUESTED}
clients[B].values["b2"] = {iconik_queue.FIELD: iconik_queue.REQUESTED}
taken = round_once()
check("pauze: A gepauzeerd, B werkt gewoon door", taken == f"{B}:b2" and clients[A].values["a2"][iconik_queue.FIELD] == iconik_queue.REQUESTED, taken)
check("pauze: alleen de status van A toont de pauze", iconik_queue.state_of(A)["paused_until"] and not iconik_queue.state_of(B)["paused_until"])
iconik_queue.current.clear()
iconik_queue.resume(B)
check("pauze: hervatten van B laat de pauze van A staan", iconik_queue.paused(A) and not iconik_queue.paused(B))
iconik_queue.resume(A)
check("pauze: hervatten van A", not iconik_queue.paused(A))
taken = round_once()
check("pauze: na hervatten neemt A zijn asset aan", taken == f"{A}:a2")
iconik_queue.current.clear()
clients[B] = None   # B: niet ingelogd; A heeft niets meer, dus beide worden gevraagd
taken = round_once()
check("fout: niet ingelogd bij B staat alleen bij B", taken is None
      and ("ingelogd" in iconik_queue.state_of(B)["error"] or "logged in" in iconik_queue.state_of(B)["error"])
      and iconik_queue.state_of(A)["error"] == "", [iconik_queue.state_of(A)["error"], iconik_queue.state_of(B)["error"]])
clients[B] = Fake()
check("status: de eerste Iconik houdt de namen van vóór stap 5", iconik_queue.state_of(A) is iconik_queue.state and worker.state is iconik_queue.state)

# --- de knoppen: elke knop zegt uit welke omgeving hij komt ---
check("knop: Refine uit B opent het asset van B", serve.refine_link(f"iconiktranscribe://refine?lt_domain={DB}&asset_ids=abcdef12") == f"/refine?item={B}:abcdef12")
check("knop: Refine uit A opent het asset van A", serve.refine_link(f"iconiktranscribe://refine?lt_domain={DA}&asset_ids=abcdef12") == f"/refine?item={A}:abcdef12")
check("knop: van vóór stap 4e (zonder domein): de eerste Iconik", serve.refine_link("iconiktranscribe://refine?asset_ids=abcdef12") == f"/refine?item={A}:abcdef12")
check("knop: uit een omgeving die deze Mac niet kent: niets", serve.refine_link("iconiktranscribe://refine?lt_domain=cccccccc-0000&asset_ids=abcdef12") is None)
serve.queue_request([(["x2"], [], DB), (["x3"], [], DA)])
with serve.jobs_lock:
    queued = {job.item for job in serve.jobs}
check("knop: transcriberen zet elk asset met zijn eigen Iconik in de lijst", {f"{B}:x2", f"{A}:x3"} <= queued, queued)
with serve.jobs_lock:
    serve.jobs.clear()
while not serve.work.empty():
    serve.work.get_nowait()

# --- instellingen per Iconik ---
r = serve.apply_settings({"iconik.auto_audio": True}, B)
check("instellingen: bewaren bij B raakt A niet", r["ok"] and settings.get("iconik.auto_audio", B) is True and settings.get("iconik.auto_audio", A) is False, r)
r = serve.apply_settings({"iconik.auto_audio": True})
check("instellingen: zonder te zeggen welke Iconik een nette weigering", r["ok"] is False and "Iconik" in r["error"] and settings.get("iconik.auto_audio", A) is False, r)
r = serve.apply_settings({"transcription.language": "nl"})
check("instellingen: een instelling van de app heeft geen Iconik nodig", r["ok"] and settings.get("transcription.language") == "nl", r)
data = serve.settings_data("iconik", B)
check("instellingen: de gegevens van de tab per Iconik", data["values"]["iconik.auto_audio"] is True and data["extra"]["instance"] == B
      and data["extra"]["worker"]["machine_alias"] == "Montage B", data["extra"]["worker"])
r = serve.apply_settings({"iconik.machine_name": "Edit 2"}, B)
check("instellingen: een andere naam in B verandert alleen de naam in B", r["ok"] and worker.machine_name(B) == "Edit 2" and worker.machine_name(A) == "Montage A")
r = serve.reset_settings(["iconik.auto_audio"], B)
check("instellingen: terugzetten per Iconik", r["ok"] and settings.stored("iconik.auto_audio", B) is None)

# --- automatisch werk: de opslag van de ene Iconik raakt de andere niet ---
for inst in (A, B):
    settings.put("iconik.auto_transcribe", True, inst)
serve.storage.update(offline=["sb"], checked=time.time())
real_check = serve.meter.check
serve.meter.check = lambda limits: (True, None, {})
rules_a, rules_b = serve.auto_settings(A), serve.auto_settings(B)
serve.meter.check = real_check
check("automatisch: A werkt met zijn eigen opslag", rules_a and rules_a["storages"] == ["sa"], rules_a)
check("automatisch: B wacht, want zijn opslag is weg", rules_b is None and iconik_queue.state_of(B)["auto"]["reason"] == "offline"
      and iconik_queue.state_of(A)["auto"]["ready"] is True, iconik_queue.state_of(B)["auto"])
check("opslag: de bewaking kent de mount points van alle Iconiks", serve.all_mounts() == {"sa": str(tmp / "opslag-a"), "sb": str(tmp / "opslag-b-weg")})
check("opslag: per Iconik een eigen lijst", [m["id"] for m in serve._iconik_extra(A)["mounts"]] == ["sa"]
      and [m["id"] for m in serve._iconik_extra(B)["mounts"]] == ["sb"])

# --- een derde koppeling naar dezelfde omgeving als A (keuze B van 25 november) ---
c_id = settings.add_integration("iconik")
cfg = json.loads(transcribe.CONFIG_PATH.read_text())
cfg["integrations"][C] = dict(cfg["integrations"].pop(c_id), worker_enabled=True, auto_transcribe=True, system_domain_id=DA)
transcribe.CONFIG_PATH.write_text(json.dumps(cfg))
clients[C] = Fake()
clients[C].values["c1"] = {iconik_queue.FIELD: iconik_queue.REQUESTED}
check("zelfde omgeving: C weet dat A dezelfde omgeving is", items.same_environment(C) == A and items.same_environment(A) is None
      and items.same_environment(B) is None)
check("zelfde omgeving: C doet niet mee aan de rij, ook met de schakelaar aan", serve.worker_enabled(C) is False and serve.worker_enabled(A) is True)
taken = round_once()
check("zelfde omgeving: in een rondje wordt C niet eens gevraagd", clients[C].posts == [] and taken is None, (taken, clients[C].posts[:1]))
check("zelfde omgeving: knoppen uit die omgeving gaan naar A", items.iconik_for_domain(DA) == A)
check("zelfde omgeving: het venster weet het", serve._iconik_extra(C)["same_as"] == A and serve._iconik_extra(A)["same_as"] is None)
check("zelfde omgeving: automatisch werk staat bij C niet aan", serve.auto_active(C) is False and serve.auto_active(A) is True)

# --- over HTTP, zoals het venster: ?instance= ---
srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def call(method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
    headers = {"Cookie": f"{serve.SESSION_COOKIE}={serve.SESSION}", "X-Local-Transcribe": "1", "Content-Type": "application/json"}
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    response = conn.getresponse()
    return json.loads(response.read() or b"{}")


reply = call("POST", f"/settings?instance={B}", {"iconik.auto_max_minutes": 30})
check("http: POST /settings?instance= bewaart bij die Iconik", reply.get("ok") and settings.get("iconik.auto_max_minutes", B) == 30
      and settings.get("iconik.auto_max_minutes", A) == 60, reply)
reply = call("GET", f"/settings-data?group=iconik&instance={B}")
check("http: /settings-data?instance= geeft die Iconik", reply["extra"]["instance"] == B and reply["values"]["iconik.auto_max_minutes"] == 30)
reply = call("GET", "/settings-data?group=iconik")
check("http: zonder instance de eerste Iconik", reply["extra"]["instance"] == A)
reply = call("POST", "/settings", {"iconik.auto_max_minutes": 20})
check("http: zonder instance bewaren bij meer Iconiks: een nette weigering", reply.get("ok") is False and settings.get("iconik.auto_max_minutes", A) == 60, reply)

# --- een Iconik weghalen ---
iconik_queue.pause(reason="x", instance=B)
for inst, key in ((A, "abcdef01"), (B, "abcdef02")):
    (transcribe.TRANSCRIPTS_DIR / inst).mkdir(parents=True, exist_ok=True)
    (transcribe.TRANSCRIPTS_DIR / inst / f"{key}.json").write_text("{}")
check("weghalen: de knoppen kunnen weg bij B (een eigen omgeving), niet bij A (C gebruikt dezelfde)",
      serve.buttons_removable(B) and not serve.buttons_removable(A) and not serve.buttons_removable(C))
clients[B] = None   # Iconik niet bereikbaar, zoals na een afgelopen abonnement
r = serve.remove_integration(B, forget=True, buttons=True)
check("weghalen: B is weg, A en C blijven", r["ok"] and serve.iconiks() == [A, C], (r, serve.iconiks()))
check("weghalen: knoppen niet weg te halen: de koppeling gaat toch, met een melding", "knoppen" in r["note"].lower() or "buttons" in r["note"].lower(), r)
check("weghalen: 'ook van deze Mac' haalt de bestanden van B weg, die van A blijven",
      not (transcribe.TRANSCRIPTS_DIR / B).exists() and (transcribe.TRANSCRIPTS_DIR / A / "abcdef01.json").exists())
check("weghalen: de stand van de rij van B is vergeten", B not in iconik_queue._lanes)
check("weghalen: A werkt nog", serve.worker_enabled(A) and settings.get("iconik.machine_name", A) == "Montage A")

srv.shutdown()
print("ALLES OK" if not fails else f"{fails} FOUT")
sys.exit(1 if fails else 0)
