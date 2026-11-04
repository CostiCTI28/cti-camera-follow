# PyInstaller spec - CTI Camera Follow (folder, fara consola)
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hidden = (collect_submodules("uvicorn") + collect_submodules("webview") +
          ["pystray._win32", "obsws_python", "cti_version"])
datas = [("src/static", "static"), ("icons", "icons")] + collect_data_files("webview")

a = Analysis(["src/launcher.py"], pathex=["src"], hiddenimports=hidden, datas=datas,
             excludes=["tkinter", "matplotlib", "numpy", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="CTI Camera Follow", icon="icons/app.ico",
          console=False, upx=False, version=None)
coll = COLLECT(exe, a.binaries, a.datas, name="CTI Camera Follow", upx=False)
