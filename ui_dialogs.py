"""Small modal dark replacement for the app's simple OK / OK-Cancel messages."""
import tkinter as tk
from pathlib import Path
import customtkinter as ctk


def _show(title, message, *, parent, confirm=False, default="cancel"):
    owner = parent.winfo_toplevel()
    previous_grab, previous_focus = owner.grab_current(), owner.focus_get()
    dialog = ctk.CTkToplevel(owner, fg_color="#0f1520")
    dialog.withdraw()
    dialog.title(title)
    dialog.transient(owner)
    try:
        dialog.iconbitmap(str(Path(__file__).resolve().parent / "assets" / "TradeWithAM_Robot.ico"))
    except (OSError, tk.TclError):
        pass
    dialog.resizable(False, False)
    approved = False
    closed = False

    def destroy_dialog():
        dialog.grab_release()
        # CTk schedules title-bar/focus updates. Cancel only callbacks owned by
        # this dialog and its children so rapid closes leave no stale Tcl calls.
        widgets, commands = [dialog], set()
        for widget in widgets:
            commands.update(getattr(widget, "_tclCommands", None) or [])
            widgets.extend(widget.winfo_children())
        for callback in dialog.tk.call("after", "info"):
            if str(dialog.tk.call("after", "info", callback)[0]) in commands:
                dialog.tk.call("after", "cancel", callback)
        dialog.destroy()

    def finish(value=False):
        nonlocal approved, closed
        if closed:
            return
        closed = True
        approved = value is True
        # Let CTk's Windows title-bar update unwind before destroying its window.
        # Keep the modal grab until destruction; repeated clicks do nothing.
        dialog.after(30, destroy_dialog)

    panel = ctk.CTkFrame(dialog, corner_radius=12, fg_color="#172131")
    panel.pack(fill="both", expand=True, padx=12, pady=12)
    ctk.CTkLabel(panel, text=title, font=("Segoe UI", 17, "bold"), text_color="#eef4ff",
                 anchor="w", justify="left", wraplength=440).pack(fill="x", padx=20, pady=(16, 10))
    # Ordinary messages use a compact label. Keep unusually long errors readable
    # without truncation or an oversized window.
    font = ctk.CTkFont(family="Segoe UI", size=14)
    lines = sum(max(1, font.measure(line) // 420 + 1) for line in message.split("\n"))
    if lines > 12:
        body = ctk.CTkTextbox(panel, width=440, height=220, font=font, wrap="word",
                             fg_color="#172131", text_color="#dbe7f4")
        body.insert("1.0", message)
        body.configure(state="disabled")
    else:
        body = ctk.CTkLabel(panel, text=message, width=440, font=font, text_color="#dbe7f4",
                           anchor="w", justify="left", wraplength=440)
    body.pack(fill="x", padx=20)
    actions = ctk.CTkFrame(panel, fg_color="transparent")
    actions.pack(fill="x", padx=20, pady=(18, 16))
    cancel = None
    if confirm:
        cancel = ctk.CTkButton(actions, text="Cancel", width=100, height=34, font=("Segoe UI", 13),
                               fg_color="#374151", hover_color="#4b5563", command=finish)
        cancel.pack(side="right", padx=(10, 0))
    ok = ctk.CTkButton(actions, text="OK", width=100, height=34, font=("Segoe UI", 13),
                       fg_color="#1f538d", hover_color="#2869ad", command=lambda: finish(True))
    ok.pack(side="right")
    dialog.protocol("WM_DELETE_WINDOW", finish)
    dialog.bind("<Escape>", lambda event: finish())
    dialog.bind("<Return>", lambda event: finish(not confirm or event.widget is ok))
    dialog.update_idletasks()
    x = max(0, owner.winfo_rootx() + (owner.winfo_width() - dialog.winfo_reqwidth()) // 2)
    y = max(0, owner.winfo_rooty() + (owner.winfo_height() - dialog.winfo_reqheight()) // 2)
    dialog.geometry(f"+{x}+{y}")
    try:
        dialog.deiconify()
        dialog.wait_visibility()
        dialog.lift()
        dialog.grab_set()
        (cancel if confirm and default != "ok" else ok).focus_set()
        dialog.wait_window()
    finally:
        if dialog.winfo_exists():
            destroy_dialog()
        for widget, method in ((previous_grab, "grab_set"), (previous_focus, "focus_set")):
            try:
                if widget is not None and widget.winfo_exists():
                    getattr(widget, method)()
            except tk.TclError:
                pass  # The owner may have been closed while the dialog was open.
    return approved


def askokcancel(title, message, *, parent, default="cancel"):
    return _show(title, message, parent=parent, confirm=True, default=default)


def showinfo(title, message, *, parent):
    return _show(title, message, parent=parent)


def showwarning(title, message, *, parent):
    return _show(title, message, parent=parent)


def showerror(title, message, *, parent):
    return _show(title, message, parent=parent)
