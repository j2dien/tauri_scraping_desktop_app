import os
import sys
import subprocess
import time
import socket
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = CURRENT_DIR / "frontend"
FRONTEND_DIST = FRONTEND_DIR / "dist"
SERVER_SCRIPT = CURRENT_DIR / "server.py"

def kill_existing_backend():
    """Hentikan proses lama yang mungkin masih menggantung di port 8008."""
    try:
        if sys.platform == "win32":
            # Cari PID yang listen di port 8008
            cmd = "Get-NetTCPConnection -LocalPort 8008 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
            subprocess.run(["powershell", "-Command", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.5)
    except Exception:
        pass

def start_backend():
    kill_existing_backend()
    print(">> Menjalankan FastAPI Backend Engine di port 8008...")
    python_exe = sys.executable
    return subprocess.Popen([python_exe, str(SERVER_SCRIPT)], cwd=str(CURRENT_DIR))

def get_js_runner():
    """Cek apakah bun tersedia, jika tidak gunakan npm."""
    try:
        subprocess.run(["bun", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return ["bun", "run"]
    except Exception:
        return ["npm.cmd", "run"]

def start_frontend_dev():
    print(">> Menjalankan Frontend Dev Server...")
    runner = get_js_runner()
    return subprocess.Popen(runner + ["dev"], cwd=str(FRONTEND_DIR))

def wait_for_backend(timeout_seconds: int = 15) -> bool:
    """Tunggu backend FastAPI siap melayani request."""
    import urllib.request
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8008/api/progress", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False

def main():
    import webview

    # 1. Pastikan frontend dist tersedia sebelum backend dinyalakan
    if not (FRONTEND_DIST / "index.html").exists():
        print(">> Melakukan build frontend production dengan Bun...")
        runner = get_js_runner()
        subprocess.run(runner + ["build"], cwd=str(FRONTEND_DIR), check=True)

    # 2. Nyalakan backend server
    server_proc = start_backend()

    # 3. Tunggu backend ready
    print(">> Menunggu backend server siap...")
    wait_for_backend(15)

    print(">> Membuka Jendela Desktop GUI...")
    window = webview.create_window(
        title="Social Media Top Commenter Analyzer — Desktop App",
        url="http://127.0.0.1:8008",
        width=1380,
        height=890,
        min_size=(1080, 700),
        background_color="#0b0f19",
    )

    try:
        webview.start(debug=False)
    finally:
        if server_proc:
            print(">> Menutup backend server...")
            server_proc.terminate()
            kill_existing_backend()

if __name__ == "__main__":
    main()
