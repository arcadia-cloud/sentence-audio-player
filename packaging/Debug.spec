# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all

ROOT = r"D:\Users\Lenovo\Documents\deepseek-harness\default-workspace\listening-app"
SPIKE = r"D:\Users\Lenovo\Documents\deepseek-harness\default-workspace\listening-spike"
BACKEND = os.path.join(ROOT, "backend")
FRONTEND = os.path.join(ROOT, "frontend")
MODEL = os.path.join(SPIKE, "out", "models", "small.en")
BIN = os.path.join(ROOT, "packaging", "assets", "bin")
CUDA = os.path.join(ROOT, "packaging", "assets", "cuda")

datas = [
    (FRONTEND, "frontend"),
    (MODEL, os.path.join("models", "small.en")),
]
binaries = []
hiddenimports = [
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    "app.main", "app.worker", "app.core", "app.db", "app.audio",
    "python_multipart", "multipart",
]
if os.path.isdir(BIN):
    datas.append((BIN, "bin"))
if os.path.isdir(CUDA):
    datas.append((CUDA, "cuda"))

for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "tokenizers", "av"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

a = Analysis(
    [os.path.join(ROOT, "packaging", "launcher.py")],
    pathex=[BACKEND],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ListeningStudioConsole",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ListeningStudioConsole",
)
