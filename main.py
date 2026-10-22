"""Trade With AM desktop GUI. Run with python main.py."""

import queue
from pathlib import Path
import threading
from uuid import uuid4
import tkinter as tk
from tkinter import scrolledtext, ttk
import customtkinter as ctk
import ui_dialogs as messagebox
from telegram_sender import DeliveryOutcome, send_trade_analysis, simple_analysis_complete

from sr_agent import (
    DEFAULT_MODEL, DEFAULT_SYMBOL, TIMEFRAMES, build_support_resistance_prompt,
    fallback_symbols, get_tradingview_watchlist,
    load_codex_models, run_support_resistance_analysis, sanitize_output,
    run_trade_setup_analysis,
    run_elliott_analysis, run_entry_analysis,
    run_simple_smc_analysis,
    run_simple_entry_conditions,
    inspect_tradingview_chart, ensure_selected_symbol,
    prepare_frozen_runtime,
)

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("dark-blue")


def load_agent_image():
    """Optional decoration, resolved beside the source or PyInstaller main module."""
    try:
        from PIL import Image
        path = Path(__file__).resolve().parent / "assets" / "Futuristic AI Robot with Breakout Chart.png"
        with Image.open(path) as source:
            image = source.convert("RGBA")
        size = (160, round(image.height * 160 / image.width))
        return ctk.CTkImage(light_image=image, dark_image=image, size=size)
    except (ImportError, OSError, ValueError, SyntaxError):
        return None  # A missing/unreadable illustration must not prevent startup.


class SupportResistanceApp:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.running = False
        self.refreshing = False
        self.telegram_attempted = set()
        root.title("Trade With AM — SMC Trading Agent")
        try:
            icon = str(Path(__file__).resolve().parent / "assets" / "TradeWithAM_Robot.ico")
            root.iconbitmap(icon)
            root.iconbitmap(default=icon)
        except (OSError, tk.TclError):
            pass  # An unavailable decorative icon must not prevent startup.
        root.geometry("860x740")
        root.minsize(780, 680)
        root.configure(fg_color="#0f1520")
        root.protocol("WM_DELETE_WINDOW", self.close)

        font = ("Segoe UI", 13)
        muted = {"font": ("Segoe UI", 12), "text_color": "#a6b6cb", "anchor": "w"}
        panel = ctk.CTkFrame(root, corner_radius=16, fg_color="#172131", border_width=1, border_color="#293b52")
        panel.pack(fill="both", expand=True, padx=16, pady=16)
        body = ctk.CTkFrame(panel, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=20)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(9, weight=1)
        ctk.CTkLabel(body, text="TRADE WITH AM", font=("Segoe UI", 13, "bold"), text_color="#8fbfff", height=20).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(body, text="Trade With AM — SMC Trading Agent", font=("Segoe UI", 26, "bold"), wraplength=500, justify="left").grid(row=1, column=0, sticky="w", pady=(5, 4))
        ctk.CTkLabel(body, text="Choose one chart. Prepare a request. Run only after confirmation.", **muted).grid(row=2, column=0, sticky="w", pady=(0, 20))
        self.agent_image = load_agent_image()  # Keep the image alive for the window's lifetime.
        self.agent_image_label = None
        if self.agent_image is not None:
            self.agent_image_label = ctk.CTkLabel(body, text="", image=self.agent_image, fg_color="transparent")
            self.agent_image_label.grid(row=0, column=1, rowspan=3, sticky="ne", padx=(16, 0))

        chart_row = ctk.CTkFrame(body, fg_color="transparent")
        chart_row.grid(row=3, column=0, columnspan=2, sticky="ew")
        chart_row.columnconfigure(1, weight=1)
        ctk.CTkLabel(chart_row, text="TradingView Chart", font=font).grid(row=0, column=0, padx=(0, 12))
        self.symbol = tk.StringVar(value=DEFAULT_SYMBOL)
        self.symbol_box = ctk.CTkComboBox(chart_row, variable=self.symbol, values=fallback_symbols(), width=260, font=font)
        self.symbol_box.grid(row=0, column=1, sticky="ew")
        self.all_charts = tk.BooleanVar(value=False)
        self.all_charts_box = ctk.CTkCheckBox(chart_row, text="All Charts", variable=self.all_charts, width=95, checkbox_width=20, checkbox_height=20, font=font)
        self.all_charts_box.grid(row=0, column=2, padx=(16, 5))
        self.all_charts.trace_add("write", lambda *_: self.update_chart_selection())
        self.refresh_button = ctk.CTkButton(chart_row, text="Refresh", command=self.refresh_sources, width=80, font=font)
        self.refresh_button.grid(row=0, column=3, padx=(8, 0))
        self.symbol_source = tk.StringVar(value="Fallback symbols — checking the current TradingView watchlist…")
        ctk.CTkLabel(body, textvariable=self.symbol_source, wraplength=710, **muted).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 17))

        settings = ctk.CTkFrame(body, fg_color="transparent")
        settings.grid(row=5, column=0, columnspan=2, sticky="ew")
        ctk.CTkLabel(settings, text="Analysis", font=font).grid(row=0, column=0, sticky="w", padx=(0, 16))
        # V1 demonstration controls only: never used to filter the prompt.
        self.smc_models = {}
        self.smc_model_boxes = {}
        for column, (name, width) in enumerate((('Turtle Soup',105),('IFVG',65),('ICT 2022',95),
                                               ('CRT',65),('Silver Bullet',110),('Unicorn',90)), 1):
            self.smc_models[name] = tk.BooleanVar(value=True)
            checkbox = ctk.CTkCheckBox(settings, text=name, variable=self.smc_models[name], width=width,
                                      checkbox_width=20, checkbox_height=20, font=font)
            checkbox.grid(row=0, column=column, sticky="w", padx=(0, 5))
            self.smc_model_boxes[name] = checkbox
        # Retain internal legacy selection values for the reused backend. They
        # have no visible controls and are never read by the entry inquiry.
        self.timeframes = {f:tk.BooleanVar(value=f!='15m') for f in TIMEFRAMES}
        self.levels = tk.StringVar(value="3")
        ctk.CTkLabel(settings, text="Codex model", font=font).grid(row=1, column=0, sticky="w", pady=(16, 0))
        self.model = tk.StringVar(value="")
        self.model_box = ctk.CTkComboBox(settings, variable=self.model, values=[], width=210, state="readonly", font=font)
        self.model_box.grid(row=1, column=1, columnspan=2, sticky="w", pady=(16, 0))
        self.model_source = tk.StringVar(value="Checking local catalog…")
        ctk.CTkLabel(settings, textvariable=self.model_source, **muted).grid(row=1, column=3, columnspan=2, sticky="w", pady=(16, 0))

        actions = ctk.CTkFrame(body, fg_color="transparent")
        actions.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(23, 10))
        button_style = {"width": 290, "height": 42, "corner_radius": 8, "font": ("Segoe UI", 13, "bold")}
        self.find_button = ctk.CTkButton(actions, text="Find SMC Trade Setups", command=self.find_levels, **button_style)
        self.find_button.pack(side="left")
        self.trade_button = ctk.CTkButton(actions, text="Check Entry Conditions", command=self.find_trade_setups, **button_style)
        self.trade_button.pack(side="left", padx=(10, 0))
        self.show_status = tk.BooleanVar(value=False)
        self.show_status_box = ctk.CTkCheckBox(actions, text="Show Status", variable=self.show_status,
                                               width=100, checkbox_width=20, checkbox_height=20, font=font,
                                               command=self.update_status_visibility)
        self.show_status_box.pack(side="left", padx=(10, 0))
        ctk.CTkLabel(body, text="Live runs verify the current chart after approval. Existing drawings must be preserved.", **muted).grid(row=7, column=0, columnspan=2, sticky="w")

        self.status_row = ctk.CTkFrame(body, fg_color="transparent")
        self.status_row.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(16, 6))
        self.status_row.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value="Ready — no analysis has been run")
        self.status_label = ctk.CTkLabel(self.status_row, textvariable=self.status, wraplength=500, height=42,
                                        font=font, anchor="w", justify="left")
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.progress = ctk.CTkProgressBar(self.status_row, mode="indeterminate", width=165, height=10)
        self.progress.set(0)
        self.progress.grid(row=0, column=1, sticky="e", padx=(14, 0))

        self.output_section = ctk.CTkFrame(body, fg_color="transparent")
        self.output_section.grid(row=9, column=0, columnspan=2, sticky="nsew")
        self.output_section.columnconfigure(0, weight=1)
        self.output_section.rowconfigure(1, weight=1)
        ctk.CTkLabel(self.output_section, text="OUTPUT / STATUS", font=("Segoe UI", 13, "bold"), text_color="#8fbfff").grid(row=0, column=0, sticky="w", pady=(6, 5))
        self.output = ctk.CTkTextbox(self.output_section, wrap="word", font=("Consolas", 12), height=190, corner_radius=10, fg_color="#101823", text_color="#dbe7f4")
        self.output.grid(row=1, column=0, sticky="nsew")
        self.update_status_visibility()
        self.log("GUI ready.\n"
                 "The Find button asks for live-test confirmation before launching the CLI.\n"
                 "Start TradingView with your existing debug launcher when preparing a live test.\n"
                 "The left button displays Codex's final response and sends a concise Telegram report after success.")
        root.after(100, self.poll_events)
        root.after(0, self.refresh_sources)

    def update_status_visibility(self):
        if self.show_status.get():
            self.output_section.grid()
        else:
            self.output_section.grid_remove()
        self.root.update_idletasks()
        # CTk geometry uses logical units; Tk's requested sizes are physical pixels.
        logical_width = int(self.root.geometry().split("x", 1)[0])
        scale = self.root.winfo_width() / logical_width if logical_width > 1 else 1
        height = round(self.root.winfo_reqheight() / scale) + 1
        minimum = max(680, height) if self.show_status.get() else height
        self.root.minsize(780, minimum)
        width = max(780, logical_width)
        self.root.geometry(f"{width}x{max(740, minimum) if self.show_status.get() else minimum}")

    def update_chart_selection(self):
        self.symbol_box.configure(state="disabled" if self.all_charts.get() else "normal")

    def refresh_sources(self):
        if self.refreshing or self.running:
            return
        self.refreshing = True
        self.refresh_button.configure(state="disabled")
        self.symbol_source.set("Refreshing TradingView watchlist… (existing choices shown until the read completes)")
        threading.Thread(target=self.sources_worker, daemon=True).start()

    def sources_worker(self):
        # Both readers are independent of Tk and never invoke an AI model.
        self.events.put(("models", load_codex_models()))
        self.events.put(("watchlist", get_tradingview_watchlist()))

    def apply_models(self, result):
        models = result["models"]
        self.model_box.configure(values=models)
        if self.model.get() not in models:
            self.model.set(DEFAULT_MODEL if DEFAULT_MODEL in models else (models[0] if models else ""))
        self.model_source.set("Local catalog; access untested" if models else "Catalog unavailable — see output")
        if result["error"]:
            self.log("MODEL CATALOG ERROR\n" + result["error"])
        else:
            self.log(f"Local model catalog: {result['source']}\nFetched: {result['fetched_at']}\n"
                     f"Listed: {', '.join(models)}\nAccess has not been tested by an invocation in this app.")

    def apply_watchlist(self, result):
        self.refreshing = False
        self.refresh_button.configure(state="disabled" if self.running else "normal")
        symbols = result["symbols"]
        self.symbol_box.configure(values=symbols)
        if self.symbol.get() not in symbols:
            self.symbol.set(DEFAULT_SYMBOL if DEFAULT_SYMBOL in symbols else symbols[0])
        self.update_chart_selection()
        if result["source"] == "live":
            self.symbol_source.set(f"TradingView watchlist: {result['name']} — {len(symbols)} actual symbols")
            self.log(f"Retrieved {len(symbols)} symbols from TradingView watchlist {result['name']!r}. No chart selection was changed.")
        else:
            self.symbol_source.set("Fallback symbol list — live watchlist unavailable. Type a symbol or set SR_SYMBOLS.")
            self.log("WATCHLIST FALLBACK\n" + result["error"])

    def selection(self, *, trade=False, elliott_wave=False):
        try:
            count = 1 if trade else int(self.levels.get())
        except ValueError:
            raise ValueError("Levels per side must be a whole number from 1 to 20.") from None
        symbol = DEFAULT_SYMBOL if self.all_charts.get() else self.symbol.get().strip()
        values = (symbol, [f for f, v in self.timeframes.items() if v.get()], count, self.model.get().strip())
        if elliott_wave:
            frames = ['1D','4H','1H','15m','5m']  # Demonstration checkboxes never filter V1 analysis.
            values = (symbol, frames, 1, self.model.get().strip())
        build_support_resistance_prompt(*values)  # Shared validation for both buttons.
        return values

    def log(self, text):
        self.output.configure(state="normal")
        self.output.insert("end", text + "\n\n")
        self.output.see("end")
        self.output.configure(state="disabled")

    def find_levels(self):
        self.start_analysis(trade=True, elliott_wave=True)

    def find_trade_setups(self):
        self.start_analysis(trade=True)

    def start_analysis(self, trade=False, *, elliott_wave=False):
        if self.running:
            return
        if self.all_charts.get():
            self.status.set("Multi-chart execution is not yet implemented — Codex was not started")
            self.log("All Charts: multi-chart execution is not yet implemented. Uncheck All Charts to use a single chart.")
            return
        try:
            values = self.selection(trade=True, elliott_wave=True) if elliott_wave else self.selection(trade=True) if trade else self.selection()
        except ValueError as exc:
            messagebox.showerror("Check selection", str(exc), parent=self.root)
            return
        self.running = True
        self.find_button.configure(state="disabled")
        self.trade_button.configure(state="disabled")
        self.refresh_button.configure(state="disabled")
        self.status.set("Inspecting the current TradingView chart before confirmation…")
        self.progress.start()
        threading.Thread(target=self.preview_worker, args=(values, trade, elliott_wave), daemon=True).start()

    def preview_worker(self, values, trade, elliott_wave=False):
        try:
            chart = inspect_tradingview_chart()
            self.events.put(("symbol_preview", (values, trade, chart, elliott_wave)))
        except Exception as exc:
            self.events.put(("chart_error", sanitize_output(exc)))

    def confirm_analysis(self, values, trade, approved_chart, elliott_wave=False):
        # Called only on Tk's main thread after the read-only preview.
        import json
        current = json.loads(approved_chart)
        symbol, frames, count, model = values
        timeframes = ", ".join(frames[:-1]) + " and " + frames[-1] if len(frames) > 1 else frames[0]
        drawings = "up to three justified SMC intraday setups" if elliott_wave else "support and resistance levels"
        details = "All six models are considered; checkboxes are demonstration-only in V1. Astra creates drawings and groups directly. The complete response is sent to Telegram after success. No automatic deletion." if elliott_wave else ""
        if current["symbol"] != symbol:
            details = f"The chart will switch: {current['symbol']} → {symbol}"
            details += "; Telegram follows successful analysis; no automatic deletion." if elliott_wave else "."
        message = ((f"Read current conditions and existing SMC groups for {symbol}.\n\n" if trade and not elliott_wave else
                   f"Analyze {symbol} on {timeframes}, then draw {drawings} on TradingView.\n\n")
                   + (details + " " if details else "") + "Continue?")
        if not messagebox.askokcancel(
            "Confirm SMC Analysis" if elliott_wave else "Check Entry Conditions" if trade else "Confirm TradingView Analysis",
            message,
            parent=self.root, default="cancel",
        ):
            self.finish_job()
            self.status.set("Cancelled — Codex was not started")
            return
        self.status.set("Selecting and verifying the approved TradingView symbol before Codex…")
        self.log(f"Checking current chart: {symbol} | {', '.join(frames)} | {model}\n"
                 "Codex will start only if the exact chart and candlestick type are verified.")
        threading.Thread(target=self.worker, args=(values, trade, approved_chart, elliott_wave), daemon=True).start()

    def worker(self, values, trade=False, approved_chart="", elliott_wave=False, *, illustrative_test=False,
               replacement_approval=None, send_telegram=True, allow_replacement=True):
        # Only the main Tk thread touches widgets. The reusable function owns execution.
        if illustrative_test and not (trade and elliott_wave):
            self.events.put(("error", "Illustrative test is only valid for the SMC setup worker."))
            return
        try:
            chart = ensure_selected_symbol(values[0], approved_chart, live_confirmed=True)
        except Exception as exc:
            self.events.put(("chart_error", sanitize_output(exc)))
            return
        try:
            self.events.put(("chart_verified", chart))
            if trade and elliott_wave and not illustrative_test:
                categories = ['smc']
                outcome=run_simple_smc_analysis(values[0],categories,values[3],confirmed_chart=chart,live_confirmed=True,
                    on_phase=lambda category:self.events.put(('simple_phase',category)))
                if send_telegram and simple_analysis_complete(outcome):
                    report=outcome.trade_report
                    if not isinstance(report,dict) or report.get('analysis_kind')!='simple_smc':
                        report=dict(analysis_kind='simple_smc',run_id=uuid4().hex,categories=categories,
                            cli_completed=True,final_analysis=outcome.final_analysis)
                        outcome.trade_report=report
                    run_id=report['run_id']
                    if run_id in self.telegram_attempted:
                        outcome.telegram_delivery=DeliveryOutcome('failed',error='Telegram was already attempted for this result. No automatic resend.')
                    else:
                        self.telegram_attempted.add(run_id)
                        self.events.put(('telegram_sending',None))
                        try:
                            outcome.telegram_delivery=send_trade_analysis(report,values[0],values[1],
                                chart_verified=False,live_send_enabled=True)
                        except Exception:
                            outcome.telegram_delivery=DeliveryOutcome('failed',error='Telegram delivery could not be confirmed. No automatic retry.')
                self.events.put(('simple_result',outcome))
                return  # No legacy drawing parser/grouping path for this execution.
            if trade and elliott_wave:
                if illustrative_test:
                    outcome = run_trade_setup_analysis(values[0], values[1], values[3], confirmed_chart=chart, live_confirmed=True,
                        elliott_wave=True, illustrative_test=True)
                else:
                    categories = ['smc']
                    outcome = run_elliott_analysis(values[0], categories, values[3], confirmed_chart=chart, live_confirmed=True,
                        replacement_approval=replacement_approval, allow_replacement=allow_replacement)
                    if outcome.status == 'replacement_confirmation_required':
                        self.events.put(('replacement_confirmation', (values, chart, outcome)))
                        return
                if not illustrative_test and send_telegram and outcome.drawings_verified and outcome.status=='completed_verified' and outcome.trade_report is not None:
                    run_id = outcome.trade_report.get("run_id")
                    if not isinstance(run_id, str) or not run_id or run_id in self.telegram_attempted:
                        outcome.telegram_delivery = DeliveryOutcome("failed", error="Delivery was not repeated: missing or previously attempted analysis identity.")
                    else:
                        # Mark before HTTP; timeout/uncertain delivery must never
                        # cause an automatic duplicate attempt for this result.
                        self.telegram_attempted.add(run_id)
                        self.events.put(("telegram_sending", None))
                        try:
                            outcome.telegram_delivery = send_trade_analysis(
                                outcome.trade_report, values[0], values[1], chart_verified=outcome.drawings_verified,
                                live_send_enabled=True,
                            )
                        except Exception:
                            outcome.telegram_delivery = DeliveryOutcome("failed", error="Telegram delivery could not be confirmed. No automatic retry.")
            elif trade:
                outcome = run_simple_entry_conditions(values[0], values[3], confirmed_chart=chart, live_confirmed=True)
            else:
                outcome = run_support_resistance_analysis(*values, confirmed_chart=chart, live_confirmed=True)
            self.events.put(("elliott_result" if elliott_wave else "entry_result" if trade else "result", outcome))
        except Exception as exc:
            self.events.put(("error", sanitize_output(exc)))

    def finish_job(self):
        self.running = False
        self.progress.stop()
        self.find_button.configure(state="normal")
        self.trade_button.configure(state="normal")
        self.refresh_button.configure(state="disabled" if self.refreshing else "normal")

    def poll_events(self):
        try:
            kind, value = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            if kind == "models":
                self.apply_models(value)
                self.root.after(100, self.poll_events)
                return
            if kind == "watchlist":
                self.apply_watchlist(value)
                self.root.after(100, self.poll_events)
                return
            if kind == "chart_verified":
                self.status.set("Codex is running — waiting for response (up to 10 minutes)")
                self.log("Verified TradingView target:\n" + value)
                self.root.after(100, self.poll_events)
                return
            if kind == "symbol_preview":
                self.confirm_analysis(*value)
                self.root.after(100, self.poll_events)
                return
            if kind == 'simple_phase':
                self.status.set('SMC — Codex is running; waiting for response…')
                self.log('Running reference_simple/smc_setups.txt'+
                         ' through the simple CLI.')
                self.root.after(100,self.poll_events)
                return
            if kind == 'replacement_confirmation':
                self.finish_job()
                values, chart, outcome = value
                inspection = outcome.trade_report['replacement']
                names = '\n'.join(f"{g['name']} ({g['id']}): {len(g['ids'])} drawings" for g in inspection['groups'])
                if messagebox.askokcancel('Replace selected SMC scenarios', names + '\n\n'
                    'Remove these selected-category groups and their verified members BEFORE new analysis? Unselected groups remain. '
                    'A later failure may leave old scenarios absent or new results incomplete. No rollback or automatic retry.',
                    parent=self.root, default='cancel'):
                    self.running=True
                    for button in (self.find_button,self.trade_button,self.refresh_button): button.configure(state='disabled')
                    self.progress.start();self.status.set('Verifying confirmed removal scope, then running fresh analysis…')
                    threading.Thread(target=self.worker,args=(values,True,chart,True),
                        kwargs={'replacement_approval':inspection},daemon=True).start()
                else:
                    self.status.set('Cancelled — no scenario removal or analysis started')
                self.root.after(100,self.poll_events)
                return
            if kind == "telegram_sending":
                self.status.set("Analysis finished — sending final report to Telegram; waiting for API acceptance…")
                self.log("Telegram delivery started in the background. Analysis and chart verification outcomes remain separate.")
                self.root.after(100, self.poll_events)
                return
            self.finish_job()
            if kind == 'simple_result':
                if value.output:self.log(value.output)
                if value.errors:self.log(value.errors)
                if value.status=='completed_response':
                    delivery=value.telegram_delivery
                    accepted=isinstance(delivery,DeliveryOutcome) and delivery.accepted
                    self.status.set('Codex response ready — '+('Telegram accepted' if accepted else 'Telegram not sent or unconfirmed'))
                    if isinstance(delivery,DeliveryOutcome) and not accepted:
                        messagebox.showwarning('Telegram delivery not confirmed',delivery.error+'\nNo automatic retry. The analysis remains available in Copy/Close.',parent=self.root)
                else:
                    self.status.set('Codex failed — remaining analysis stopped')
                    messagebox.showwarning('SMC analysis stopped',value.errors or value.status,parent=self.root)
                if value.final_analysis:
                    self.show_trade_results(value.final_analysis,title='SMC — Analysis Results')
                self.root.after(100,self.poll_events)
                return
            if kind == 'entry_result':
                self.status.set(value.status + ' | read-only entry inquiry')
                for label, text in (('CODEX RESPONSE',value.response),('CODEX STDOUT',value.output),('CLI DIAGNOSTICS / ERRORS',value.errors)):
                    if text:self.log(label+'\n'+text)
                if value.status == 'no_scenarios':
                    messagebox.showinfo('Check Entry Conditions',value.final_analysis,parent=self.root)
                elif value.analysis_completed and value.status in ('completed_response','completed_verified'):
                    self.show_trade_results(value.final_analysis,title='SMC — Entry Conditions')
                else:
                    self.log(value.errors)
                    messagebox.showwarning('Entry inquiry incomplete',value.errors or value.status,parent=self.root)
                self.root.after(100,self.poll_events)
                return
            if kind == "chart_error":
                self.status.set("Chart verification failed — Codex was not started")
                self.log(value)
                messagebox.showerror("TradingView chart verification failed", value, parent=self.root)
            elif kind == "error":
                self.status.set("Execution error — inspect chart before another run")
                self.log(value)
            else:
                code = value.return_code if value.return_code is not None else "not available"
                self.status.set(f"{value.status} | return code: {code} | "
                                + ("analysis and chart state verified" if kind in ("trade_result", "elliott_result") and value.drawings_verified else
                                   "drawings and group verified" if value.drawings_verified else "verification incomplete"))
                self.log(f"Execution status: {value.status}\nSubprocess return code: {code}")
                if value.response:
                    self.log("CODEX RESPONSE\n" + value.response)
                if value.output and value.output != value.response:
                    self.log("CODEX STDOUT\n" + value.output)
                if value.errors:
                    self.log("CLI DIAGNOSTICS / ERRORS\n" + value.errors)
                if kind in ("trade_result", "elliott_result"):
                    if not value.drawings_verified or value.status not in ("completed_verified", "completed_no_trade"):
                        messagebox.showwarning("SMC — Verification Incomplete" if kind == "elliott_result" else "Trade Setups — Verification Incomplete",
                                               f"Outcome: {value.status}\n\n"
                                               "Drawing/group verification is incomplete. Partial changes may exist and were preserved. "
                                               "See Output / Status and inspect TradingView before another run. No automatic retry.\n\n" + value.errors[:1200],
                                               parent=self.root)
                    if value.analysis_completed and value.final_analysis:
                        if kind == "elliott_result":
                            if value.trade_report and value.trade_report.get('analysis_kind')=='elliott_wave':
                                self.show_telegram_outcome(value)
                                if isinstance(value.telegram_delivery,DeliveryOutcome) and value.telegram_delivery.accepted:
                                    self.show_trade_results(value.final_analysis,title='SMC — Analysis Results')
                                self.root.after(100,self.poll_events)
                                return
                            if value.drawings_verified and value.status in ("completed_verified", "completed_no_trade"):
                                count = len(value.trade_report["setups"])
                                illustration = bool(value.trade_report.get("illustrative_test"))
                                messagebox.showinfo("Analysis Completed",
                                    "NO TRADE / WAIT — TEST SCENARIO ONLY: 1 illustration and group verified." if count and illustration else
                                    "1 SMC setup and group verified." if count else
                                    "NO TRADE / WAIT — no qualifying setup; no new drawings or group created.", parent=self.root)
                            self.show_trade_results(value.final_analysis, title="SMC — Analysis Results")
                        else:
                            self.show_telegram_outcome(value)
                elif value.drawings_verified and value.status == "completed_verified":
                    messagebox.showinfo("Analysis Completed",
                                        "Support and resistance levels have been successfully drawn and grouped on TradingView.",
                                        parent=self.root)
                else:
                    self.log("Success was not verified. Review the output and TradingView before another run. No automatic retry.")
                    messagebox.showwarning("Analysis not fully verified",
                                           f"Outcome: {value.status}\n\n"
                                           "Drawing and grouping completion was not verified. Partial changes may exist. "
                                           "See Output / Status and inspect TradingView before another run. No automatic retry.",
                                           parent=self.root)
        self.root.after(100, self.poll_events)

    def show_telegram_outcome(self, outcome):
        delivery = outcome.telegram_delivery
        accepted = isinstance(delivery, DeliveryOutcome) and delivery.accepted
        count = len(outcome.trade_report["setups"]) if outcome.trade_report is not None else None
        chart_ok = outcome.drawings_verified and outcome.status in ("completed_verified", "completed_no_trade")
        ew = outcome.trade_report is not None and outcome.trade_report.get('analysis_kind')=='elliott_wave'
        summary = (' | '.join(f"{c.title()}: {outcome.trade_report['phases'][c]['scenario_count']} scenarios"
                   for c in outcome.trade_report['categories']) if ew else '')
        if accepted and count is not None:
            self.log(f"Telegram API accepted {delivery.accepted_parts}/{delivery.required_parts} messages. "
                     "This confirms API acceptance, not that the recipient read them.")
            self.status.set("Analysis completed | Telegram accepted all messages | "
                            + ("chart verification passed" if chart_ok else "chart verification INCOMPLETE"))
            if chart_ok:
                message = (summary + '\nDrawings/groups verified; Telegram accepted the concise report.' if ew else
                           "NO TRADE / WAIT assessment sent to Telegram successfully." if count == 0 else
                           f"{count} trade setup{'s' if count != 1 else ''} found and sent to Telegram successfully.")
                messagebox.showinfo("Analysis Completed", message, parent=self.root)
            else:
                messagebox.showwarning("Telegram Accepted — Chart Verification Incomplete",
                                       "Telegram accepted the final analysis, but chart drawings/groups are not fully verified. "
                                       "Inspect TradingView and Output / Status; no automatic retry.", parent=self.root)
                self.show_trade_results(outcome.final_analysis)
        else:
            error = delivery.error if isinstance(delivery, DeliveryOutcome) else "No confirmed Telegram delivery record is available."
            parts = f"Accepted parts: {delivery.accepted_parts}/{delivery.required_parts}. " if isinstance(delivery, DeliveryOutcome) else ""
            self.log("TELEGRAM DELIVERY NOT CONFIRMED\n" + parts + error)
            self.status.set("Analysis completed | Telegram delivery failed/unconfirmed | "
                            + ("chart verification passed" if chart_ok else "chart verification INCOMPLETE"))
            messagebox.showwarning("Telegram Delivery Failed", "Analysis completed, but Telegram delivery failed.\n\n"
                                   + parts + error + "\nThe final analysis is available in the Copy/Close window. "
                                   "Inspect Telegram before a manual resend; no automatic retry.", parent=self.root)
            self.show_trade_results(outcome.final_analysis)

    def show_trade_results(self, text, *, title="Trade Setups — Analysis Results"):
        """Show the final response, never tool stdout or diagnostics."""
        popup = tk.Toplevel(self.root)
        popup.title(title)
        popup.geometry("760x580")
        popup.minsize(540, 360)
        popup.resizable(True, True)
        popup.transient(self.root)
        analysis = scrolledtext.ScrolledText(popup, name="analysis", wrap="word", font=("Segoe UI", 11), padx=14, pady=14)
        analysis.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        analysis.insert("1.0", text)
        analysis.configure(state="disabled")
        actions = ttk.Frame(popup, name="actions", padding=(12, 6, 12, 12))
        actions.pack(fill="x")

        def copy():
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
            except tk.TclError as exc:
                messagebox.showerror("Copy Trade Setups", str(exc), parent=popup)

        ttk.Button(actions, name="copy", text="Copy", command=copy).pack(side="left")
        ttk.Button(actions, name="close", text="Close", command=popup.destroy).pack(side="right")
        return popup

    def close(self):
        if self.running:
            messagebox.showinfo("Codex is running", "Keep this window open until the request finishes.\n"
                                "Closing it would not reliably undo or stop chart changes.", parent=self.root)
            return
        self.root.destroy()


def main():
    root = ctk.CTk()
    prepare_frozen_runtime()
    SupportResistanceApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
