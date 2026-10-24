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
        return ended

    def stop(self, game_id: str) -> int | None:
        """Tenta fechar os processos ligados ao jogo que o Hub abriu.

        Se algum deles continuar rodando, a interface avisa o usuário.
        """
        session = self.sessions.get(game_id)
        if session is None:
            raise RuntimeError("O jogo já não está em execução pelo GameHub.")
        processes = self._live_owned(session)
        failures = []
        # Fechamos os processos filhos antes do principal.
        ordered = sorted(processes, key=lambda proc: proc.pid == session["process"].pid)
        for proc in ordered:
            try:
                proc.terminate()
            except psutil.NoSuchProcess:
                pass
            except (psutil.AccessDenied, OSError) as exc:
                failures.append(f"PID {proc.pid}: {exc}")
        if failures:
            raise RuntimeError("Não foi possível encerrar todos os processos:\n" + "\n".join(failures))
        # Dá um pequeno tempo para o encerramento, sem travar a janela.
        if processes:
            psutil.wait_procs(processes, timeout=0.8)
        if self._live_owned(session) or session["process"].poll() is None:
            return None
        seconds = self.elapsed(game_id)
        del self.sessions[game_id]
        return seconds

    def finish_all(self) -> list[tuple[str, int]]:
        """Calcula as sessões em andamento ao sair, sem fechar os jogos."""
        ended = [(game_id, self.elapsed(game_id)) for game_id in list(self.sessions)]
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
    from PIL import Image, ImageDraw, ImageOps, ImageTk
except ImportError as exc:
    print("Dependência não instalada:", exc)
    print("Execute: python -m pip install -r requirements.txt")
    raise SystemExit(1) from exc


# Cores usadas na interface. Assim fica fácil mudar o tema depois.
BG = "#0C101A"
SIDE = "#101624"
PANEL = "#171E2E"
PANEL_HOVER = "#1B2538"
INPUT = "#202B3E"
BORDER = "#2A3548"
TEXT = "#F2F4FC"
MUTED = "#98A5BE"
ACCENT = "#7967F2"
ACCENT_HOVER = "#9384FF"
GREEN = "#55D6AC"
RED = "#F27C91"

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


class GameHubApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("GameHub — Minha biblioteca")
        self._scale = ui_scale(self)
        # O Tkinter já ajusta as fontes conforme a tela. Aqui escalamos
        # os demais elementos para não deixar tudo pequeno ou borrado.
        if sys.platform == "win32" or os.environ.get("GAMEHUB_UI_SCALE"):
            self.tk.call("tk", "scaling", (96.0 * self._scale) / 72.0)
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        self._compact = (screen_h / self._scale < 800 or screen_w / self._scale < 1100)
        width = min(self.px(1260), max(640, screen_w - self.px(44)))
        height = min(self.px(780), max(510, screen_h - self.px(80)))
        self.geometry(f"{width}x{height}")
        self.minsize(min(self.px(870), width), min(self.px(580), height))
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
        self.search_var = tk.StringVar()
        self.category_var = tk.StringVar(value="Todas as categorias")
        self._image_refs = []
        self._card_info = {}
        self._modal = None
        self._cols = 2
        self._cpu_boxes = {}

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
        # O Hub já abre em tela cheia. F11 alterna o modo e Esc volta
        # para uma janela maximizada.
        self.bind("<F11>", self._toggle_fullscreen)
        self.bind("<Escape>", self._exit_fullscreen)
        self.after(100, self._enter_fullscreen)
        self.search_var.trace_add("write", lambda *_: self.render_games())
        self.category_var.trace_add("write", lambda *_: self.render_games())
        psutil.cpu_percent(interval=None)
        self.render_games()
        self._tick()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _enter_fullscreen(self):
        """Abre o GameHub ocupando a tela inteira."""
        try:
            self.attributes("-fullscreen", True)
        except tk.TclError:
            # Se a tela cheia falhar, pelo menos mantemos a janela maximizada.
            self._maximize()

    def _maximize(self):
        """Volta para a janela maximizada ao sair da tela cheia."""
        try:
            if sys.platform == "win32":
                self.state("zoomed")
            else:
                self.attributes("-zoomed", True)
        except tk.TclError:
            w, h = self.winfo_screenwidth(), self.winfo_screenheight()
            self.geometry(f"{w}x{h}+0+0")

    def _toggle_fullscreen(self, _event=None):
        if bool(self.attributes("-fullscreen")):
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _exit_fullscreen(self, _event=None):
        if bool(self.attributes("-fullscreen")):
            self.attributes("-fullscreen", False)
            self.after_idle(self._maximize)

    def px(self, value):
        """Ajusta tamanhos e espaçamentos à escala da tela."""
        return max(1, round(value * self._scale)) if value else 0

    def _button(self, parent, text, command, bg=ACCENT, fg=TEXT, hover=ACCENT_HOVER, **kwargs):
        options = dict(text=text, command=command, bg=bg, fg=fg,
                       activebackground=hover, activeforeground=fg,
                       relief="flat", bd=0, cursor="hand2", font=font(10, "bold"),
                       padx=self.px(14), pady=self.px(9))
        options.update(kwargs)
        btn = tk.Button(parent, **options)
        btn.bind("<Enter>", lambda _event: btn.configure(bg=hover) if str(btn["state"]) != "disabled" else None)
        btn.bind("<Leave>", lambda _event: btn.configure(bg=bg) if str(btn["state"]) != "disabled" else None)
        return btn

    def _build_layout(self):
        sidebar = tk.Frame(self, bg=SIDE, width=self.px(228))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = tk.Frame(sidebar, bg=SIDE)
        brand.pack(fill="x", padx=self.px(23), pady=(self.px(31), self.px(35)))
        tk.Label(brand, text="GH", bg=ACCENT, fg="white", width=3, height=2,
                 font=font(11, "bold")).pack(side="left", padx=(0, self.px(12)))
        brand_text = tk.Frame(brand, bg=SIDE)
        brand_text.pack(side="left")
        tk.Label(brand_text, text="GameHub", bg=SIDE, fg=TEXT,
                 font=font(19, "bold")).pack(anchor="w")
        tk.Label(brand_text, text="SUA CENTRAL GAMER", bg=SIDE, fg=MUTED,
                 font=font(8, "bold")).pack(anchor="w", pady=(self.px(2), 0))

        tk.Label(sidebar, text="BIBLIOTECA", bg=SIDE, fg=MUTED,
                 font=font(9, "bold")).pack(anchor="w", padx=self.px(25), pady=(0, self.px(12)))
        self._button(sidebar, "▣   Meus jogos", lambda: self._reset_filters(),
                     bg="#2A2B51", hover="#373968", anchor="w").pack(fill="x", padx=self.px(15))
        self._button(sidebar, "+   Adicionar jogo", self.open_game_form,
                     bg=SIDE, hover=PANEL_HOVER, anchor="w").pack(fill="x", padx=self.px(15), pady=(self.px(8), 0))
        self._button(sidebar, "⛶   Alternar tela (F11)", self._toggle_fullscreen,
                     bg=SIDE, hover=PANEL_HOVER, anchor="w").pack(fill="x", padx=self.px(15), pady=(self.px(8), 0))

        bottom = tk.Frame(sidebar, bg=SIDE)
        bottom.pack(side="bottom", fill="x", padx=self.px(22), pady=self.px(25))
        self._button(bottom, "✕   Sair do GameHub", self._on_close,
                     bg=SIDE, hover=PANEL_HOVER, anchor="w").pack(fill="x", pady=(0, self.px(14)))
        tk.Frame(bottom, bg=BORDER, height=self.px(1)).pack(fill="x", pady=(0, self.px(17)))
        self.sidebar_status = tk.Label(bottom, text="●  Nenhum jogo aberto", bg=SIDE,
                                       fg=MUTED, font=font(10))
        self.sidebar_status.pack(anchor="w")
        tk.Label(bottom, text="GameHub v1.4  ·  Windows", bg=SIDE, fg="#6C7892",
                 font=font(9)).pack(anchor="w", pady=(self.px(11), 0))

        main = tk.Frame(self, bg=BG)
        main.pack(side="right", fill="both", expand=True)

        header = tk.Frame(main, bg=BG)
        header.pack(fill="x", padx=self.px(32), pady=(self.px(12 if self._compact else 27), self.px(10 if self._compact else 22)))
        texts = tk.Frame(header, bg=BG)
        texts.pack(side="left")
        tk.Label(texts, text="Sua biblioteca", bg=BG, fg=TEXT,
                 font=font(27, "bold")).pack(anchor="w")
        if not self._compact:
            tk.Label(texts, text="Todos os seus jogos em um só lugar.", bg=BG,
                     fg=MUTED, font=font(11)).pack(anchor="w", pady=(self.px(4), 0))
        self._button(header, "+  Adicionar jogo", self.open_game_form).pack(side="right", pady=self.px(10))

        stats = tk.Frame(main, bg=BG)
        stats.pack(fill="x", padx=self.px(27), pady=(0, self.px(12 if self._compact else 25)))
        for i in range(4):
            stats.grid_columnconfigure(i, weight=1, uniform="stat")
        self.stats_labels = {}
        stats_info = [
            ("Jogos cadastrados", "games", "0"),
            ("Tempo total", "time", "0h"),
            ("CPU em uso", "cpu", "—"),
            ("Memória RAM", "ram", "—"),
        ]
        for index, (caption, key, default) in enumerate(stats_info):
            tile = tk.Frame(stats, bg=PANEL, padx=self.px(9 if self._compact else 17),
                            pady=self.px(8 if self._compact else 17), highlightthickness=1,
                            highlightbackground=BORDER)
            tile.grid(row=0, column=index, sticky="nsew", padx=self.px(5))
            caption_text = ("JOGOS", "TEMPO", "CPU", "RAM")[index] if self._compact else caption.upper()
            tk.Label(tile, text=caption_text, bg=PANEL, fg=MUTED,
                     font=font(9 if not self._compact else 8, "bold")).pack(anchor="w")
            number = tk.Label(tile, text=default, bg=PANEL, fg=TEXT,
                              font=font(18 if self._compact else 24, "bold"))
            number.pack(anchor="w", pady=(self.px(4 if self._compact else 9), self.px(2)))
            self.stats_labels[key] = number
            if key in ("cpu", "ram"):
                meter = tk.Canvas(tile, width=self.px(110), height=self.px(6), bg=PANEL,
                                  highlightthickness=0, bd=0)
                meter.pack(fill="x", pady=(self.px(5 if self._compact else 10), 0))
                meter.bind("<Configure>", lambda event, k=key: self._draw_meter(k))
                self._cpu_boxes[key] = (meter, 0.0)
            elif not self._compact:
                tk.Label(tile, text="Na sua biblioteca", bg=PANEL, fg="#7785A0",
                         font=font(9)).pack(anchor="w", pady=(self.px(8), 0))

        filters = tk.Frame(main, bg=BG)
        filters.pack(fill="x", padx=self.px(33), pady=(0, self.px(8 if self._compact else 14)))
        tk.Label(filters, text="MEUS JOGOS", bg=BG, fg=TEXT,
                 font=font(12, "bold")).pack(side="left")
        categories = ttk.Combobox(filters, textvariable=self.category_var,
                                  values=["Todas as categorias", *CATEGORIES],
                                  state="readonly", width=19, style="Dark.TCombobox")
        categories.pack(side="right")
        search = tk.Entry(filters, textvariable=self.search_var, bg=INPUT, fg=TEXT,
                          insertbackground=TEXT, relief="flat", font=font(11), bd=0,
                          highlightbackground=BORDER, highlightthickness=1)
        search.pack(side="right", padx=(0, self.px(12)), ipady=self.px(9), ipadx=self.px(11))
        search.insert(0, "")
        tk.Label(filters, text="Buscar:", fg=MUTED, bg=BG, font=font(10)).pack(side="right", padx=(0, self.px(9)))

        list_outer = tk.Frame(main, bg=BG)
        list_outer.pack(fill="both", expand=True, padx=(self.px(30), self.px(22)), pady=(0, self.px(17)))
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

    def _reset_filters(self):
        self.search_var.set("")
        self.category_var.set("Todas as categorias")

    def _on_cards_resize(self, _event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_resize(self, event):
        self.canvas.itemconfigure(self.canvas_window, width=event.width)
        # A quantidade de cartões por linha muda conforme o espaço disponível.
        new_cols = (3 if event.width >= self.px(1020)
                    else 2 if event.width >= self.px(720) else 1)
        if new_cols != self._cols:
            self._cols = new_cols
            self.render_games()

    def _on_mousewheel(self, event):
        if self._modal and self._modal.winfo_exists():
            return
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _draw_meter(self, key):
        if key not in self._cpu_boxes:
            return
        meter, value = self._cpu_boxes[key]
        meter.delete("all")
        width = max(1, meter.winfo_width())
        meter.create_rectangle(0, 0, width, self.px(6), fill=INPUT, outline="")
        meter.create_rectangle(0, 0, int(width * min(100, value) / 100), self.px(6),
                               fill=GREEN if value < 80 else RED, outline="")

    def _create_cover(self, game, parent):
        cover_path = game.get("cover") or ""
        cover_w, cover_h = self.px(120), self.px(164)
        try:
            if not cover_path or not Path(cover_path).is_file():
                raise FileNotFoundError
            with Image.open(cover_path) as source:
                # Redimensiona a capa já no tamanho certo para ela não ficar
                # borrada quando o Windows usa escala maior que 100%.
                image = ImageOps.fit(source.convert("RGB"), (cover_w, cover_h),
                                     method=Image.Resampling.LANCZOS)
        except (OSError, ValueError):
            # Se não tiver capa, mostramos um desenho simples no lugar.
            # Ele é feito maior e reduzido depois para não ficar serrilhado.
            w, h = cover_w * 2, cover_h * 2
            image = Image.new("RGB", (w, h), "#29264E")
            draw = ImageDraw.Draw(image)
            for row in range(h):
                p = row / h
                draw.line([(0, row), (w, row)],
                          fill=(int(44 + 30 * p), int(34 + 22 * p), int(88 + 40 * p)))
            sx, sy = w / 120, h / 164
            def pt(x, y):
                return (round(x * sx), round(y * sy))
            draw.rounded_rectangle((*pt(32, 59), *pt(88, 103)),
                                   radius=round(18 * sx), outline="#BAB0FF", width=max(2, round(3 * sx)))
            draw.ellipse((*pt(48, 74), *pt(56, 82)), fill="#E3DDFF")
            draw.ellipse((*pt(73, 73), *pt(79, 79)), fill="#E3DDFF")
            draw.ellipse((*pt(78, 80), *pt(84, 86)), fill="#E3DDFF")
            draw.line([pt(52, 75), pt(52, 90)], fill="#E3DDFF", width=max(2, round(3 * sx)))
            draw.line([pt(45, 82), pt(59, 82)], fill="#E3DDFF", width=max(2, round(3 * sx)))
            image = image.resize((cover_w, cover_h), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image, master=self)
        # Sem guardar a referência, o Tkinter pode deixar a imagem sumir.
        self._image_refs.append(photo)
        return tk.Label(parent, image=photo, bd=0, bg=PANEL)

    def render_games(self):
        if not hasattr(self, "cards_container"):
            return
        for widget in self.cards_container.winfo_children():
            widget.destroy()
        self._image_refs = []
        self._card_info = {}
        for index in range(3):
            self.cards_container.grid_columnconfigure(index, weight=0, uniform="")
        for index in range(self._cols):
            self.cards_container.grid_columnconfigure(index, weight=1, uniform="cards")

        # Busca pelo nome e filtra pela categoria selecionada.
        needle = self.search_var.get().strip().casefold()
        category = self.category_var.get()
        games = sorted(self.store.games, key=lambda g: g.get("name", "").casefold())
        games = [g for g in games if needle in g.get("name", "").casefold()
                 and (category == "Todas as categorias" or g.get("category") == category)]

        if not games:
            empty = tk.Frame(self.cards_container, bg=PANEL, padx=self.px(30), pady=self.px(48),
                             highlightthickness=1, highlightbackground=BORDER)
            empty.grid(row=0, column=0, columnspan=self._cols, sticky="ew", padx=self.px(5), pady=self.px(12))
            tk.Label(empty, text="▣", bg=PANEL, fg=ACCENT,
                     font=font(36)).pack(pady=(0, self.px(10)))
            if self.store.games:
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
        self._update_stats()

    def _game_card(self, game, row, col):
        card = tk.Frame(self.cards_container, bg=PANEL, height=self.px(204),
                        highlightthickness=1, highlightbackground=BORDER)
        card.grid(row=row, column=col, sticky="nsew", padx=self.px(6), pady=self.px(7))
        card.grid_propagate(False)
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=self.px(14), pady=self.px(14))

        cover = self._create_cover(game, inner)
        cover.pack(side="left", fill="y", padx=(0, self.px(15)))
        body = tk.Frame(inner, bg=PANEL)
        body.pack(side="left", fill="both", expand=True)

        top = tk.Frame(body, bg=PANEL)
        top.pack(fill="x")
        category = game.get("category") or "Outros"
        tk.Label(top, text="  " + category.upper() + "  ", bg="#292E50", fg="#C9C2FF",
                 font=font(8, "bold"), pady=self.px(4)).pack(side="left")
        title = tk.Label(body, text=game.get("name", "Jogo sem nome"),
                         bg=PANEL, fg=TEXT, anchor="w", justify="left",
                         font=font(14, "bold"), wraplength=self.px(225))
        title.pack(anchor="w", fill="x", pady=(self.px(10), self.px(5)))

        running = game["id"] in self.tracker.sessions
        time_label = tk.Label(body, bg=PANEL, fg=GREEN if running else MUTED,
                              text=self._time_text(game), font=font(10), anchor="w")
        time_label.pack(anchor="w")
        status = tk.Label(body, bg=PANEL, fg=GREEN if running else "#76849C",
                          text="● Em execução" if running else friendly_date(game.get("last_played")),
                          font=font(9), anchor="w")
        status.pack(anchor="w", pady=(self.px(3), 0))

        actions = tk.Frame(body, bg=PANEL)
        actions.pack(side="bottom", fill="x", pady=(self.px(9), 0))
        # O botão muda enquanto o jogo estiver em execução.
        if running:
            self._button(actions, "■  Fechar jogo",
                         lambda game_id=game["id"]: self.stop_game(game_id),
                         bg="#7D3445", hover="#A2465B",
                         font=font(9, "bold")).pack(side="left")
        else:
            self._button(actions, "▶  Jogar",
                         lambda game_id=game["id"]: self.launch_game(game_id),
                         bg=ACCENT, hover=ACCENT_HOVER).pack(side="left")
        self._button(actions, "Editar", lambda game_id=game["id"]: self.open_game_form(game_id),
                     bg=INPUT, hover=BORDER, font=font(9)).pack(side="left", padx=(self.px(7), 0))
        self._button(actions, "×", lambda game_id=game["id"]: self.delete_game(game_id),
                     bg=INPUT, hover="#66323D", font=font(13)).pack(side="right")
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
            if ended:
                self.render_games()

            cpu = psutil.cpu_percent(interval=None)
            memory = psutil.virtual_memory().percent
            self.stats_labels["cpu"].configure(text=f"{cpu:.0f}%")
            self.stats_labels["ram"].configure(text=f"{memory:.0f}%")
            for key, value in (("cpu", cpu), ("ram", memory)):
                meter, _ = self._cpu_boxes[key]
                self._cpu_boxes[key] = (meter, value)
                self._draw_meter(key)
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
        """Confirma com o usuário antes de forçar o fechamento do jogo."""
        try:
            game = self.store.get(game_id)
        except KeyError:
            return
        if not messagebox.askyesno(
            "Fechar jogo",
            f"Deseja realmente fechar '{game['name']}'?\n\n"
            "O jogo pode ser encerrado imediatamente, sem salvar seu progresso.\n"
            "Salve sua partida antes de continuar.",
            icon="warning", parent=self,
        ):
            return
        # O encerramento pode fazer o jogador perder uma partida não salva.
        try:
            seconds = self.tracker.stop(game_id)
            if seconds is None:
                messagebox.showinfo(
                    "Encerramento em andamento",
                    "O GameHub solicitou o encerramento, mas ainda há um processo ativo. "
                    "Confira se o jogo foi fechado.", parent=self,
                )
            else:
                self.store.record_session(game_id, seconds)
            self.render_games()
        except (RuntimeError, OSError, psutil.Error) as exc:
            messagebox.showerror(
                "Não foi possível fechar o jogo",
                f"{exc}\n\nO GameHub só encerra processos iniciados por ele e seus filhos. "
                "Jogos abertos por launchers externos podem precisar ser fechados manualmente.",
                parent=self,
            )

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
        label("Capa (opcional: PNG, JPG ou WEBP)")
        row_cover = tk.Frame(inner, bg=BG)
        row_cover.pack(fill="x")
        entry(row_cover, cover_var).pack(side="left", fill="x", expand=True, ipady=self.px(8))

        def browse_cover():
            path = filedialog.askopenfilename(parent=modal, title="Escolha uma imagem de capa",
                                              filetypes=[("Imagens", "*.png *.jpg *.jpeg *.webp"), ("Todos", "*.*")])
            if path:
                cover_var.set(path)

        self._button(row_cover, "Procurar", browse_cover, bg=INPUT, hover=BORDER).pack(side="left", padx=(self.px(8), 0))

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
