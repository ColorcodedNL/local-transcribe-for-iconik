#!/usr/bin/env python3
"""Proef: verschijnen menubalkiconen vanuit deze Python? Drie iconen, 30 seconden:
  A  het echte icoon van de app (glyph() uit app/app_main.py, zelfde code)
  B  de tekst "LT"
  C  een systeemsymbool (waveform)
Zo zie je of het aan het tekenen van ons icoon ligt of aan het plaatsen zelf.

Draaien: build/python/bin/python3.12 tools/menubar_probe.py [regular]
"""
import ast
import sys
from pathlib import Path

import AppKit
import Foundation
from PyObjCTools import AppHelper

# glyph() en GLYPH letterlijk uit app_main.py halen, zonder de rest van de app te laden.
source = (Path(__file__).resolve().parent.parent / "app" / "app_main.py").read_text()
tree = ast.parse(source)
wanted = [node for node in tree.body if (isinstance(node, ast.FunctionDef) and node.name == "glyph")
          or (isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "GLYPH" for t in node.targets))]
namespace = {}
exec(compile(ast.Module(body=wanted, type_ignores=[]), "app_main.py", "exec"), namespace)

app = AppKit.NSApplication.sharedApplication()
regular = "regular" in sys.argv[1:]   # zoals de app met venster: een gewone app met Dock-icoon
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular if regular else AppKit.NSApplicationActivationPolicyAccessory)
print("activatiebeleid:", "regular (met Dock-icoon)" if regular else "accessory (zonder Dock-icoon)")
bar = AppKit.NSStatusBar.systemStatusBar()
items = []

a = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
image = namespace["glyph"]()
a.button().setImage_(image)
items.append(("A (ons icoon)", a))
print(f"A: afbeelding {image.size().width:.1f} x {image.size().height:.1f}, template {image.isTemplate()}, geldig {image.isValid()}")

b = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
b.button().setTitle_("LT")
items.append(("B (tekst LT)", b))

c = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
symbol = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_("waveform", "test")
c.button().setImage_(symbol)
items.append(("C (systeemsymbool)", c))


def report():
    for name, item in items:
        window = item.button().window()
        frame = window.frame() if window else None
        where = f"x={frame.origin.x:.0f} breedte={frame.size.width:.0f}" if frame else "geen venster"
        print(f"{name}: zichtbaar={item.isVisible()} {where}")


AppHelper.callLater(2, report)
AppHelper.callLater(30, AppHelper.stopEventLoop)
print("30 seconden: kijk welke van A, B en C in de menubalk staan.")
AppHelper.runEventLoop()
