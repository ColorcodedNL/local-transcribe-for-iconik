#!/usr/bin/env python3
"""Local Transcribe for Iconik: de app. Eén proces met ontvanger, werkrij en venster.

Gestart door de launcher in Contents/MacOS. Links van het type iconiktranscribe://
komen binnen via de app-delegate (ook als de app er zelf door gestart wordt).
Venster sluiten verbergt het alleen; Cmd+Q stopt de app, met een waarschuwing
als er nog een opdracht loopt.
"""

import http.client
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

APP_NAME = "Local Transcribe for Iconik"
RES = Path(os.environ.get("LOCALTRANSCRIBE_RES") or Path(__file__).resolve().parent.parent)  # Contents/Resources van de bundel
CODE_VERSION = os.environ.get("LOCALTRANSCRIBE_CODE")   # gezet door main.py als een code-update draait
SUPPORT = Path.home() / "Library" / "Application Support" / APP_NAME
LOGDIR = Path.home() / "Library" / "Logs" / APP_NAME
PORT = 8765
BASE = f"http://localhost:{PORT}"


def prepare():
    """Mappen, zoekpad en log, voordat er iets anders geladen wordt."""
    try:
        old = SUPPORT.with_name("Iconik Transcribe")  # naam tot oktober 2026
        if old.is_dir() and not SUPPORT.exists():
            old.rename(SUPPORT)
    except OSError:
        pass
    SUPPORT.mkdir(parents=True, exist_ok=True)
    LOGDIR.mkdir(parents=True, exist_ok=True)
    os.environ["PATH"] = f"{RES / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin"
    os.chdir(SUPPORT)
    # Alles wat het proces schrijft, ook uit ffmpeg en C-bibliotheken, naar het log.
    log = open(LOGDIR / "serve.log", "a", buffering=1)
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    code = f"code-update {CODE_VERSION}" if CODE_VERSION else "code uit de bundel"
    print(f"\n===== app gestart {datetime.now():%Y-%m-%d %H:%M:%S} vanaf {RES.parent.parent} ({code}) =====")


prepare()
sys.path.insert(0, str(Path(__file__).resolve().parent))
import transcribe  # noqa: E402  (na prepare, zodat het zoekpad en de telemetrie-instelling kloppen)
import serve  # noqa: E402

window = None
hidden = False  # het venster is verborgen (rode knop of Cmd+W)


def show(page=None):
    """Venster tonen en naar voren halen, eventueel met een andere pagina."""
    global hidden
    if window is None:
        return
    if page:
        window.load_url(BASE + page)
    hidden = False
    window.show()
    reconcile()


def local_get(path, timeout=60):
    """GET op de eigen ontvanger, zonder doorverwijzingen te volgen. Geeft (status, Location)."""
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    try:
        connection.request("GET", path, headers={"X-Local-Transcribe": "1"})  # /window/show vraagt erom
        response = connection.getresponse()
        location = response.getheader("Location") or ""
        response.read()
        return response.status, location
    finally:
        connection.close()


def send_link(url):
    """Geeft een link uit Iconik aan de ontvanger. Geeft de pagina terug die het venster moet tonen."""
    query = url.split("?", 1)[1] if "?" in url else ""
    if not query:
        return None
    try:
        _, location = local_get(f"/transcribe/{serve.listener_key()}?{query}")
        print(f"[{datetime.now():%H:%M:%S}] link uit Iconik, doorverwezen naar: {location or 'niets'}")
        return "/login" if location.endswith("/login") else "/"
    except Exception as error:
        print(f"Link uit Iconik verwerken mislukte: {type(error).__name__}: {error}")
        return None


def on_link(url):
    """Een klik op Local Transcribe in Iconik. Draait in een eigen draad."""
    show(send_link(url))


def our_receiver_running():
    """Draait er al een exemplaar van deze app op de poort?"""
    try:
        status, _ = local_get("/window/state", timeout=2)
        return status == 200
    except Exception:
        return False


def run_as_messenger():
    """Er draait al een exemplaar (bijvoorbeeld vanaf een andere kopie van de app).

    Dan geven we een eventuele link door, laten het venster daar naar voren komen
    en stoppen we zelf, zonder Dock-icoon en zonder melding.
    """
    import objc
    import AppKit
    from PyObjCTools import AppHelper

    print("Er draait al een exemplaar; link en venster gaan daarheen.")
    handled = {"link": False}

    def forward(urls):
        page = None
        for url in urls:
            page = send_link(url) or page
        try:
            local_get(f"/window/show?page={page or '/'}", timeout=5)
        except Exception:
            pass
        AppHelper.callAfter(AppKit.NSApp.terminate_, None)

    class LocalTranscribeMessenger(AppKit.NSObject):
        @objc.typedSelector(b"v@:@@")
        def application_openURLs_(self, app, urls):
            handled["link"] = True
            threading.Thread(target=forward, args=([str(u.absoluteString()) for u in urls],), daemon=True).start()

        def applicationDidFinishLaunching_(self, notification):
            # Komt er binnen een seconde geen link, dan was het een gewone start: venster tonen.
            def later():
                if not handled["link"]:
                    threading.Thread(target=forward, args=([],), daemon=True).start()
            AppHelper.callLater(1.0, later)

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyProhibited)
    delegate = LocalTranscribeMessenger.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()


def restart():
    """Na een code-update: deze app afsluiten en opnieuw openen, zodat de nieuwe code draait."""
    import subprocess
    import AppKit
    from PyObjCTools import AppHelper
    bundle = RES.parent.parent
    if bundle.suffix != ".app":
        print("Herstarten kan alleen vanuit de app zelf.")
        return
    print(f"[{datetime.now():%H:%M:%S}] de app herstart (update of vastgelopen opdracht)")
    # Een los scriptje wacht tot dit proces weg is en opent dan de app. Is dit proces na een minuut nog
    # niet weg (iemand koos Annuleren), dan opent het niets.
    script = ("n=0; while kill -0 \"$1\" 2>/dev/null && [ $n -lt 120 ]; do sleep 0.5; n=$((n+1)); done; "
              "kill -0 \"$1\" 2>/dev/null || open \"$2\"")
    subprocess.Popen(["/bin/sh", "-c", script, "sh", str(os.getpid()), str(bundle)], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    AppHelper.callAfter(AppKit.NSApp.terminate_, None)


def on_closing():
    """Rode knop of Cmd+W: alleen verbergen, de app en het werk lopen door."""
    global hidden
    if window is not None:
        window.hide()
        hidden = True
        reconcile()
    return False


menubar = None  # (statusitem, doel), alleen zolang opdrachten op afstand aan staan


def status_text():
    import worker as remote
    if serve.busy():
        return transcribe.L("Bezig met een opdracht", "Working on a job")
    if remote.paused():
        until, why = remote.state["paused_until"], remote.state.get("pause_reason") or ""
        return transcribe.L(f"Gepauzeerd tot {until}" + (f": {why[:70]}" if why else ""),
                            f"Paused until {until}" + (f": {why[:70]}" if why else ""))
    return transcribe.L("Wacht op opdrachten", "Waiting for jobs")


def reconcile():
    """Menubalkicoon en Dock-icoon in lijn brengen met de instellingen. Mag vanaf elke draad."""
    from PyObjCTools import AppHelper
    AppHelper.callAfter(apply_menubar)


# Het app-icoon (icon.svg) zonder achtergrond: geluidsbalkjes links, tekstregels rechts (x, y, breedte, hoogte).
GLYPH = [(222, 437, 44, 150), (290, 347, 44, 330), (358, 397, 44, 230), (426, 457, 44, 110),
         (522, 409, 104, 42), (642, 409, 160, 42), (522, 491, 150, 42), (688, 491, 58, 42), (762, 491, 40, 42),
         (522, 573, 84, 42), (622, 573, 112, 42)]


def glyph(height=16):
    """Menubalkicoon als sjabloonafbeelding: macOS kleurt het zelf voor licht en donker."""
    import AppKit
    import Foundation
    x0, y0, width, tall = 222, 347, 580, 330
    scale = height / tall

    def draw(rect):
        AppKit.NSColor.blackColor().set()
        for x, y, w, h in GLYPH:
            AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                Foundation.NSMakeRect((x - x0) * scale, (y - y0) * scale, w * scale, h * scale),
                min(w, h) * scale / 2, min(w, h) * scale / 2).fill()
        return True

    image = AppKit.NSImage.imageWithSize_flipped_drawingHandler_(Foundation.NSMakeSize(width * scale, height), True, draw)
    image.setTemplate_(True)
    return image


_Target = None


def menu_target():
    """De klasse voor de menu-acties. Een Objective-C klasse kan maar één keer bestaan, dus één keer aanmaken."""
    global _Target
    if _Target is None:
        import AppKit

        class MenuTarget(AppKit.NSObject):
            def openWindow_(self, sender):
                show()

            def quitApp_(self, sender):
                AppKit.NSApp.terminate_(None)

            def resumeNow_(self, sender):
                import worker as remote
                remote.resume()
                remote.wake.set()

            def menuNeedsUpdate_(self, menu):
                import worker as remote
                menu.itemAtIndex_(0).setTitle_(status_text())
                menu.itemAtIndex_(2).setHidden_(not remote.paused())   # "Nu hervatten" alleen tijdens een pauze

        _Target = MenuTarget
    return _Target


def apply_menubar():
    """Op de hoofddraad: icoon in de menubalk zolang opdrachten op afstand aan staan, Dock-icoon weg als het venster verborgen is."""
    global menubar
    import AppKit
    import objc
    want = serve.worker_enabled()
    if want and menubar is None:
        target = menu_target().alloc().init()
        menu = AppKit.NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        menu.addItemWithTitle_action_keyEquivalent_(status_text(), None, "").setEnabled_(False)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        for title, action in ((transcribe.L("Nu hervatten", "Resume now"), b"resumeNow:"),
                              (transcribe.L("Venster openen", "Open window"), b"openWindow:"),
                              (transcribe.L("Stop", "Quit"), b"quitApp:")):
            item = menu.addItemWithTitle_action_keyEquivalent_(title, action, "")
            item.setTarget_(target)
        menu.setDelegate_(target)
        item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        item.button().setImage_(glyph())
        item.setMenu_(menu)
        menubar = (item, target, menu)
        print(f"[{datetime.now():%H:%M:%S}] menubalk: icoon geplaatst")
    elif not want and menubar is not None:
        AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(menubar[0])
        menubar = None
        print(f"[{datetime.now():%H:%M:%S}] menubalk: icoon weggehaald")
    policy = AppKit.NSApplicationActivationPolicyAccessory if (want and hidden) else AppKit.NSApplicationActivationPolicyRegular
    app = AppKit.NSApp
    if app.activationPolicy() != policy:
        app.setActivationPolicy_(policy)
        if policy == AppKit.NSApplicationActivationPolicyRegular and not hidden:
            app.activateIgnoringOtherApps_(True)


def watch_settings():
    """Het vinkje in Voorkeuren kan elk moment veranderen: elke paar seconden bijwerken."""
    import time
    while True:
        time.sleep(3)
        try:
            reconcile()
        except Exception as error:
            print(f"menubalk: {error}")


STARTING = SUPPORT / "code" / "starting"


def begin_start():
    """Een code-update die niet tot een venster komt, wordt bij de volgende start overgeslagen (zie main.py)."""
    if CODE_VERSION:
        STARTING.parent.mkdir(parents=True, exist_ok=True)
        STARTING.write_text(f"{CODE_VERSION} {os.getpid()}")


def mark_started():
    global hidden
    try:
        STARTING.unlink()
    except FileNotFoundError:
        pass
    if serve.LOGIN_MARKER.exists():  # gestart door inloggen: met opdrachten op afstand aan meteen naar de menubalk
        serve.LOGIN_MARKER.unlink(missing_ok=True)
        if serve.worker_enabled() and window is not None:
            hidden = True
            window.hide()
            reconcile()


def main():
    global window
    if our_receiver_running():
        run_as_messenger()
        return
    begin_start()
    try:
        serve.start(idle_exit=False)
    except OSError as error:
        print(f"Poort {PORT} is bezet ({error}) door iets anders dan deze app.")
        mark_started()  # dit ligt niet aan de code
        import subprocess
        subprocess.run(["osascript", "-e",
                        f'display alert "{APP_NAME}" message "' + transcribe.L(
                            "Er draait nog een andere ontvanger op poort 8765. Sluit die af of herstart de Mac.",
                            "Another receiver is already running on port 8765. Quit it or restart the Mac.") + '"'])
        sys.exit(1)

    import objc
    import AppKit
    import webview
    from webview.platforms.cocoa import BrowserView

    class LocalTranscribeDelegate(BrowserView.AppDelegate):
        """Delegate van pywebview, uitgebreid met links, het Dock-icoon en afsluiten."""

        def applicationShouldTerminate_(self, app):
            if not serve.busy():
                return AppKit.NSTerminateNow
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_(transcribe.L("Er loopt nog een opdracht", "A job is still running"))
            alert.setInformativeText_(transcribe.L(
                "Afsluiten breekt de opdracht af. Je kunt hem later opnieuw starten vanuit Iconik.",
                "Quitting stops the job. You can start it again from Iconik later."))
            alert.addButtonWithTitle_(transcribe.L("Annuleren", "Cancel"))
            alert.addButtonWithTitle_(transcribe.L("Toch afsluiten", "Quit anyway"))
            alert.setAlertStyle_(AppKit.NSAlertStyleWarning)
            if alert.runModal() == AppKit.NSAlertSecondButtonReturn:
                print(f"[{datetime.now():%H:%M:%S}] afgesloten tijdens een opdracht")
                return AppKit.NSTerminateNow
            return AppKit.NSTerminateCancel

        @objc.typedSelector(b"Z@:@Z")
        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible):
            show()  # klik op het Dock-icoon of de app opnieuw geopend
            return True

        @objc.typedSelector(b"v@:@@")
        def application_openURLs_(self, app, urls):
            for url in urls:
                threading.Thread(target=on_link, args=(str(url.absoluteString()),), daemon=True).start()

    BrowserView.AppDelegate = LocalTranscribeDelegate
    serve.show_hook = show  # een tweede exemplaar kan zo het venster naar voren halen
    serve.restart_hook = restart  # na een code-update

    storage = SUPPORT / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    window = webview.create_window(APP_NAME, BASE + "/", width=980, height=620, min_size=(640, 360))
    window.events.closing += on_closing
    # Niet in privemodus: zo blijft de inlog bij Iconik in dit venster bewaard.
    threading.Thread(target=watch_settings, daemon=True).start()
    webview.start(mark_started, private_mode=False, storage_path=str(storage))  # de functie draait zodra het venster er is


if __name__ == "__main__":
    main()
