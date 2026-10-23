"""Private token setup: explicit paste, Russian keyboard support, inline errors."""
from concurrent.futures import ThreadPoolExecutor

from photo_auditor.credentials import BOT_USERNAME, SetupError, normalize_token, store_verified_token, verify_token


def control_shortcut(event, entry):
    # Tk keysyms change with the Russian layout; physical Ctrl+V/C/A do not.
    if event.keycode == 86:
        entry.event_generate("<<Paste>>")
        return "break"
    if event.keycode == 65:
        entry.selection_range(0, "end")
        return "break"
    if event.keycode == 67:
        entry.event_generate("<<Copy>>")
        return "break"


def run_setup(root):
    import tkinter as tk
    from tkinter import ttk

    window = tk.Tk()
    window.title("Фотоаудитор — подключение")
    window.geometry("600x280")
    window.resizable(False, False)
    window.attributes("-topmost", True)
    panel = ttk.Frame(window, padding=20)
    panel.pack(fill="both", expand=True)
    ttk.Label(panel, text=f"Подключение @{BOT_USERNAME}", font=("Segoe UI", 12, "bold")).pack(anchor="w")
    ttk.Label(panel, text="Скопируйте токен в BotFather и нажмите «Вставить из буфера».\n"
              "Можно вставить сообщение целиком. Токен останется скрытым.", wraplength=550).pack(anchor="w", pady=(12, 10))
    token_var = tk.StringVar()
    entry = ttk.Entry(panel, textvariable=token_var, show="*", width=75)
    entry.pack(fill="x")
    entry.bind("<Control-KeyPress>", lambda event: control_shortcut(event, entry))
    status = tk.StringVar(value="Ожидаю вставку токена.")
    status_label = ttk.Label(panel, textvariable=status, wraplength=550)
    status_label.pack(anchor="w", pady=(12, 8))
    row = ttk.Frame(panel)
    row.pack(fill="x", side="bottom")
    state = {"future": None, "token": None, "result": {"connected": False, "cancelled": True}}
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="photo-auditor-setup")

    def note(text, error=False):
        status.set(text)
        status_label.configure(foreground="#b42318" if error else "#1d4b34")

    def paste():
        try:
            text = window.clipboard_get()
        except tk.TclError:
            note("Буфер обмена пуст. Нажмите копирование токена в BotFather.", True)
            return
        token_var.set(text)
        entry.icursor("end")
        try:
            normalize_token(text)
        except SetupError as error:
            note(str(error), True)
        else:
            note("Токен найден. Нажмите «Подключить».")

    def set_busy(busy):
        for widget in (entry, paste_button, connect_button):
            widget.configure(state="disabled" if busy else "normal")

    def poll():
        future = state["future"]
        if not future.done():
            window.after(100, poll)
            return
        try:
            future.result()
            state["result"] = store_verified_token(root, state["token"])
        except SetupError as error:
            note(str(error), True)
            set_busy(False)
            entry.focus_set()
        except Exception:
            note("Не удалось сохранить защищённое подключение. Токен не отображён; попробуйте снова.", True)
            set_busy(False)
        else:
            token_var.set("")
            window.unbind("<Return>")
            note(f"Готово: @{BOT_USERNAME} подключён. Токен сохранён в зашифрованном виде.")
            window.after(2000, window.destroy)
        finally:
            state["token"] = None

    def connect():
        if state["future"] is not None and not state["future"].done():
            return
        try:
            state["token"] = normalize_token(token_var.get())
        except SetupError as error:
            note(str(error), True)
            return
        set_busy(True)
        note("Проверяю подключение к Telegram…")
        state["future"] = executor.submit(verify_token, state["token"])
        window.after(100, poll)

    paste_button = ttk.Button(row, text="Вставить из буфера", command=paste)
    paste_button.pack(side="left")
    connect_button = ttk.Button(row, text="Подключить", command=connect)
    connect_button.pack(side="right")
    ttk.Button(row, text="Закрыть", command=window.destroy).pack(side="right", padx=8)
    window.bind("<Return>", lambda event: connect())
    window.protocol("WM_DELETE_WINDOW", window.destroy)
    window.after(100, lambda: (window.lift(), entry.focus_force()))
    try:
        window.mainloop()
    finally:
        state["token"] = None
        executor.shutdown(wait=False, cancel_futures=True)
    return state["result"]
