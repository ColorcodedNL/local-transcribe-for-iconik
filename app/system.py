"""Wat per besturingssysteem verschilt, onder vaste namen. De rest van de app gebruikt alleen deze module.

Een versie voor een ander systeem (platform_windows.py, platform_linux.py) heeft dezelfde namen en komt
erbij in IMPLEMENTATIONS. Zolang er alleen de Mac-versie is, draait die overal: ontbrekende commando's
vangt hij op, en zo doen de tests op Linux hetzelfde als op een Mac.
"""
import importlib
import sys

IMPLEMENTATIONS = {"darwin": "platform_mac"}
_current = importlib.import_module(IMPLEMENTATIONS.get(sys.platform, "platform_mac"))

NAMES = ["NAME", "support_dir", "logs_dir", "process_footprint",
         "PRESSURE_NORMAL", "on_mains_power", "memory", "CpuTicks", "gpu_percent",
         "ask_folder", "find_moved_mount", "mount_reachable", "volume_place",
         "keep_awake_command", "open_url", "login_item_on", "set_login_item", "computer_name"]

NAME = _current.NAME
support_dir, logs_dir = _current.support_dir, _current.logs_dir
process_footprint = _current.process_footprint
PRESSURE_NORMAL, on_mains_power, memory = _current.PRESSURE_NORMAL, _current.on_mains_power, _current.memory
CpuTicks, gpu_percent = _current.CpuTicks, _current.gpu_percent
ask_folder, find_moved_mount, mount_reachable = _current.ask_folder, _current.find_moved_mount, _current.mount_reachable
volume_place = _current.volume_place
keep_awake_command, open_url = _current.keep_awake_command, _current.open_url
login_item_on, set_login_item = _current.login_item_on, _current.set_login_item
computer_name = _current.computer_name
