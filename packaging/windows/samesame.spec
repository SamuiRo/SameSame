# PyInstaller spec for the Windows build: one folder holding the windowed
# SameSame.exe (GUI) and the console samesame.exe (CLI), sharing one runtime.
# Build with tools/build_windows.ps1 rather than calling PyInstaller directly.
# ruff: noqa
import re
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

ROOT = Path(SPECPATH).resolve().parents[1]
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "dedupe" / "__init__.py").read_text()).group(1)
ICON = str(ROOT / "dedupe" / "gui" / "assets" / "samesame.ico")
VERSION_TUPLE = tuple(int(part) for part in VERSION.split(".")[:3]) + (0,)

# Qt modules SameSame never imports. The PySide6 hooks only collect what is
# imported, so these are a guard against a transitive import dragging them in.
EXCLUDES = [
    "tkinter", "unittest", "pydoc_data", "pytest", "mypy", "ruff",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.Qt3DCore",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtPdf",
    "PySide6.QtDesigner", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtBluetooth",
    "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSensors", "PySide6.QtSerialPort",
]
DATAS = [(str(ROOT / "dedupe" / "gui" / "assets"), "dedupe/gui/assets")]


def version_info(description, internal_name, original_filename):
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=VERSION_TUPLE, prodvers=VERSION_TUPLE),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "StarLith"),
                StringStruct("FileDescription", description),
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", internal_name),
                StringStruct("LegalCopyright", "MIT License"),
                StringStruct("OriginalFilename", original_filename),
                StringStruct("ProductName", "SameSame"),
                StringStruct("ProductVersion", VERSION),
            ])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )


def analysis(script):
    return Analysis(
        [str(Path(SPECPATH) / script)],
        pathex=[str(ROOT)],
        datas=DATAS,
        excludes=EXCLUDES,
        noarchive=False,
    )


gui = analysis("entry_gui.py")
cli = analysis("entry_cli.py")

gui_exe = EXE(
    PYZ(gui.pure), gui.scripts, [],
    exclude_binaries=True, name="SameSame", icon=ICON, console=False, upx=False,
    version=version_info("SameSame media duplicate finder", "SameSame", "SameSame.exe"),
)
cli_exe = EXE(
    PYZ(cli.pure), cli.scripts, [],
    exclude_binaries=True, name="samesame-cli", icon=ICON, console=True, upx=False,
    version=version_info("SameSame command line", "samesame-cli", "samesame-cli.exe"),
)

COLLECT(
    gui_exe, gui.binaries, gui.datas,
    cli_exe, cli.binaries, cli.datas,
    strip=False, upx=False, name="SameSame",
)
