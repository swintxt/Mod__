# -*- coding: utf-8 -*-
"""
Mod Builder — собирает исходники Minecraft Forge-модов (Gradle/ForgeGradle) в .jar.

Принимает ZIP-архивы с исходниками или готовые папки проектов, сам скачивает
нужные JDK (по умолчанию 8) и Gradle (по умолчанию 4.10.3), запускает сборку
и складывает готовые .jar в выбранную папку.

Запуск:  python mod_builder.py
Сборка .exe: pyinstaller --onefile --windowed --name ModBuilder mod_builder.py
"""
import os
import sys
import shutil
import queue
import platform
import tempfile
import threading
import subprocess
import urllib.request
import zipfile
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except Exception:  # на сервере без tkinter ядро всё равно можно импортировать
    tk = None

APP_NAME = "Mod Builder"
IS_WIN = os.name == "nt"
DEFAULT_JAVA = "8"
DEFAULT_GRADLE = "4.10.3"

TOOLS_DIR = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ModBuilder" / "tools"
    if IS_WIN
    else Path.home() / ".modbuilder" / "tools"
)

TASK_PRESETS = {
    "Авто (по build.gradle)": None,
    "setupCiWorkspace build": ["setupCiWorkspace", "build"],
    "setupDecompWorkspace build": ["setupDecompWorkspace", "build"],
    "Только build": ["build"],
}


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- #
#                                   ЯДРО                                       #
# --------------------------------------------------------------------------- #
class Builder:
    def __init__(self, log, java_major=DEFAULT_JAVA, gradle_version=DEFAULT_GRADLE,
                 tasks=None, keep_sources=False, tools_dir=TOOLS_DIR, refresh=False):
        self.log = log
        self.java_major = str(java_major).strip() or DEFAULT_JAVA
        self.gradle_version = gradle_version.strip() or DEFAULT_GRADLE
        self.tasks = tasks            # None = авто
        self.keep_sources = keep_sources
        self.refresh = refresh
        self.tools_dir = Path(tools_dir)
        self._cancel = threading.Event()
        self._proc = None

    # ----- управление ----- #
    def cancel(self):
        self._cancel.set()
        p = self._proc
        if p and p.poll() is None:
            try:
                if IS_WIN:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                                   creationflags=subprocess.CREATE_NO_WINDOW,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    import signal
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def _check(self):
        if self._cancel.is_set():
            raise Cancelled()

    # ----- утилиты ----- #
    @staticmethod
    def safe_extract(zip_path, dest):
        dest = Path(dest).resolve()
        with zipfile.ZipFile(zip_path) as z:
            for m in z.infolist():
                target = (dest / m.filename).resolve()
                if dest != target and dest not in target.parents:
                    raise RuntimeError("Небезопасный путь в архиве: " + m.filename)
            z.extractall(dest)
        if not IS_WIN:
            for f in dest.rglob("*"):
                if f.is_file() and (f.parent.name == "bin" or f.name == "gradlew"):
                    f.chmod(f.stat().st_mode | 0o111)

    @staticmethod
    def find_project_root(base):
        """Самая верхняя папка, где лежит build.gradle(.kts)."""
        base = Path(base)
        best = None
        for name in ("build.gradle", "build.gradle.kts"):
            for p in base.rglob(name):
                if "__MACOSX" in p.parts:
                    continue
                if best is None or len(p.parts) < len(best.parts):
                    best = p
        return best.parent if best else None

    def _download(self, url, dest):
        self.log("Скачиваю: " + url)
        req = urllib.request.Request(url, headers={"User-Agent": "ModBuilder/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done, last = 0, -10
            while True:
                self._check()
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    if pct >= last + 10:
                        last = pct
                        self.log("  %d%%" % pct)

    # ----- Java ----- #
    @staticmethod
    def _java_exe(home):
        return Path(home) / "bin" / ("java.exe" if IS_WIN else "java")

    def _java_major_of(self, home):
        exe = self._java_exe(home)
        if not exe.exists():
            return None
        try:
            out = subprocess.run(
                [str(exe), "-version"], capture_output=True, text=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0,
                stdin=subprocess.DEVNULL)
            text = (out.stderr or "") + (out.stdout or "")
            q = text.split('"')
            if len(q) < 2:
                return None
            v = q[1]
            return v.split(".")[1] if v.startswith("1.") else v.split(".")[0].split("-")[0]
        except Exception:
            return None

    def ensure_java(self):
        want = self.java_major
        # 1) наш каталог
        local = self.tools_dir / ("jdk" + want)
        if self._java_major_of(local) == want:
            self.log("JDK %s: %s" % (want, local))
            return local
        # 2) JAVA_HOME
        jh = os.environ.get("JAVA_HOME")
        if jh and self._java_major_of(jh) == want:
            self.log("JDK %s из JAVA_HOME: %s" % (want, jh))
            return Path(jh)
        # 3) скачиваем Temurin
        self.log("JDK %s не найден — скачиваю Temurin (один раз)..." % want)
        osname = "windows" if IS_WIN else ("mac" if sys.platform == "darwin" else "linux")
        arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
        url = ("https://api.adoptium.net/v3/binary/latest/%s/ga/%s/%s/jdk/hotspot/normal/eclipse"
               % (want, osname, arch))
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="jdk_", dir=self.tools_dir))
        try:
            archive = tmp / "jdk.zip"
            if not IS_WIN:
                archive = tmp / "jdk.tar.gz"
            self._download(url, archive)
            self.log("Распаковываю JDK...")
            ex = tmp / "x"
            ex.mkdir()
            if archive.suffix == ".zip":
                self.safe_extract(archive, ex)
            else:
                import tarfile
                with tarfile.open(archive) as t:
                    t.extractall(ex)
            home = next((p.parent.parent for p in ex.rglob("java*")
                         if p.parent.name == "bin" and p.name in ("java", "java.exe")), None)
            if home is None:
                raise RuntimeError("В скачанном JDK не найден java")
            if local.exists():
                shutil.rmtree(local, ignore_errors=True)
            shutil.move(str(home), str(local))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.log("JDK установлен: %s" % local)
        return local

    # ----- Gradle ----- #
    def ensure_gradle(self):
        ver = self.gradle_version
        home = self.tools_dir / ("gradle-" + ver)
        exe = home / "bin" / ("gradle.bat" if IS_WIN else "gradle")
        if exe.exists():
            self.log("Gradle %s: %s" % (ver, home))
            return exe
        self.log("Gradle %s не найден — скачиваю (один раз)..." % ver)
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="gr_", dir=self.tools_dir))
        try:
            z = tmp / "gradle.zip"
            self._download("https://services.gradle.org/distributions/gradle-%s-bin.zip" % ver, z)
            self.log("Распаковываю Gradle...")
            self.safe_extract(z, tmp / "x")
            inner = tmp / "x" / ("gradle-" + ver)
            if home.exists():
                shutil.rmtree(home, ignore_errors=True)
            shutil.move(str(inner), str(home))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return exe

    # ----- запуск процесса ----- #
    def _run(self, cmd, cwd, env):
        self.log("$ " + " ".join(cmd))
        kw = dict(cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                  stdin=subprocess.DEVNULL)
        if IS_WIN:
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        self._proc = subprocess.Popen(cmd, **kw)
        for raw in iter(self._proc.stdout.readline, b""):
            self.log(raw.decode("utf-8", "replace").rstrip())
        code = self._proc.wait()
        self._proc = None
        self._check()
        return code

    # ----- сборка одного мода ----- #
    def build_one(self, source, out_dir, java_home=None, gradle_exe=None):
        """source — путь к .zip или к папке. Возвращает список скопированных jar."""
        source = Path(source)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="mb_"))
        try:
            self._check()
            if source.is_dir():
                self.log("Копирую проект: %s" % source)
                shutil.copytree(source, work / "p", ignore=shutil.ignore_patterns(
                    "build", ".gradle", "run", ".git"))
            else:
                self.log("Распаковываю: %s" % source.name)
                self.safe_extract(source, work / "p")
            root = self.find_project_root(work / "p")
            if root is None:
                raise RuntimeError("В %s не найден build.gradle" % source.name)
            self.log("Корень проекта: %s" % root.relative_to(work))

            java_home = java_home or self.ensure_java()
            self._check()

            env = os.environ.copy()
            env["JAVA_HOME"] = str(java_home)
            env["PATH"] = str(Path(java_home) / "bin") + os.pathsep + env.get("PATH", "")

            wrapper = root / ("gradlew.bat" if IS_WIN else "gradlew")
            wrapper_jar = root / "gradle" / "wrapper" / "gradle-wrapper.jar"
            if wrapper.exists() and wrapper_jar.exists():
                self.log("Использую gradlew из проекта")
                if not IS_WIN:
                    wrapper.chmod(wrapper.stat().st_mode | 0o111)
                gradle_cmd = [str(wrapper)]
            else:
                gradle_cmd = [str(gradle_exe or self.ensure_gradle())]

            tasks = self.tasks
            if tasks is None:
                script = ""
                for n in ("build.gradle", "build.gradle.kts"):
                    if (root / n).exists():
                        script = (root / n).read_text("utf-8", "replace")
                        break
                is_fg2 = "net.minecraftforge.gradle.forge" in script or "ForgeGradle:2" in script
                tasks = ["setupCiWorkspace", "build"] if is_fg2 else ["build"]
                self.log("Задачи (авто): " + " ".join(tasks))

            code = self._run(gradle_cmd + list(tasks) + ["--no-daemon", "--stacktrace"]
                             + (["--refresh-dependencies"] if self.refresh else []), root, env)
            if code != 0:
                raise RuntimeError("Gradle завершился с кодом %d" % code)

            libs = root / "build" / "libs"
            jars = sorted(libs.glob("*.jar")) if libs.exists() else []
            if not self.keep_sources:
                main = [j for j in jars if not j.stem.endswith(("-sources", "-javadoc", "-dev"))]
                jars = main or jars
            if not jars:
                raise RuntimeError("Сборка прошла, но в build/libs нет .jar")
            copied = []
            for j in jars:
                dst = out_dir / j.name
                shutil.copy2(j, dst)
                copied.append(dst)
                self.log("✔ Готово: %s" % dst)
            return copied
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def build_many(self, sources, out_dir):
        """Возвращает (успешных, всего)."""
        ok = 0
        java_home = gradle_exe = None
        try:
            for i, s in enumerate(sources, 1):
                self._check()
                self.log("\n=== [%d/%d] %s ===" % (i, len(sources), Path(s).name))
                try:
                    java_home = java_home or self.ensure_java()
                    # gradle скачиваем лениво внутри build_one, если нет gradlew
                    self.build_one(s, out_dir, java_home, gradle_exe)
                    ok += 1
                except Cancelled:
                    raise
                except Exception as e:
                    self.log("✘ Ошибка: %s" % e)
        except Cancelled:
            self.log("\nОстановлено пользователем.")
        return ok, len(sources)


# --------------------------------------------------------------------------- #
#                                   GUI                                        #
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.geometry("820x640")
        root.minsize(700, 520)
        self.q = queue.Queue()
        self.builder = None
        self.thread = None

        main = ttk.Frame(root, padding=10)
        main.pack(fill="both", expand=True)

        # источники
        ttk.Label(main, text="Моды для сборки (ZIP с исходниками или папки проектов):").pack(anchor="w")
        box = ttk.Frame(main)
        box.pack(fill="x", pady=(2, 6))
        self.lb = tk.Listbox(box, height=6, selectmode="extended")
        sb = ttk.Scrollbar(box, command=self.lb.yview)
        self.lb.configure(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        btns = ttk.Frame(box)
        btns.pack(side="left", padx=(8, 0), fill="y")
        for text, cmd in (("Добавить ZIP…", self.add_zip), ("Добавить папку…", self.add_dir),
                          ("Удалить", self.remove_sel), ("Очистить", lambda: self.lb.delete(0, "end"))):
            ttk.Button(btns, text=text, command=cmd).pack(fill="x", pady=1)

        # выход
        row = ttk.Frame(main)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Папка для .jar:").pack(side="left")
        self.out_var = tk.StringVar(value=str(Path.home() / "ModBuilder" / "jars"))
        ttk.Entry(row, textvariable=self.out_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="Обзор…", command=self.pick_out).pack(side="left")

        # настройки
        opt = ttk.LabelFrame(main, text="Настройки", padding=6)
        opt.pack(fill="x", pady=6)
        ttk.Label(opt, text="Java:").grid(row=0, column=0, sticky="w")
        self.java_var = tk.StringVar(value=DEFAULT_JAVA)
        ttk.Combobox(opt, textvariable=self.java_var, width=5, values=("8", "11", "17", "21")
                     ).grid(row=0, column=1, padx=(4, 14))
        ttk.Label(opt, text="Gradle:").grid(row=0, column=2, sticky="w")
        self.gr_var = tk.StringVar(value=DEFAULT_GRADLE)
        ttk.Entry(opt, textvariable=self.gr_var, width=8).grid(row=0, column=3, padx=(4, 14))
        ttk.Label(opt, text="Задачи:").grid(row=0, column=4, sticky="w")
        self.task_var = tk.StringVar(value=list(TASK_PRESETS)[0])
        ttk.Combobox(opt, textvariable=self.task_var, width=28, state="readonly",
                     values=list(TASK_PRESETS)).grid(row=0, column=5, padx=4)
        self.src_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Копировать также *-sources.jar / *-dev.jar",
                        variable=self.src_var).grid(row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))

        self.refresh_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Сбросить кэш зависимостей (--refresh-dependencies) — включите, если ошибка «Could not find» повторяется",
                        variable=self.refresh_var).grid(row=2, column=0, columnspan=6, sticky="w")

        # действия
        act = ttk.Frame(main)
        act.pack(fill="x", pady=4)
        self.go_btn = ttk.Button(act, text="▶ Собрать", command=self.start)
        self.go_btn.pack(side="left")
        self.stop_btn = ttk.Button(act, text="■ Стоп", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(act, text="Открыть папку с .jar", command=self.open_out).pack(side="left")
        ttk.Button(act, text="Копировать лог", command=self.copy_log).pack(side="left", padx=6)
        ttk.Button(act, text="Сохранить лог…", command=self.save_log).pack(side="left")
        self.prog = ttk.Progressbar(act, mode="indeterminate", length=160)
        self.prog.pack(side="right")

        # лог
        lf = ttk.Frame(main)
        lf.pack(fill="both", expand=True)
        self.log_txt = tk.Text(lf, height=12, wrap="none", state="disabled",
                               font=("Consolas", 9) if IS_WIN else ("monospace", 9))
        ls = ttk.Scrollbar(lf, command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=ls.set)
        self.log_txt.pack(side="left", fill="both", expand=True)
        ls.pack(side="left", fill="y")

        self.log("Первая сборка скачает JDK и Gradle (≈ 300 МБ) — это делается один раз.")
        self.poll()

    # --- события --- #
    def add_zip(self):
        for f in filedialog.askopenfilenames(title="Выберите ZIP с исходниками мода",
                                             filetypes=[("ZIP", "*.zip"), ("Все файлы", "*.*")]):
            self.lb.insert("end", f)

    def add_dir(self):
        d = filedialog.askdirectory(title="Выберите папку проекта мода")
        if d:
            self.lb.insert("end", d)

    def remove_sel(self):
        for i in reversed(self.lb.curselection()):
            self.lb.delete(i)

    def pick_out(self):
        d = filedialog.askdirectory(title="Куда сохранять .jar")
        if d:
            self.out_var.set(d)

    def open_out(self):
        p = Path(self.out_var.get())
        p.mkdir(parents=True, exist_ok=True)
        if IS_WIN:
            os.startfile(str(p))  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])

    def log(self, msg):
        self.q.put(msg)

    def _log_text(self):
        return self.log_txt.get("1.0", "end")

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self._log_text())

    def save_log(self):
        f = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="modbuilder_log.txt")
        if f:
            Path(f).write_text(self._log_text(), encoding="utf-8")

    def poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m is None:
                    self.finish()
                    continue
                self.log_txt.configure(state="normal")
                self.log_txt.insert("end", m + "\n")
                self.log_txt.see("end")
                self.log_txt.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def start(self):
        sources = list(self.lb.get(0, "end"))
        if not sources:
            messagebox.showwarning(APP_NAME, "Добавьте хотя бы один ZIP или папку.")
            return
        self.builder = Builder(self.log, self.java_var.get(), self.gr_var.get(),
                               TASK_PRESETS[self.task_var.get()], self.src_var.get(),
                               refresh=self.refresh_var.get())
        out = self.out_var.get()
        self.go_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.prog.start(12)

        def work():
            try:
                ok, total = self.builder.build_many(sources, out)
                self.log("\nИтог: собрано %d из %d. Папка: %s" % (ok, total, out))
            except Exception as e:
                self.log("Критическая ошибка: %s" % e)
            finally:
                self.q.put(None)

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def stop(self):
        if self.builder:
            self.builder.cancel()

    def finish(self):
        self.prog.stop()
        self.go_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")


def main():
    if tk is None:
        print("tkinter не установлен")
        return 1
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if IS_WIN else "clam")
    except Exception:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
