"""Start a detached local supervisor using the current authorized session."""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import socket
import subprocess
import sys
import threading
import time

from .runner import lock
from .store import now


def _process_matches(config, pid, command):
    if type(pid) is not int or pid < 2:
        return False
    try:
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=3)
        expected = f"{config.root / 'start.py'} {command}"
        return result.returncode == 0 and expected in result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False


def status(config):
    try:
        data = json.loads((config.root / "service" / "state.json").read_text())
    except (OSError, ValueError):
        data = {}
    pid = data.get("supervisor_pid")
    worker = data.get("worker_pid")
    qq_worker = data.get("qq_worker_pid")
    video_worker = data.get("video_worker_pid")
    running = _process_matches(config, pid, "supervise")
    return {"backend": "detached-supervisor", "running": running,
            "pid": pid if running else None,
            "worker_running": running and _process_matches(config, worker, "run"),
            "worker_pid": worker if running else None,
            "qq_worker_running": running and _process_matches(config, qq_worker, "qq run"),
            "qq_worker_pid": qq_worker if running else None,
            "video_worker_running": running and _process_matches(config,video_worker,"videos worker"),
            "video_worker_pid":video_worker if running else None,
            "dashboard_url": f"http://127.0.0.1:{config.port}",
            "scope": "本机后台进程；重启电脑后需重新 start"}


def service(config, action):
    current = status(config)
    if action == "status":
        return current
    if action == "stop":
        if current["running"]:
            os.kill(current["pid"], signal.SIGTERM)
            for _ in range(100):
                if not _process_matches(config, current["pid"], "supervise"):
                    return status(config)
                time.sleep(0.1)
            raise ValueError("已发送停止信号，后台仍在等待当前请求结束，请稍后检查 service status")
        return current
    if current["running"]:
        return current
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", config.port)) == 0:
            raise ValueError("状态页端口已占用，请检查已有 ./monitor run 进程或修改 config.json")
    with (config.root / "logs" / "service-stderr.log").open("ab") as error_log:
        subprocess.Popen([sys.executable, str(config.root / "start.py"), "supervise"],
                         cwd=config.root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=error_log, start_new_session=True, close_fds=True)
    for _ in range(50):
        current = status(config)
        if current["worker_running"]:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", config.port)) == 0:
                    return current
        time.sleep(0.1)
    raise ValueError("后台尚未就绪，请查看 logs/service-stderr.log 和 logs/supervisor.log")


def supervise(config):
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    logger = logging.getLogger("supervisor")
    handler = RotatingFileHandler(config.root / "logs" / "supervisor.log", maxBytes=1_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    state_file = config.root / "service" / "state.json"
    child = None
    qq_child = None
    video_child = None
    video_retry_at = 0
    qq_retry_at = 0
    qq_python = config.root / ".qq-venv" / "bin" / "python"

    def save_processes():
        temporary = state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps({"supervisor_pid": os.getpid(), "worker_pid": child.pid,
                                        "qq_worker_pid": qq_child.pid if qq_child else None,
                                        "video_worker_pid":video_child.pid if video_child else None,"started_at": now()}))
        os.replace(temporary, state_file)

    with lock(config, "supervisor.lock"):
        try:
            while not stopped.is_set():
                child = subprocess.Popen([sys.executable, str(config.root / "start.py"), "run"],
                                         cwd=config.root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, close_fds=True)
                save_processes()
                logger.info("worker_started pid=%s", child.pid)
                while not stopped.wait(0.5) and child.poll() is None:
                    ready = qq_python.exists() and (config.root / "qq_bot.json").exists() and (config.root / "secrets" / "qq_app_secret").exists()
                    if ready and (qq_child is None or qq_child.poll() is not None) and time.time() >= qq_retry_at:
                        qq_child = subprocess.Popen([str(qq_python), str(config.root / "start.py"), "qq", "run"],
                                                    cwd=config.root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                    stderr=subprocess.DEVNULL, close_fds=True)
                        qq_retry_at = time.time() + 60
                        save_processes()
                        logger.info("qq_worker_started pid=%s", qq_child.pid)
                    try:
                        video_ready = ready and json.loads((config.root/"qq_bot.json").read_text()).get("send_video_files") is True
                    except (OSError,ValueError):video_ready = False
                    if video_ready and (video_child is None or video_child.poll() is not None) and time.time()>=video_retry_at:
                        video_child = subprocess.Popen([sys.executable,str(config.root/"start.py"),"videos","worker"],
                            cwd=config.root,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True)
                        video_retry_at = time.time()+60;save_processes()
                        logger.info("video_worker_started pid=%s",video_child.pid)
                if stopped.is_set():
                    break
                logger.warning("worker_exited code=%s restarting_in=30s", child.returncode)
                stopped.wait(30)
        finally:
            if video_child and video_child.poll() is None:
                video_child.terminate()
                try:video_child.wait(timeout=15)
                except subprocess.TimeoutExpired:video_child.kill();video_child.wait()
            if qq_child and qq_child.poll() is None:
                qq_child.terminate()
                try:
                    qq_child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    qq_child.kill()
                    qq_child.wait()
            if child and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=config.request_timeout + 5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            state_file.unlink(missing_ok=True)
            logger.info("supervisor_stopped")
