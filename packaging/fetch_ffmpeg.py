"""Baixa binários estáticos do FFmpeg/ffprobe para vendor/ffmpeg/ (embutidos no app).

Uso:  python packaging/fetch_ffmpeg.py
Se o download falhar, usa o ffmpeg do pacote imageio-ffmpeg; sem ffprobe o
Mega Sinc continua funcionando (lê os metadados via 'ffmpeg -i').
"""
from __future__ import annotations

import io
import os
import platform
import shutil
import stat
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "vendor" / "ffmpeg"

WIN = ["https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
       "ffmpeg-master-latest-win64-lgpl.zip",
       "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"]
MAC = "https://ffmpeg.martin-riedl.de/redirect/latest/macos/{arch}/{channel}/{tool}.zip"


def download(url: str) -> bytes:
    print(f"  baixando {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (build script)"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def extract(data: bytes, names: set[str]) -> list[str]:
    got = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            base = os.path.basename(info.filename)
            if base in names and not info.is_dir():
                out = DEST / base
                with z.open(info) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                out.chmod(out.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                got.append(base)
    return got


def fetch_windows() -> None:
    for url in WIN:
        try:
            got = extract(download(url), {"ffmpeg.exe", "ffprobe.exe"})
            if "ffmpeg.exe" in got and "ffprobe.exe" in got:
                return
        except Exception as e:  # noqa: BLE001
            print(f"  falhou: {e}")
    fallback_imageio("ffmpeg.exe")


def fetch_macos() -> None:
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "amd64"
    for tool in ("ffmpeg", "ffprobe"):
        ok = False
        for channel in ("release", "snapshot"):
            try:
                if tool in extract(download(MAC.format(arch=arch, channel=channel, tool=tool)),
                                   {tool}):
                    ok = True
                    break
            except Exception as e:  # noqa: BLE001
                print(f"  falhou: {e}")
        if not ok and tool == "ffmpeg":
            fallback_imageio("ffmpeg")
        elif not ok:
            print("  ffprobe indisponível — o app usará 'ffmpeg -i' para ler metadados.")


def fallback_imageio(name: str) -> None:
    try:
        import imageio_ffmpeg
        src = Path(imageio_ffmpeg.get_ffmpeg_exe())
        shutil.copy2(src, DEST / name)
        print(f"  usando ffmpeg do imageio-ffmpeg ({src.name})")
    except Exception as e:  # noqa: BLE001
        print(f"  ERRO: não foi possível obter o ffmpeg ({e})")
        sys.exit(1)


def main() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    print(f"Destino: {DEST}")
    if sys.platform == "win32":
        fetch_windows()
    elif sys.platform == "darwin":
        fetch_macos()
    else:
        for tool in ("ffmpeg", "ffprobe"):
            p = shutil.which(tool)
            if p:
                shutil.copy2(p, DEST / tool)
    print("Conteúdo:", sorted(p.name for p in DEST.iterdir()))


if __name__ == "__main__":
    main()
