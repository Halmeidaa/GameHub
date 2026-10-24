"""GameHub - biblioteca de jogos para Windows."""


# Ajusta a escala da tela antes de carregar a interface.

import ctypes
import os
import sys
import traceback


def enable_high_dpi() -> bool:
    """Evita que o Windows deixe os textos e botões borrados.

    Nem toda versão do Windows usa o mesmo ajuste, por isso tentamos
    mais de uma opção antes de abrir a janela.
    """
    if sys.platform != "win32":
        return False
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True
    except (AttributeError, OSError, ValueError):
        pass
    try:
        # Segunda opção para versões do Windows que não aceitam o ajuste acima.
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return True
    except (AttributeError, OSError, ValueError):
        pass
    try:
        return bool(ctypes.windll.user32.SetProcessDPIAware())
    except (AttributeError, OSError, ValueError):
        return False


def ui_scale(window) -> float:
    """Lê a escala do monitor para dimensionar os elementos da janela."""
    # Também dá para forçar uma escala pela variável de ambiente, se precisar.
    override = os.environ.get("GAMEHUB_UI_SCALE")
    if override:
        try:
            return max(1.0, min(2.5, float(override)))
        except ValueError:
            pass
    if sys.platform != "win32":
        return 1.0
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(window.winfo_id())
        if dpi:
            return max(1.0, min(2.5, dpi / 96.0))
    except (AttributeError, OSError, ValueError):
        pass
    try:
        return max(1.0, min(2.5, ctypes.windll.user32.GetDpiForSystem() / 96.0))
    except (AttributeError, OSError, ValueError):
        return 1.0


# Os jogos cadastrados e o tempo de uso são guardados no próprio computador.

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4


def data_file() -> Path:
    """Escolhe onde salvar os cadastros, sem misturá-los aos arquivos do programa."""
    override = os.environ.get("GAMEHUB_DATA_DIR")
    if override:
        return Path(override) / "jogos.json"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "GameHub" / "jogos.json"
    return Path.home() / ".gamehub" / "jogos.json"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class GameStore:
    def __init__(self, file_path: Path | None = None):
        self.file_path = Path(file_path) if file_path else data_file()
        self.games = self._load()

    def _load(self) -> list[dict]:
        if not self.file_path.exists():
            return []
        try:
            data = json.loads(self.file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Não foi possível ler o arquivo de jogos:\n{self.file_path}\n\n{exc}") from exc
        if not isinstance(data, list) or not all(isinstance(game, dict) for game in data):
            raise RuntimeError(f"O arquivo de jogos tem formato inválido:\n{self.file_path}")
        # Cadastros antigos continuam funcionando normalmente.
        for game in data:
            game.pop("run_as_admin", None)
        return data

    def _save(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        # Salva primeiro em um arquivo temporário para não estragar o JSON
        # caso a gravação seja interrompida no meio.
        temp = self.file_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.games, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self.file_path)

    def add(self, name: str, executable: str, category: str, cover: str) -> dict:
        game = {
            "id": uuid4().hex,
            "name": name,
            "executable": executable,
            "category": category,
            "cover": cover,
            "seconds_played": 0,
            "last_played": None,
            "created_at": now_iso(),
        }
        self.games.append(game)
        self._save()
        return game

    def update(self, game_id: str, name: str, executable: str, category: str, cover: str) -> None:
        game = self.get(game_id)
        game.update(name=name, executable=executable, category=category, cover=cover)
        self._save()

    def get(self, game_id: str) -> dict:
        for game in self.games:
            if game.get("id") == game_id:
                return game
        raise KeyError(f"Jogo não encontrado: {game_id}")

    def remove(self, game_id: str) -> None:
        self.games = [game for game in self.games if game.get("id") != game_id]
        self._save()

    def record_session(self, game_id: str, seconds: int) -> None:
        # Somamos o tempo novo ao total que já estava salvo.
        game = self.get(game_id)
        game["seconds_played"] = int(game.get("seconds_played", 0)) + max(0, int(seconds))
        game["last_played"] = now_iso()
        self._save()

    def total_seconds(self) -> int:
        return sum(max(0, int(game.get("seconds_played", 0))) for game in self.games)


# Acompanha os jogos iniciados aqui para contar as horas e poder fechá-los.

import os
import subprocess
import time
from pathlib import Path

import psutil


def normalized_path(value: str) -> str:
    return os.path.normcase(os.path.abspath(value))


class WindowsGameProcess:
    """Guarda o identificador do processo aberto pelo Windows.

    Ele permite acompanhar um jogo que pediu UAC sem precisar executar
    o GameHub inteiro como administrador.
    """

    def __init__(self, handle: int):
        from ctypes import wintypes
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.GetProcessId.argtypes = [wintypes.HANDLE]
        self._kernel.GetProcessId.restype = wintypes.DWORD
        self._kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL
        self._handle = handle
        self.pid = self._kernel.GetProcessId(wintypes.HANDLE(handle))
        if not self.pid:
            code = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(code)

    def poll(self):
        if not self._handle:
            return 0
        # Não trava a interface: apenas consulta se o jogo ainda está aberto.
        result = self._kernel.WaitForSingleObject(ctypes.c_void_p(self._handle), 0)
        if result == 0x102:  # WAIT_TIMEOUT
            return None
        if result == 0:  # WAIT_OBJECT_0
            return 0
        raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        # Soltar o identificador não fecha o jogo, só libera o recurso do Windows.
        if self._handle:
            self._kernel.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None


def open_windows_game(executable: str) -> WindowsGameProcess:
    """O Windows solicita autorização quando o jogo precisar dela."""
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell.ShellExecuteExW.restype = wintypes.BOOL

    def execute(verb):
        options = SHELLEXECUTEINFOW()
        options.cbSize = ctypes.sizeof(options)
        options.fMask = 0x00000040  # Guarda o processo para acompanhar o tempo jogado.
        options.lpVerb = verb
        options.lpFile = executable
        options.lpDirectory = str(Path(executable).parent)
        options.nShow = 1
        if not shell.ShellExecuteExW(ctypes.byref(options)):
            return None, ctypes.get_last_error()
        return options.hProcess, 0

    # Normalmente o Windows já reconhece executáveis que precisam de UAC.
    handle, error = execute("open")
    if error == 740:
        # Se ele exigir elevação de forma explícita, solicitamos ao usuário.
        handle, error = execute("runas")
    if error == 1223:
        raise RuntimeError("A autorização do Windows foi cancelada. O jogo não foi aberto.")
    if error in (5, 740):
        raise RuntimeError("O Windows não permitiu abrir este jogo. Confira as permissões do executável.")
    if error:
        raise ctypes.WinError(error)
    if not handle:
        raise RuntimeError("O Windows abriu o executável, mas não foi possível acompanhar o jogo.")
    return WindowsGameProcess(handle)


class SessionTracker:
    """Cuida dos jogos que foram abertos pelo GameHub.

    Cada sessão guarda os processos ligados ao jogo. Isso evita fechar,
    por engano, outro programa que tenha o mesmo nome.
    """

    def __init__(self):
        self.sessions: dict[str, dict] = {}

    def start(self, game_id: str, executable: str) -> None:
        if game_id in self.sessions:
            raise RuntimeError("Este jogo já está em execução pelo GameHub.")
        executable = str(Path(executable).resolve())
        if not Path(executable).is_file():
            raise FileNotFoundError(f"Executável não encontrado:\n{executable}")
        # Abre o jogo na própria pasta dele. Alguns jogos precisam disso
        # para encontrar arquivos que ficam junto do executável.
        # No Windows, o ShellExecuteEx também abre jogos que pedem autorização.
        # Em outros sistemas, continuamos usando o processo comum.
        if sys.platform == "win32":
            process = open_windows_game(executable)
        else:
            process = subprocess.Popen([executable], cwd=str(Path(executable).parent))
        started = time.monotonic()
        try:
            created = psutil.Process(process.pid).create_time()
        except psutil.Error:
            # Alguns launchers fecham o processo inicial logo após abrir o jogo.
            created = None
        self.sessions[game_id] = {
            "process": process,
            "start": started,
            "last_seen": started,
            "owned": {process.pid: created},
            "had_children": False,
        }
        self.refresh_game(game_id)

    def elapsed(self, game_id: str) -> int:
        session = self.sessions.get(game_id)
        if not session:
            return 0
        return max(0, int(time.monotonic() - session["start"]))

    def _live_owned(self, session: dict) -> list[psutil.Process]:
        """Confere quais processos desta sessão ainda estão abertos.

        O horário de criação é importante porque o Windows pode reutilizar
        o número de um processo depois que ele termina.
        """
        live: dict[int, psutil.Process] = {}
        for pid, created in list(session["owned"].items()):
            if created is None:
                # Se o jogo abriu muito rápido, tentamos descobrir seu horário
                # de criação enquanto o processo original ainda existe.
                if pid != session["process"].pid or session["process"].poll() is not None:
                    continue
                try:
                    created = psutil.Process(pid).create_time()
                    session["owned"][pid] = created
                except (psutil.Error, OSError):
                    continue
            try:
                candidate = psutil.Process(pid)
                if abs(candidate.create_time() - created) <= 0.1 and candidate.is_running() \
                        and candidate.status() != psutil.STATUS_ZOMBIE:
                    live[pid] = candidate
            except (psutil.Error, OSError):
                continue
        # Alguns jogos abrem outros processos. Guardamos esses filhos também,
        # mas só quando conseguimos ligá-los a esta sessão.
        pending = list(live.values())
        while pending:
            parent = pending.pop()
            try:
                children = parent.children(recursive=False)
            except (psutil.Error, OSError):
                continue
            for child in children:
                if child.pid in live:
                    continue
                try:
                    born = child.create_time()
                    if not child.is_running() or child.status() == psutil.STATUS_ZOMBIE:
                        continue
                    original = session["owned"].get(child.pid)
                    if original is not None and abs(original - born) > 0.1:
                        continue
                    session["owned"][child.pid] = born
                    session["had_children"] = True
                    live[child.pid] = child
                    pending.append(child)
                except (psutil.Error, OSError):
                    continue
        return list(live.values())

    def refresh_game(self, game_id: str) -> None:
        """Procura novos processos do jogo sem interferir na execução."""
        session = self.sessions.get(game_id)
        if session:
            self._live_owned(session)

    def poll(self) -> list[tuple[str, int]]:
        """Verifica quais jogos fecharam e quanto tempo ficaram abertos."""
        now = time.monotonic()
        ended = []
        for game_id, session in list(self.sessions.items()):
            running = bool(self._live_owned(session))
            if session["process"].poll() is None:
                running = True
            if running:
                session["last_seen"] = now
            # Espera alguns segundos antes de considerar a sessão encerrada.
            # Isso ajuda nos jogos que trocam de processo durante a abertura.
            elif now - session["last_seen"] >= 3:
                ended.append((game_id, max(0, int(now - session["start"]))))
                del self.sessions[game_id]
                self._close_process_handle(session)
        return ended

    def stop(self, game_id: str) -> int | None:
        """Pede o encerramento e aguarda a confirmação do Windows.

        Um processo pode recusar o comando e fechar por conta própria logo
        depois. Por isso, um erro de permissão aqui não significa que o jogo
        continuou aberto.
        """
        session = self.sessions.get(game_id)
        if session is None:
            raise RuntimeError("O jogo já não está em execução pelo GameHub.")

        processes = self._live_owned(session)
        # Fecha os processos filhos antes do principal.
        ordered = sorted(processes, key=lambda proc: proc.pid == session["process"].pid)
        for proc in ordered:
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                # Só mostraremos um erro se o jogo realmente continuar aberto.
                pass

        # Não bloqueia a interface esperando o jogo fechar. A janela vai
        # conferir novamente em intervalos curtos.
        return self.finish_if_closed(game_id)

    def finish_if_closed(self, game_id: str) -> int | None:
        """Termina a sessão se não houver mais processos em execução."""
        session = self.sessions.get(game_id)
        if session is None:
            return None
        if self._live_owned(session) or session["process"].poll() is None:
            return None
        seconds = self.elapsed(game_id)
        del self.sessions[game_id]
        self._close_process_handle(session)
        return seconds

    def closing_status(self, game_id: str) -> str:
        """Diz se o jogo fechou, ainda está aberto ou não dá para confirmar.

        Não usamos a presença de um launcher ou de um processo auxiliar como
        prova de que o jogo em si continua rodando.
        """
        session = self.sessions.get(game_id)
        if not session:
            return "closed"
        try:
            active = self._live_owned(session)
            main_active = session["process"].poll() is None
        except (OSError, psutil.Error):
            return "unknown"
        if not active and not main_active:
            return "closed"
        # Se o launcher criou outro processo e só ele ficou aberto, não há
        # confirmação de que o jogo continue rodando.
        if session.get("had_children") and not any(
            proc.pid != session["process"].pid for proc in active
        ):
            return "unknown"
        if active or main_active:
            return "running"
        return "unknown"

    @staticmethod
    def _close_process_handle(session: dict) -> None:
        process = session["process"]
        if isinstance(process, WindowsGameProcess):
            process.close()

    def finish_all(self) -> list[tuple[str, int]]:
        """Calcula as sessões em andamento ao sair, sem fechar os jogos."""
        ended = [(game_id, self.elapsed(game_id)) for game_id in list(self.sessions)]
        for session in self.sessions.values():
            self._close_process_handle(session)
        self.sessions.clear()
        return ended


# A partir daqui ficam as janelas, os botões e a biblioteca visual.

import os
import sys


# Esse ajuste precisa acontecer antes de criar qualquer janela do Tkinter.
enable_high_dpi()

import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import psutil
    from PIL import Image, ImageDraw, ImageOps, ImageTk, ImageFilter
except ImportError as exc:
    print("Dependência não instalada:", exc)
    print("Execute: python -m pip install -r requirements.txt")
    raise SystemExit(1) from exc


# Cores usadas na interface. Assim fica fácil mudar o tema depois.
BG = "#0E1019"
SIDE = "#11131D"
PANEL = "#1A1D2A"
PANEL_HOVER = "#222638"
INPUT = "#25293A"
BORDER = "#303347"
TEXT = "#F3F3F9"
MUTED = "#A4A7BB"
ACCENT = "#775CF2"
ACCENT_HOVER = "#947EFF"
GREEN = "#53D4AE"
RED = "#F08092"

CATEGORIES = ["Ação", "Aventura", "RPG", "Estratégia", "Corrida", "Simulação", "Indie", "Emulador", "Outros"]


def font(size=11, weight="normal"):
    return ("Segoe UI", size, weight)


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}min"
    if minutes:
        return f"{minutes}min {secs:02d}s"
    return f"{secs}s"


def friendly_date(value: str | None) -> str:
    if not value:
        return "Ainda não jogado"
    try:
        return "Último acesso: " + datetime.fromisoformat(value).strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return "Último acesso não disponível"



def extract_exe_icon(executable: str, size: int = 192):
    """Lê o ícone do .exe usando o próprio Windows, sem bibliotecas extras."""
    if sys.platform != "win32" or not Path(executable).is_file():
        return None

    from ctypes import wintypes

    class BitmapInfoHeader(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BitmapInfo(ctypes.Structure):
        _fields_ = [("bmiHeader", BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 1)]

    class ShellFileInfo(ctypes.Structure):
        _fields_ = [
            ("hIcon", wintypes.HICON), ("iIcon", ctypes.c_int),
            ("dwAttributes", wintypes.DWORD),
            ("szDisplayName", wintypes.WCHAR * 260),
            ("szTypeName", wintypes.WCHAR * 80),
        ]

    handle = None
    dc = bitmap = previous = None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.DestroyIcon.restype = wintypes.BOOL
    try:
        # Primeiro procura a imagem original dentro do executável, que pode
        # ter resolução maior que o ícone mostrado no Explorador de Arquivos.
        extractor = user32.PrivateExtractIconsW
        extractor.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.POINTER(wintypes.HICON),
                              ctypes.POINTER(wintypes.UINT), wintypes.UINT, wintypes.UINT]
        extractor.restype = wintypes.UINT
        icon_out = wintypes.HICON()
        icon_id = wintypes.UINT()
        count = extractor(str(executable), 0, size, size,
                          ctypes.byref(icon_out), ctypes.byref(icon_id), 1, 0)
        if count != 0xFFFFFFFF and count > 0 and icon_out:
            handle = icon_out
        else:
            # Nem todo .exe possui ícone próprio. Nesses casos tentamos o
            # ícone associado ao arquivo pelo Windows.
            shell32.SHGetFileInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                               ctypes.POINTER(ShellFileInfo),
                                               wintypes.UINT, wintypes.UINT]
            shell32.SHGetFileInfoW.restype = ctypes.c_void_p
            info = ShellFileInfo()
            result = shell32.SHGetFileInfoW(str(executable), 0, ctypes.byref(info),
                                            ctypes.sizeof(info), 0x100)
            if not result or not info.hIcon:
                return None
            handle = info.hIcon

        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BitmapInfo),
                                           wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                           wintypes.HANDLE, wintypes.DWORD]
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                      wintypes.HICON, ctypes.c_int, ctypes.c_int,
                                      wintypes.UINT, wintypes.HBRUSH, wintypes.UINT]
        user32.DrawIconEx.restype = wintypes.BOOL

        dc = gdi32.CreateCompatibleDC(None)
        if not dc:
            return None
        bitmap_info = BitmapInfo()
        bitmap_info.bmiHeader.biSize = ctypes.sizeof(BitmapInfoHeader)
        bitmap_info.bmiHeader.biWidth = size
        bitmap_info.bmiHeader.biHeight = -size  # Linhas de cima para baixo
        bitmap_info.bmiHeader.biPlanes = 1
        bitmap_info.bmiHeader.biBitCount = 32
        bitmap_info.bmiHeader.biCompression = 0
        pixels = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(dc, ctypes.byref(bitmap_info), 0,
                                         ctypes.byref(pixels), None, 0)
        if not bitmap or not pixels.value:
            return None
        previous = gdi32.SelectObject(dc, bitmap)
        if not previous:
            return None
        if not user32.DrawIconEx(dc, 0, 0, handle, size, size, 0, None, 0x0003):
            return None
        raw = ctypes.string_at(pixels.value, size * size * 4)
        image = Image.frombytes("RGBA", (size, size), raw, "raw", "BGRA")
        if not image.getchannel("A").getbbox():
            # Alguns ícones antigos não gravam o canal de transparência.
            # Aproveita as cores desenhadas, quando existirem.
            rgb = image.convert("RGB")
            mask = rgb.convert("L").point(lambda n: 255 if n > 0 else 0)
            image.putalpha(mask)
        return image if image.getchannel("A").getbbox() else None
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError):
        return None
    finally:
        if dc and previous:
            gdi32.SelectObject(dc, previous)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if dc:
            gdi32.DeleteDC(dc)
        if handle:
            user32.DestroyIcon(handle)


def icon_cover(icon, width: int, height: int):
    """Prepara um fundo para o ícone, sem esticá-lo como se fosse uma foto."""
    base = Image.new("RGBA", (width, height), "#27233e")
    background = Image.new("RGBA", icon.size, "#27233e")
    background.alpha_composite(icon.convert("RGBA"))
    background = ImageOps.fit(background.convert("RGB"), (width, height),
                              method=Image.Resampling.LANCZOS)
    background = background.filter(ImageFilter.GaussianBlur(max(8, width // 17)))
    base = Image.blend(background.convert("RGBA"), base, 0.77)
    icon_size = max(24, min(int(height * 0.68), int(width * 0.57)))
    foreground = ImageOps.contain(icon.convert("RGBA"), (icon_size, icon_size),
                                  method=Image.Resampling.LANCZOS)
    x = (width - foreground.width) // 2
    y = (height - foreground.height) // 2
    base.alpha_composite(foreground, (x, y))
    return base.convert("RGB")


class GameHubApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("GameHub — Biblioteca de jogos")
        self._scale = ui_scale(self)
        # O Tkinter já ajusta as fontes conforme a tela. Aqui escalamos
        # os demais elementos para não deixar tudo pequeno ou borrado.
        if sys.platform == "win32" or os.environ.get("GAMEHUB_UI_SCALE"):
            self.tk.call("tk", "scaling", (96.0 * self._scale) / 72.0)
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        self._compact = (screen_h / self._scale < 800 or screen_w / self._scale < 1100)
        width = min(self.px(1260), max(640, screen_w - self.px(44)))
        height = min(self.px(780), max(510, screen_h - self.px(80)))
        # Começa em uma janela centralizada. A tela cheia fica a cargo do F11.
        left = max(0, (screen_w - width) // 2)
        top = max(0, (screen_h - height) // 2)
        self.geometry(f"{width}x{height}+{left}+{top}")
        self.minsize(min(self.px(860), width), min(self.px(560), height))
        self._window_before_fullscreen = None
        self.configure(bg=BG)
        # No executável, o PyInstaller deixa o ícone na pasta de recursos.
        # Ao abrir pelo Python, usamos o ícone que está junto do código.
        resource_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        icon_path = resource_dir / "GameHub.ico"
        if icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except tk.TclError:
                pass

        try:
            self.store = GameStore()
        except RuntimeError as exc:
            messagebox.showerror("Erro no GameHub", str(exc), parent=self)
            self.destroy()
            raise SystemExit(1) from exc

        self.tracker = SessionTracker()
        self._closing_games = set()
        self.search_var = tk.StringVar()
        self.category_var = tk.StringVar(value="Todas as categorias")
        self._image_refs = []
        self._card_info = {}
        self._modal = None
        self._cols = 3
        self._last_canvas_width = 0
        self._exe_icons = {}
        self._show_running_only = False

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Dark.TCombobox", fieldbackground=INPUT, background=INPUT,
                        foreground=TEXT, arrowcolor=TEXT, bordercolor=BORDER,
                        lightcolor=INPUT, darkcolor=INPUT, padding=(self.px(8), self.px(7)))
        style.map("Dark.TCombobox", fieldbackground=[("readonly", INPUT)],
                  foreground=[("readonly", TEXT)])
        style.configure("Dark.Vertical.TScrollbar", background=BORDER, troughcolor=BG,
                        arrowcolor=MUTED, bordercolor=BG)
        self.option_add("*TCombobox*Listbox.background", INPUT)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)

        self._build_layout()
        # F11 entra ou sai da tela cheia; Esc retorna à janela anterior.
        self.bind("<F11>", self._toggle_fullscreen)
        self.bind("<Escape>", self._exit_fullscreen)
        self.search_var.trace_add("write", lambda *_: self.render_games())
        self.category_var.trace_add("write", lambda *_: self.render_games())
        psutil.cpu_percent(interval=None)
        self.render_games()
        self._tick()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _enter_fullscreen(self):
        """Esconde as bordas da janela até a pessoa apertar F11 novamente."""
        if bool(self.attributes("-fullscreen")):
            return
        self._window_before_fullscreen = self.geometry()
        try:
            self.attributes("-fullscreen", True)
        except tk.TclError:
            self._window_before_fullscreen = None

    def _toggle_fullscreen(self, _event=None):
        if bool(self.attributes("-fullscreen")):
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _exit_fullscreen(self, _event=None):
        if not bool(self.attributes("-fullscreen")):
            return
        self.attributes("-fullscreen", False)
        # Restauramos as dimensões que estavam em uso antes da tela cheia.
        previous = self._window_before_fullscreen
        self._window_before_fullscreen = None
        if previous:
            self.after_idle(lambda: self.geometry(previous))

    def px(self, value):
        """Ajusta tamanhos e espaçamentos à escala da tela."""
        return max(1, round(value * self._scale)) if value else 0

    def _button(self, parent, text, command, bg=ACCENT, fg=TEXT, hover=ACCENT_HOVER, **kwargs):
        options = dict(text=text, command=command, bg=bg, fg=fg,
                       activebackground=hover, activeforeground=fg,
                       relief="flat", bd=0, highlightthickness=0, cursor="hand2", font=font(10, "bold"),
                       padx=self.px(14), pady=self.px(9))
        options.update(kwargs)
        btn = tk.Button(parent, **options)
        btn.bind("<Enter>", lambda _event: btn.configure(bg=hover) if str(btn["state"]) != "disabled" else None)
        btn.bind("<Leave>", lambda _event: btn.configure(bg=bg) if str(btn["state"]) != "disabled" else None)
        return btn

    def _build_layout(self):
        # Uma barra lateral fixa e o restante livre para a biblioteca.
        sidebar = tk.Frame(self, bg=SIDE, width=self.px(210))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Frame(self, bg=BORDER, width=self.px(1)).pack(side="left", fill="y")

        brand = tk.Frame(sidebar, bg=SIDE)
        brand.pack(fill="x", padx=self.px(18), pady=(self.px(22), self.px(31)))
        icon_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        try:
            with Image.open(icon_dir / "GameHub.ico") as ico:
                ico_img = ico.convert("RGBA").resize((self.px(43), self.px(43)), Image.Resampling.LANCZOS)
            self._brand_photo = ImageTk.PhotoImage(ico_img, master=self)
            tk.Label(brand, image=self._brand_photo, bg=SIDE).pack(side="left", padx=(0, self.px(11)))
        except (OSError, ValueError, tk.TclError):
            tk.Label(brand, text="G", bg=ACCENT, fg="white", width=3,
                     font=font(16, "bold")).pack(side="left", padx=(0, self.px(11)))
        bt = tk.Frame(brand, bg=SIDE)
        bt.pack(side="left")
        tk.Label(bt, text="GameHub", bg=SIDE, fg=TEXT,
                 font=font(19, "bold")).pack(anchor="w")
        tk.Label(bt, text="SUA BIBLIOTECA", bg=SIDE, fg=MUTED,
                 font=font(8, "bold")).pack(anchor="w", pady=(self.px(3), 0))

        tk.Label(sidebar, text="NAVEGAÇÃO", bg=SIDE, fg="#74788E",
                 font=font(9, "bold")).pack(anchor="w", padx=self.px(25), pady=(0, self.px(12)))
        self.nav_all = self._button(sidebar, "▦   Biblioteca", self._reset_filters,
                                    bg="#292342", hover="#382E58", anchor="w")
        self.nav_all.pack(fill="x", padx=self.px(13), pady=(0, self.px(7)))
        self.nav_running = self._button(sidebar, "▶   Em execução", self._show_running,
                                        bg=SIDE, hover=PANEL_HOVER, anchor="w")
        self.nav_running.pack(fill="x", padx=self.px(13), pady=(0, self.px(7)))
        self._button(sidebar, "+   Adicionar jogo", self.open_game_form,
                     bg=SIDE, hover=PANEL_HOVER, anchor="w").pack(fill="x", padx=self.px(13))

        tk.Frame(sidebar, bg=BORDER, height=self.px(1)).pack(fill="x", padx=self.px(21), pady=(self.px(30), self.px(18)))
        tk.Label(sidebar, text="PREFERÊNCIAS", bg=SIDE, fg="#74788E",
                 font=font(9, "bold")).pack(anchor="w", padx=self.px(25), pady=(0, self.px(10)))
        self._button(sidebar, "⛶   Tela cheia  ·  F11", self._toggle_fullscreen,
                     bg=SIDE, hover=PANEL_HOVER, anchor="w").pack(fill="x", padx=self.px(13))

        bottom = tk.Frame(sidebar, bg=SIDE)
        bottom.pack(side="bottom", fill="x", padx=self.px(22), pady=self.px(24))
        self._button(bottom, "✕   Sair do GameHub", self._on_close,
                     bg=SIDE, hover=PANEL_HOVER, anchor="w").pack(fill="x", pady=(0, self.px(13)))
        tk.Frame(bottom, bg=BORDER, height=self.px(1)).pack(fill="x", pady=(0, self.px(16)))
        self.sidebar_status = tk.Label(bottom, text="●  Nenhum jogo aberto", bg=SIDE,
                                       fg=MUTED, font=font(10), anchor="w")
        self.sidebar_status.pack(anchor="w")
        tk.Label(bottom, text="GameHub v2.0  ·  Windows", bg=SIDE, fg="#74788E",
                 font=font(9)).pack(anchor="w", pady=(self.px(12), 0))

        main = tk.Frame(self, bg=BG)
        main.pack(side="left", fill="both", expand=True)

        header = tk.Frame(main, bg=BG)
        header.pack(fill="x", padx=self.px(25), pady=(self.px(16), self.px(10)))
        texts = tk.Frame(header, bg=BG)
        texts.pack(side="left")
        tk.Label(texts, text="BIBLIOTECA", bg=BG, fg=ACCENT_HOVER,
                 font=font(8, "bold")).pack(anchor="w", pady=(0, self.px(3)))
        tk.Label(texts, text="Meus jogos", bg=BG, fg=TEXT,
                 font=font(20, "bold")).pack(anchor="w")
        self._button(header, "+  Adicionar jogo", self.open_game_form,
                     font=font(9, "bold"), padx=self.px(14), pady=self.px(8)).pack(side="right", pady=self.px(5))

        # Indicadores discretos no topo. A grade ocupa o restante da janela.
        stats = tk.Frame(main, bg=PANEL, highlightthickness=1,
                         highlightbackground="#292C3B")
        stats.pack(fill="x", padx=self.px(29), pady=(0, self.px(14)))
        self.stats_labels = {}
        stats_info = [
            ("Jogos", "games", "0"),
            ("Horas jogadas", "time", "0h"),
            ("CPU", "cpu", "—"),
            ("RAM", "ram", "—"),
        ]
        for index, (caption, key, default) in enumerate(stats_info):
            stats.grid_columnconfigure(index, weight=1, uniform="stat")
            tile = tk.Frame(stats, bg=PANEL, padx=self.px(11), pady=self.px(9))
            tile.grid(row=0, column=index, sticky="nsew")
            line = tk.Frame(tile, bg=PANEL)
            line.pack(anchor="center")
            tk.Label(line, text=caption + ":", bg=PANEL, fg=MUTED,
                     font=font(9)).pack(side="left", padx=(0, self.px(6)))
            number = tk.Label(line, text=default, bg=PANEL, fg=TEXT,
                              font=font(11, "bold"))
            number.pack(side="left")
            self.stats_labels[key] = number
            if index < len(stats_info) - 1:
                tk.Frame(stats, bg=BORDER, width=self.px(1)).grid(
                    row=0, column=index, sticky="nse", pady=self.px(8))

        filters = tk.Frame(main, bg=BG)
        filters.pack(fill="x", padx=self.px(29), pady=(0, self.px(9)))
        tk.Label(filters, text="Todos os jogos", bg=BG, fg=TEXT,
                 font=font(15, "bold")).pack(side="left")
        categories = ttk.Combobox(filters, textvariable=self.category_var,
                                  values=["Todas as categorias", *CATEGORIES],
                                  state="readonly", width=18, style="Dark.TCombobox")
        categories.pack(side="right")
        search_frame = tk.Frame(filters, bg=INPUT, highlightbackground=BORDER, highlightthickness=1)
        search_frame.pack(side="right", padx=(0, self.px(12)))
        tk.Label(search_frame, text="⌕", bg=INPUT, fg=MUTED, font=font(17)).pack(side="left", padx=(self.px(11), self.px(2)))
        search = tk.Entry(search_frame, textvariable=self.search_var, bg=INPUT, fg=TEXT,
                          insertbackground=TEXT, relief="flat", font=font(10), bd=0, width=20)
        search.pack(side="left", ipady=self.px(8), padx=(0, self.px(12)))
        # A dica desaparece quando começamos a digitar.
        placeholder = "Buscar jogo..."
        search.insert(0, placeholder)
        search.configure(fg=MUTED)
        def search_focus_in(_event):
            if search.get() == placeholder and search.cget("fg") == MUTED:
                search.delete(0, "end")
                search.configure(fg=TEXT)
        def search_focus_out(_event):
            if not search.get():
                search.insert(0, placeholder)
                search.configure(fg=MUTED)
        search.bind("<FocusIn>", search_focus_in)
        search.bind("<FocusOut>", search_focus_out)

        list_outer = tk.Frame(main, bg=BG)
        list_outer.pack(fill="both", expand=True, padx=(self.px(23), self.px(19)), pady=(0, self.px(11)))
        self.canvas = tk.Canvas(list_outer, bg=BG, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(list_outer, orient="vertical", command=self.canvas.yview,
                                  style="Dark.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.cards_container = tk.Frame(self.canvas, bg=BG)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.cards_container, anchor="nw")
        self.cards_container.bind("<Configure>", self._on_cards_resize)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind_all("<Button-4>", lambda _: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind_all("<Button-5>", lambda _: self.canvas.yview_scroll(1, "units"))

    def _show_running(self):
        self._show_running_only = True
        self.search_var.set("")
        self.category_var.set("Todas as categorias")
        self.render_games()

    def _game_art(self, game):
        # Uma capa escolhida pela pessoa tem prioridade sobre o ícone do .exe.
        cover = game.get("cover") or ""
        if cover and Path(cover).is_file():
            try:
                with Image.open(cover) as source:
                    return source.convert("RGBA"), "custom"
            except (OSError, ValueError):
                pass

        exe = game.get("executable") or ""
        if not exe or sys.platform != "win32":
            return None, None
        try:
            exe_path = Path(exe)
            updated_at = exe_path.stat().st_mtime_ns
        except OSError:
            return None, None
        cache_key = (os.path.normcase(os.path.abspath(exe)), updated_at)
        if cache_key not in self._exe_icons:
            self._exe_icons[cache_key] = extract_exe_icon(exe)
        icon = self._exe_icons[cache_key]
        return (icon, "icon") if icon is not None else (None, None)

    def _reset_filters(self):
        self._show_running_only = False
        self.search_var.set("")
        self.category_var.set("Todas as categorias")
        self.render_games()

    def _on_cards_resize(self, _event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_resize(self, event):
        self.canvas.itemconfigure(self.canvas_window, width=event.width)
        # A quantidade de cartões por linha muda conforme o espaço disponível.
        new_cols = (6 if event.width >= self.px(1550)
                    else 5 if event.width >= self.px(1240)
                    else 4 if event.width >= self.px(890)
                    else 3 if event.width >= self.px(650)
                    else 2 if event.width >= self.px(430) else 1)
        if new_cols != self._cols or abs(event.width - self._last_canvas_width) > self.px(16):
            self._cols = new_cols
            self._last_canvas_width = event.width
            self.render_games()

    def _on_mousewheel(self, event):
        if self._modal and self._modal.winfo_exists():
            return
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _create_cover(self, game, parent):
        view_w = max(self.px(140), (self.canvas.winfo_width() - self.px(18) * self._cols) // max(1, self._cols))
        cover_w, cover_h = view_w, self.px(146 if self._compact else 168)
        art, art_type = self._game_art(game)
        if art_type == "custom":
            picture = ImageOps.fit(art.convert("RGB"), (cover_w, cover_h),
                                   method=Image.Resampling.LANCZOS)
        elif art_type == "icon":
            picture = icon_cover(art, cover_w, cover_h)
        else:
            w, h = cover_w * 2, cover_h * 2
            picture = Image.new("RGB", (w, h), "#30264D")
            draw = ImageDraw.Draw(picture)
            for y in range(h):
                p = y / max(1, h)
                draw.line((0, y, w, y), fill=(int(38 + 23 * p), int(29 + 21 * p), int(72 + 36 * p)))
            cx, cy = w // 2, h // 2
            size = min(w, h) // 5
            draw.rounded_rectangle((cx - size * 2, cy - size, cx + size * 2, cy + size),
                                   radius=size // 2, outline="#B9AAFF", width=max(3, w // 120))
            draw.line((cx - size, cy - size // 2, cx - size, cy + size // 2),
                      fill="#E5DFFF", width=max(3, w // 100))
            draw.line((cx - size * 3 // 2, cy, cx - size // 2, cy),
                      fill="#E5DFFF", width=max(3, w // 100))
            draw.ellipse((cx + size // 2, cy - size // 2, cx + size, cy), fill="#E5DFFF")
            draw.ellipse((cx + size, cy, cx + size * 3 // 2, cy + size // 2), fill="#E5DFFF")
            picture = picture.resize((cover_w, cover_h), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(picture, master=self)
        self._image_refs.append(photo)
        label = tk.Label(parent, image=photo, bd=0, bg=PANEL)
        return label

    def render_games(self):
        if not hasattr(self, "cards_container"):
            return
        for widget in self.cards_container.winfo_children():
            widget.destroy()
        self._image_refs = []
        self._card_info = {}
        for index in range(6):
            self.cards_container.grid_columnconfigure(index, weight=0, uniform="")
        for index in range(self._cols):
            self.cards_container.grid_columnconfigure(index, weight=1, uniform="cards")

        # Busca pelo nome e filtra pela categoria selecionada.
        needle = self.search_var.get().strip().casefold()
        if needle == "buscar jogo...":
            needle = ""
        category = self.category_var.get()
        games = sorted(self.store.games, key=lambda g: g.get("name", "").casefold())
        games = [g for g in games if needle in g.get("name", "").casefold()
                 and (category == "Todas as categorias" or g.get("category") == category)
                 and (not self._show_running_only or g["id"] in self.tracker.sessions)]

        if not games:
            empty = tk.Frame(self.cards_container, bg=PANEL, padx=self.px(30), pady=self.px(48),
                             highlightthickness=1, highlightbackground=BORDER)
            empty.grid(row=0, column=0, columnspan=self._cols, sticky="ew", padx=self.px(5), pady=self.px(12))
            tk.Label(empty, text="▣", bg=PANEL, fg=ACCENT,
                     font=font(36)).pack(pady=(0, self.px(10)))
            if self._show_running_only:
                heading = "Nenhum jogo em execução"
                explanation = "Volte à biblioteca e inicie um jogo para vê-lo aqui."
            elif self.store.games:
                heading = "Nenhum jogo encontrado"
                explanation = "Tente outro nome ou selecione uma categoria diferente."
            else:
                heading = "Sua biblioteca está vazia"
                explanation = "Adicione o executável de um jogo para começar."
            tk.Label(empty, text=heading, bg=PANEL, fg=TEXT,
                     font=font(16, "bold")).pack()
            tk.Label(empty, text=explanation, bg=PANEL, fg=MUTED,
                     font=font(10)).pack(pady=(self.px(7), self.px(15)))
            if not self.store.games:
                self._button(empty, "+  Adicionar primeiro jogo", self.open_game_form).pack()
        for index, game in enumerate(games):
            self._game_card(game, index // self._cols, index % self._cols)
        self.nav_all.configure(bg=SIDE if self._show_running_only else "#292342")
        self.nav_running.configure(bg="#292342" if self._show_running_only else SIDE)
        self._update_stats()

    def _game_card(self, game, row, col):
        card = tk.Frame(self.cards_container, bg=PANEL,
                        highlightthickness=1, highlightbackground="#292C3B")
        card.grid(row=row, column=col, sticky="nsew", padx=self.px(6), pady=self.px(7))

        cover = self._create_cover(game, card)
        cover.pack(fill="x", expand=False)

        body = tk.Frame(card, bg=PANEL)
        body.pack(fill="both", expand=True, padx=self.px(13), pady=(self.px(10), self.px(12)))
        running = game["id"] in self.tracker.sessions
        closing = game["id"] in self._closing_games
        info = tk.Frame(body, bg=PANEL)
        info.pack(fill="x")
        tk.Label(info, text=(game.get("category") or "Outros").upper(), bg=PANEL,
                 fg="#AD9AFF", font=font(8, "bold")).pack(side="left")
        if running:
            tk.Label(info, text="● ATIVO", bg=PANEL, fg=GREEN,
                     font=font(8, "bold")).pack(side="right")
        title = tk.Label(body, text=game.get("name", "Jogo sem nome"),
                         bg=PANEL, fg=TEXT, anchor="w", justify="left",
                         font=font(12, "bold"), wraplength=max(self.px(120),
                                     self.canvas.winfo_width() // max(1, self._cols) - self.px(38)))
        title.pack(anchor="w", fill="x", pady=(self.px(5), self.px(5)))
        time_label = tk.Label(body, bg=PANEL, fg=MUTED,
                              text=self._time_text(game), font=font(9), anchor="w")
        time_label.pack(anchor="w")
        status = tk.Label(body, bg=PANEL, fg=GREEN if running else "#80849C",
                          text=("● Encerrando..." if closing else "● Em execução" if running
                                else friendly_date(game.get("last_played"))),
                          font=font(8), anchor="w")
        status.pack(anchor="w", pady=(self.px(3), self.px(7)))
        actions = tk.Frame(body, bg=PANEL)
        actions.pack(side="bottom", fill="x", pady=(self.px(3), 0))
        if running:
            btn = self._button(actions, "Encerrando..." if closing else "■  Fechar jogo",
                               lambda gid=game["id"]: self.stop_game(gid),
                               bg="#753949", hover="#97495C", font=font(9, "bold"),
                               padx=self.px(12), pady=self.px(8))
            if closing:
                btn.configure(state="disabled", cursor="arrow")
            btn.pack(side="left")
        else:
            self._button(actions, "▶  Jogar", lambda gid=game["id"]: self.launch_game(gid),
                         bg=ACCENT, hover=ACCENT_HOVER, font=font(9, "bold"),
                         padx=self.px(14), pady=self.px(8)).pack(side="left")
        self._button(actions, "Editar", lambda gid=game["id"]: self.open_game_form(gid),
                     bg=INPUT, hover=BORDER, font=font(9), padx=self.px(11),
                     pady=self.px(8)).pack(side="left", padx=(self.px(5), 0))
        self._button(actions, "×", lambda gid=game["id"]: self.delete_game(gid),
                     bg=INPUT, hover="#66323D", font=font(12),
                     padx=self.px(9), pady=self.px(5)).pack(side="right")
        self._card_info[game["id"]] = (time_label, status)

    def _time_text(self, game):
        elapsed = self.tracker.elapsed(game["id"])
        return "Tempo jogado: " + format_duration(int(game.get("seconds_played", 0)) + elapsed)

    def _update_stats(self):
        if not hasattr(self, "stats_labels"):
            return
        self.stats_labels["games"].configure(text=str(len(self.store.games)))
        live = sum(self.tracker.elapsed(gid) for gid in self.tracker.sessions)
        self.stats_labels["time"].configure(text=format_duration(self.store.total_seconds() + live))
        count = len(self.tracker.sessions)
        self.sidebar_status.configure(text=(f"●  {count} jogo(s) em execução" if count else "●  Nenhum jogo aberto"),
                                      fg=GREEN if count else MUTED)

    def _tick(self):
        # A cada dois segundos atualizamos o uso do PC e o tempo das sessões.
        try:
            ended = self.tracker.poll()
            for game_id, seconds in ended:
                self.store.record_session(game_id, seconds)
                self._closing_games.discard(game_id)
            if ended:
                self.render_games()

            cpu = psutil.cpu_percent(interval=None)
            memory = psutil.virtual_memory().percent
            self.stats_labels["cpu"].configure(text=f"{cpu:.0f}%")
            self.stats_labels["ram"].configure(text=f"{memory:.0f}%")
            for game_id, (label, _status) in list(self._card_info.items()):
                try:
                    label.configure(text=self._time_text(self.store.get(game_id)))
                except (KeyError, tk.TclError):
                    pass
            self._update_stats()
        except (OSError, RuntimeError, ValueError) as exc:
            messagebox.showerror("Erro ao atualizar o GameHub", str(exc), parent=self)
        self.after(2000, self._tick)

    def launch_game(self, game_id):
        try:
            game = self.store.get(game_id)
            self.tracker.start(game_id, game["executable"])
        except (OSError, RuntimeError, KeyError) as exc:
            messagebox.showerror("Não foi possível iniciar o jogo", str(exc), parent=self)
            return
        self.render_games()
        # Alguns jogos só iniciam o processo principal depois de alguns instantes.
        # Fazemos mais duas verificações para tentar acompanhá-lo.
        self.after(400, lambda gid=game_id: self.tracker.refresh_game(gid))
        self.after(1200, lambda gid=game_id: self.tracker.refresh_game(gid))

    def stop_game(self, game_id):
        """Confirma antes de fechar, para não perder uma partida sem querer."""
        if game_id in self._closing_games:
            return
        try:
            game = self.store.get(game_id)
        except KeyError:
            return
        if not messagebox.askyesno(
            "Fechar jogo",
            f"Fechar '{game['name']}' agora?\n\n"
            "Se houver progresso não salvo, ele poderá ser perdido.",
            icon="warning", parent=self,
        ):
            return
        try:
            seconds = self.tracker.stop(game_id)
            if seconds is None:
                # Alguns jogos demoram para terminar. Acompanhamos sem exibir
                # mensagens enquanto o encerramento está acontecendo.
                self._closing_games.add(game_id)
                self.render_games()
                self.after(900, lambda gid=game_id: self._check_game_closed(gid, 0))
            else:
                self.store.record_session(game_id, seconds)
                self.render_games()
        except (RuntimeError, OSError, psutil.Error):
            # Não conclui que falhou antes de conferir se o processo terminou.
            if game_id in self.tracker.sessions:
                self._closing_games.add(game_id)
                self.render_games()
                self.after(900, lambda gid=game_id: self._check_game_closed(gid, 0))

    def _check_game_closed(self, game_id, attempts):
        if game_id not in self._closing_games:
            return
        try:
            ended = self.tracker.poll()
            for ended_id, seconds in ended:
                self.store.record_session(ended_id, seconds)
                self._closing_games.discard(ended_id)
            if game_id not in self.tracker.sessions:
                # O fechamento foi confirmado: volta ao botão Jogar sem aviso.
                self._closing_games.discard(game_id)
                self.render_games()
                return
        except (OSError, RuntimeError, psutil.Error):
            # Uma falha de leitura isolada não significa que o jogo ficou aberto.
            # Tentamos novamente antes de decidir se é necessário avisar.
            pass

        if attempts < 13:
            self.after(900, lambda gid=game_id, n=attempts + 1: self._check_game_closed(gid, n))
            return

        status = self.tracker.closing_status(game_id)
        self._closing_games.discard(game_id)
        self.render_games()
        if status == "running":
            messagebox.showwarning(
                "O jogo continua aberto",
                "O jogo ainda está em execução. Tente fechá-lo pela própria janela.",
                parent=self,
            )
        # Se não foi possível confirmar, não exibimos um erro que pode ser falso.
        # O acompanhamento normal continua até o processo realmente terminar.

    def delete_game(self, game_id):
        if game_id in self.tracker.sessions:
            messagebox.showinfo("Jogo em execução", "Feche o jogo antes de removê-lo.", parent=self)
            return
        game = self.store.get(game_id)
        # Tirar da biblioteca não apaga nenhum arquivo da instalação do jogo.
        if not messagebox.askyesno("Remover jogo", f"Remover '{game['name']}' da biblioteca?\n\n"
                                   "Isso não desinstala nem exclui o jogo do computador.", parent=self):
            return
        try:
            self.store.remove(game_id)
            self.render_games()
        except OSError as exc:
            messagebox.showerror("Erro ao remover", str(exc), parent=self)

    def open_game_form(self, game_id=None):
        if self._modal is not None and self._modal.winfo_exists():
            self._modal.lift()
            return
        if game_id in self.tracker.sessions:
            messagebox.showinfo("Jogo em execução", "Feche o jogo antes de editar o cadastro.", parent=self)
            return
        game = self.store.get(game_id) if game_id else None
        modal = tk.Toplevel(self)
        self._modal = modal
        modal.title("Editar jogo" if game else "Adicionar jogo")
        modal.configure(bg=BG)
        form_width = min(self.px(630), self.winfo_screenwidth() - self.px(40))
        form_height = min(self.px(520), self.winfo_screenheight() - self.px(70))
        modal.geometry(f"{form_width}x{form_height}")
        modal.resizable(False, False)
        modal.transient(self)
        modal.grab_set()
        modal.focus_force()
        modal.protocol("WM_DELETE_WINDOW", modal.destroy)
        self.update_idletasks()
        x = self.winfo_rootx() + max(self.px(20), (self.winfo_width() - form_width) // 2)
        y = self.winfo_rooty() + max(self.px(20), (self.winfo_height() - form_height) // 2)
        modal.geometry(f"+{x}+{y}")

        # Em telas menores, o formulário pode ser rolado em vez de cortar
        # os campos que ficam mais para baixo.
        form_canvas = tk.Canvas(modal, bg=BG, highlightthickness=0, bd=0)
        form_canvas.pack(side="left", fill="both", expand=True)
        form_scroll = ttk.Scrollbar(modal, orient="vertical", command=form_canvas.yview,
                                    style="Dark.Vertical.TScrollbar")
        form_scroll.pack(side="right", fill="y")
        form_canvas.configure(yscrollcommand=form_scroll.set)
        inner = tk.Frame(form_canvas, bg=BG, padx=self.px(34), pady=self.px(28))
        form_window = form_canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: form_canvas.configure(scrollregion=form_canvas.bbox("all")))
        form_canvas.bind("<Configure>", lambda e: form_canvas.itemconfigure(form_window, width=e.width))
        modal.bind("<MouseWheel>",
                   lambda event: form_canvas.yview_scroll(-int(event.delta / 120), "units"))
        tk.Label(inner, text="Editar jogo" if game else "Novo jogo", bg=BG, fg=TEXT,
                 font=font(22, "bold")).pack(anchor="w")
        tk.Label(inner, text="Cadastre o executável e personalize sua biblioteca.",
                 bg=BG, fg=MUTED, font=font(10)).pack(anchor="w", pady=(self.px(3), self.px(20)))

        name_var = tk.StringVar(value=game.get("name", "") if game else "")
        exe_var = tk.StringVar(value=game.get("executable", "") if game else "")
        cover_var = tk.StringVar(value=game.get("cover", "") if game else "")
        genre_var = tk.StringVar(value=game.get("category", "Outros") if game else "Outros")

        def label(text):
            tk.Label(inner, text=text, bg=BG, fg=TEXT, font=font(10, "bold")).pack(anchor="w", pady=(self.px(7), self.px(5)))

        def entry(parent, variable):
            return tk.Entry(parent, textvariable=variable, bg=INPUT, fg=TEXT,
                            insertbackground=TEXT, relief="flat", bd=0,
                            highlightbackground=BORDER, highlightthickness=1,
                            font=font(11))

        label("Nome do jogo")
        entry(inner, name_var).pack(fill="x", ipady=self.px(8))
        label("Executável do jogo (.exe)")
        row_exe = tk.Frame(inner, bg=BG)
        row_exe.pack(fill="x")
        entry(row_exe, exe_var).pack(side="left", fill="x", expand=True, ipady=self.px(8))

        def browse_exe():
            path = filedialog.askopenfilename(parent=modal, title="Escolha o executável do jogo",
                                              filetypes=[("Executáveis Windows", "*.exe"), ("Todos os arquivos", "*.*")])
            if path:
                exe_var.set(path)
                if not name_var.get().strip():
                    name_var.set(Path(path).stem.replace("_", " ").title())

        self._button(row_exe, "Procurar", browse_exe, bg=INPUT, hover=BORDER).pack(side="left", padx=(self.px(8), 0))

        label("Categoria")
        ttk.Combobox(inner, textvariable=genre_var, values=CATEGORIES,
                     state="readonly", style="Dark.TCombobox").pack(fill="x")
        label("Capa personalizada (opcional: PNG, JPG ou WEBP)")
        row_cover = tk.Frame(inner, bg=BG)
        row_cover.pack(fill="x")
        entry(row_cover, cover_var).pack(side="left", fill="x", expand=True, ipady=self.px(8))

        def browse_cover():
            path = filedialog.askopenfilename(parent=modal, title="Escolha uma imagem de capa",
                                              filetypes=[("Imagens", "*.png *.jpg *.jpeg *.webp"), ("Todos", "*.*")])
            if path:
                cover_var.set(path)

        self._button(row_cover, "Procurar", browse_cover, bg=INPUT, hover=BORDER).pack(side="left", padx=(self.px(8), 0))
        self._button(row_cover, "Limpar", lambda: cover_var.set(""),
                     bg=INPUT, hover=BORDER).pack(side="left", padx=(self.px(7), 0))
        tk.Label(inner, text="Sem capa personalizada, usamos o ícone do executável automaticamente.",
                 bg=BG, fg=MUTED, font=font(9), wraplength=form_width - self.px(74),
                 justify="left").pack(anchor="w", pady=(self.px(8), 0))

        actions = tk.Frame(inner, bg=BG)
        actions.pack(side="bottom", fill="x", pady=(self.px(22), 0))

        def save():
            name = name_var.get().strip()
            exe = exe_var.get().strip().strip('"')
            cover = cover_var.get().strip().strip('"')
            category = genre_var.get()
            if not name:
                messagebox.showwarning("Preencha o nome", "Informe o nome do jogo.", parent=modal)
                return
            if not exe or not Path(exe).is_file() or Path(exe).suffix.lower() != ".exe":
                messagebox.showwarning("Executável inválido", "Selecione um arquivo .exe existente.", parent=modal)
                return
            if cover and (not Path(cover).is_file() or Path(cover).suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp")):
                messagebox.showwarning("Capa inválida", "Selecione uma imagem PNG, JPG ou WEBP existente.", parent=modal)
                return
            # Evita cadastrar duas vezes o mesmo executável.
            normalized = os.path.normcase(os.path.abspath(exe))
            for other in self.store.games:
                if other["id"] != game_id and os.path.normcase(os.path.abspath(other["executable"])) == normalized:
                    messagebox.showwarning("Jogo duplicado", "Este executável já está cadastrado.", parent=modal)
                    return
            try:
                if game_id:
                    self.store.update(game_id, name, exe, category, cover)
                else:
                    self.store.add(name, exe, category, cover)
            except OSError as exc:
                messagebox.showerror("Erro ao salvar", str(exc), parent=modal)
                return
            modal.destroy()
            self.render_games()

        self._button(actions, "Salvar jogo", save).pack(side="right")
        self._button(actions, "Cancelar", modal.destroy, bg=INPUT,
                     hover=BORDER).pack(side="right", padx=(0, self.px(10)))

    def _on_close(self):
        # O Hub fecha, mas os jogos continuam abertos. Antes disso,
        # guardamos o tempo que foi contado até aqui.
        try:
            for game_id, seconds in self.tracker.finish_all():
                self.store.record_session(game_id, seconds)
        except OSError as exc:
            messagebox.showerror("Erro ao salvar sessões", str(exc), parent=self)
        self.destroy()

# Como o executável não abre CMD, usamos uma mensagem do Windows se algo falhar.

def show_error(message: str) -> None:
    """Mostra o erro em uma janela quando não existe console aberto."""
    import ctypes
    import sys

    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, "GameHub - Erro ao iniciar", 0x10)
            return
        except Exception:
            pass
    print(message)


def main():
    GameHubApp().mainloop()


if __name__ == "__main__":
    try:
        main()
    except (Exception, SystemExit):
        # Guarda o erro num arquivo para facilitar quando o programa não abrir.
        error = traceback.format_exc()
        try:
            if os.name == "nt":
                folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GameHub"
            else:
                folder = Path.home() / ".gamehub"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "erros.log").write_text(error, encoding="utf-8")
        except OSError:
            pass
        show_error("O GameHub nao conseguiu abrir.\n\n"
                   "Execute criar_exe.bat para gerar o programa completo.\n\n"
                   + error[-1400:])
