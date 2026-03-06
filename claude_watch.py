#!/usr/bin/python3
"""Claude Watch — GNOME tray usage indicator for Claude.ai."""

import hashlib
import json
import os
import shutil
import sqlite3
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import AyatanaAppIndicator3 as AppIndicator3
from gi.repository import GLib, Gdk, Gtk, Pango, PangoCairo

CONFIG_PATH = Path.home() / ".config" / "claude-watch" / "config.json"
CLAUDE_BASE_URL = "https://claude.ai"
REFRESH_OPTIONS = [1, 5, 10]
DEBUG = os.environ.get("CLAUDE_WATCH_DEBUG", "").lower() in ("1", "true", "yes")

_ICON_PATHS = ["/tmp/claude-watch-0.png", "/tmp/claude-watch-1.png"]

CHROME_PATHS = [
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/snap/bin/chromium",
]

DEFAULT_CONFIG = {
    "refresh_minutes": 5,
    "icon_color": "#ffffff",
    "show_extra_usage": False,
    "extra_usage_mode": "spent",
}


class Config:
    def __init__(self):
        self.data = dict(DEFAULT_CONFIG)
        self.load()

    def load(self):
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH) as f:
                    saved = json.load(f)
                    for k in DEFAULT_CONFIG:
                        if k in saved:
                            self.data[k] = saved[k]
            except Exception:
                pass

    def save(self):
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(self.data, f, indent=2)

    def __getattr__(self, name):
        if name == "data":
            raise AttributeError(name)
        return self.data.get(name)

    def __setattr__(self, name, value):
        if name == "data":
            super().__setattr__(name, value)
        else:
            self.data[name] = value


@dataclass
class UsageData:
    pct_5h: float = 0.0
    reset_5h: Optional[datetime] = None
    pct_week: float = 0.0
    reset_week: Optional[datetime] = None
    extra_used: float = 0.0
    extra_currency: str = ""
    extra_enabled: bool = False
    prepaid_balance: float = 0.0
    prepaid_currency: str = ""
    last_updated: Optional[datetime] = None
    error: Optional[str] = None


class ClaudeAIScraper:

    def _get_chrome_key(self) -> Optional[bytes]:
        try:
            import dbus
            bus = dbus.SessionBus()
            svc = bus.get_object("org.freedesktop.secrets", "/org/freedesktop/secrets")
            svc_iface = dbus.Interface(svc, "org.freedesktop.Secret.Service")
            _, session = svc_iface.OpenSession("plain", dbus.String("", variant_level=1))
            for app in ("chrome", "chromium"):
                unlocked, _ = svc_iface.SearchItems({"application": app})
                if unlocked:
                    secrets = svc_iface.GetSecrets(list(unlocked), session)
                    for secret in secrets.values():
                        pwd = bytes(secret[2]).decode("utf-8", errors="replace")
                        if pwd:
                            return hashlib.pbkdf2_hmac(
                                "sha1", pwd.encode("utf-8"), b"saltysalt", 1, dklen=16
                            )
        except Exception as e:
            if DEBUG:
                print(f"[claude-watch] Keyring error: {e}", file=sys.stderr)
        for pwd in ("", "peanuts"):
            try:
                return hashlib.pbkdf2_hmac(
                    "sha1", pwd.encode("utf-8"), b"saltysalt", 1, dklen=16
                )
            except Exception:
                pass
        return None

    def _decrypt_chrome_value(self, encrypted: bytes, key: bytes) -> Optional[str]:
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend
            cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16), backend=default_backend())
            decryptor = cipher.decryptor()
            raw = decryptor.update(encrypted) + decryptor.finalize()
            pad = raw[-1]
            if pad > 16:
                return None
            raw = raw[:-pad]
            if len(raw) > 32:
                raw = raw[32:]  # strip 32-byte v11 nonce prefix
            return raw.decode("utf-8")
        except Exception:
            return None

    def _get_chrome_cookies(self) -> list:
        db_paths = [
            Path.home() / ".config" / "google-chrome" / "Default" / "Cookies",
            Path.home() / ".config" / "google-chrome" / "Default" / "Network" / "Cookies",
            Path.home() / ".config" / "chromium" / "Default" / "Cookies",
            Path.home() / ".config" / "chromium" / "Default" / "Network" / "Cookies",
        ]

        key = None
        for db_path in db_paths:
            if not db_path.exists():
                continue
            try:
                with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tf:
                    tmp = tf.name
                shutil.copy2(str(db_path), tmp)
                try:
                    conn = sqlite3.connect(tmp)
                    rows = conn.execute(
                        "SELECT name, value, host_key, path, is_secure, expires_utc, encrypted_value "
                        "FROM cookies WHERE host_key LIKE '%claude.ai'"
                    ).fetchall()
                    conn.close()
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

                if not rows:
                    continue

                if key is None:
                    key = self._get_chrome_key()

                cookies = []
                for name, value, host, path, secure, expires_utc, enc in rows:
                    if enc:
                        enc = bytes(enc)
                        if enc[:3] in (b"v10", b"v11") and key:
                            value = self._decrypt_chrome_value(enc[3:], key)
                    if not value:
                        continue
                    cookie = {"name": name, "value": value, "domain": host, "path": path}
                    if expires_utc and expires_utc > 0:
                        try:
                            # Chrome epoch: microseconds since 1601-01-01
                            unix_ts = (expires_utc / 1_000_000) - 11644473600
                            if unix_ts > 0:
                                cookie["expires"] = int(unix_ts)
                        except Exception:
                            pass
                    if secure:
                        cookie["secure"] = True
                    cookies.append(cookie)

                if any(c["name"] == "sessionKey" for c in cookies):
                    if DEBUG:
                        print(f"[claude-watch] Chrome cookies: {[c['name'] for c in cookies]}", file=sys.stderr)
                    return cookies

            except Exception as e:
                if DEBUG:
                    print(f"[claude-watch] Chrome DB error ({db_path}): {e}", file=sys.stderr)

        return []

    @staticmethod
    def _find_chrome_executable() -> Optional[str]:
        for path in CHROME_PATHS:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return None

    def fetch(self) -> UsageData:
        now = datetime.now(timezone.utc)

        cookies = self._get_chrome_cookies()
        if not cookies:
            return UsageData(
                error="No claude.ai session found in Chrome.\nOpen claude.ai in Chrome and log in, then refresh.",
                last_updated=now,
            )

        chrome_exe = self._find_chrome_executable()
        if not chrome_exe:
            return UsageData(
                error="Chrome not found. Install Google Chrome or Chromium.",
                last_updated=now,
            )

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return UsageData(
                error="Playwright not installed. Run: pip3 install --break-system-packages playwright",
                last_updated=now,
            )

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    executable_path=chrome_exe,
                    args=["--no-sandbox", "--disable-dev-shm-usage",
                          "--disable-blink-features=AutomationControlled"],
                    headless=True,
                )
                ctx = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    )
                )
                ctx.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                ctx.add_cookies(cookies)
                page = ctx.new_page()

                resp = page.goto(
                    f"{CLAUDE_BASE_URL}/api/organizations",
                    wait_until="domcontentloaded", timeout=20000,
                )
                if resp.status in (401, 403):
                    browser.close()
                    return UsageData(
                        error="Session expired — open claude.ai in Chrome and log in again.",
                        last_updated=now,
                    )

                orgs_raw = page.evaluate("document.body.innerText")
                if DEBUG:
                    print(f"[claude-watch] /api/organizations: {orgs_raw[:300]}", file=sys.stderr)

                try:
                    orgs = json.loads(orgs_raw)
                except Exception:
                    browser.close()
                    return UsageData(
                        error="Unexpected response from claude.ai — try refreshing.",
                        last_updated=now,
                    )

                if not isinstance(orgs, list) or not orgs:
                    browser.close()
                    return UsageData(error="No organizations found.", last_updated=now)

                org = next(
                    (o for o in orgs if "chat" in o.get("capabilities", [])),
                    orgs[0],
                )
                org_id = org.get("uuid") or org.get("id")
                if not org_id:
                    browser.close()
                    return UsageData(
                        error="Could not find org ID — rerun with CLAUDE_WATCH_DEBUG=1",
                        last_updated=now,
                    )
                if DEBUG:
                    print(f"[claude-watch] org_id={org_id}", file=sys.stderr)

                resp = page.goto(
                    f"{CLAUDE_BASE_URL}/api/organizations/{org_id}/usage",
                    wait_until="domcontentloaded", timeout=20000,
                )
                usage_raw = page.evaluate("document.body.innerText")
                if DEBUG:
                    print(f"[claude-watch] /usage [{resp.status}]: {usage_raw}", file=sys.stderr)

                if resp.status != 200:
                    return UsageData(
                        error=f"Usage endpoint returned {resp.status}",
                        last_updated=now,
                    )

                try:
                    data = json.loads(usage_raw)
                except Exception:
                    return UsageData(
                        error="Could not parse usage response — rerun with CLAUDE_WATCH_DEBUG=1",
                        last_updated=now,
                    )

                fh = data.get("five_hour") or {}
                wd = data.get("seven_day") or {}

                extra_used = 0.0
                extra_currency = ""
                extra_enabled = False
                try:
                    resp2 = page.goto(
                        f"{CLAUDE_BASE_URL}/api/organizations/{org_id}/overage_spend_limit",
                        wait_until="domcontentloaded", timeout=15000,
                    )
                    if resp2.status == 200:
                        ov = json.loads(page.evaluate("document.body.innerText"))
                        if DEBUG:
                            print(f"[claude-watch] overage_spend_limit: {ov}", file=sys.stderr)
                        extra_enabled = bool(ov.get("is_enabled"))
                        extra_used = float(ov.get("used_credits") or 0) / 100
                        extra_currency = ov.get("currency") or ""
                except Exception as e:
                    if DEBUG:
                        print(f"[claude-watch] overage_spend_limit error: {e}", file=sys.stderr)

                prepaid_balance = 0.0
                prepaid_currency = ""
                try:
                    resp3 = page.goto(
                        f"{CLAUDE_BASE_URL}/api/organizations/{org_id}/prepaid/credits",
                        wait_until="domcontentloaded", timeout=15000,
                    )
                    if resp3.status == 200:
                        pc = json.loads(page.evaluate("document.body.innerText"))
                        if DEBUG:
                            print(f"[claude-watch] prepaid/credits: {pc}", file=sys.stderr)
                        prepaid_balance = float(pc.get("amount") or 0) / 100
                        prepaid_currency = pc.get("currency") or ""
                except Exception as e:
                    if DEBUG:
                        print(f"[claude-watch] prepaid/credits error: {e}", file=sys.stderr)

                browser.close()
                return UsageData(
                    pct_5h=float(fh.get("utilization") or 0),
                    reset_5h=_parse_dt(fh.get("resets_at")),
                    pct_week=float(wd.get("utilization") or 0),
                    reset_week=_parse_dt(wd.get("resets_at")),
                    extra_used=extra_used,
                    extra_currency=extra_currency,
                    extra_enabled=extra_enabled,
                    prepaid_balance=prepaid_balance,
                    prepaid_currency=prepaid_currency,
                    last_updated=now,
                )

        except Exception as e:
            if DEBUG:
                import traceback
                traceback.print_exc(file=sys.stderr)
            return UsageData(error=f"Error: {e}", last_updated=now)


def _parse_dt(val) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def fmt_duration(dt: Optional[datetime]) -> str:
    if dt is None:
        return "unknown"
    diff = dt - datetime.now(timezone.utc)
    if diff.total_seconds() <= 0:
        return "now"
    total_minutes = int(diff.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    days, hours = divmod(hours, 24)
    if days > 0:
        return f"in {days}d {hours}h"
    if hours > 0:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"


_CURRENCY_SYMBOLS = {
    "EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥", "CNY": "¥",
    "INR": "₹", "KRW": "₩", "BRL": "R$", "CAD": "CA$", "AUD": "A$",
    "SGD": "S$", "HKD": "HK$", "SEK": "kr", "NOK": "kr", "DKK": "kr",
    "CHF": "Fr",
}


def _currency_symbol(iso: str) -> str:
    return _CURRENCY_SYMBOLS.get((iso or "").upper(), iso or "")


def _parse_color(hex_color: str) -> tuple:
    try:
        h = hex_color.lstrip("#")
        if len(h) == 3:
            h = h[0]*2 + h[1]*2 + h[2]*2
        return int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255
    except Exception:
        return 1.0, 1.0, 1.0


def render_tray_icon(text: str, path: str, color: str = "#ffffff") -> None:
    font_desc = Pango.FontDescription("Monospace Bold 10")

    tmp = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    ctx = cairo.Context(tmp)
    layout = PangoCairo.create_layout(ctx)
    layout.set_font_description(font_desc)
    layout.set_text(text, -1)
    tw, th = layout.get_pixel_size()

    pad = 4
    w = tw + pad * 2
    h = max(th + pad * 2, 22)

    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    ctx = cairo.Context(surf)

    # Grayscale antialiasing ensures all pixels have R==G==B, preventing GNOME
    # from mapping subpixel colour fringing to theme accent colours (green tint).
    font_opts = cairo.FontOptions()
    font_opts.set_antialias(cairo.ANTIALIAS_GRAY)
    ctx.set_font_options(font_opts)

    ctx.set_source_rgba(0, 0, 0, 0)
    ctx.paint()

    r, g, b = _parse_color(color)
    layout = PangoCairo.create_layout(ctx)
    layout.set_font_description(font_desc)
    layout.set_text(text, -1)
    ctx.set_source_rgba(r, g, b, 1.0)
    ctx.move_to(pad, (h - th) / 2)
    PangoCairo.show_layout(ctx, layout)

    surf.write_to_png(path)


class SettingsDialog(Gtk.Dialog):
    def __init__(self, parent, config: Config):
        super().__init__(title="Claude Watch Settings", transient_for=parent, modal=True)
        self.config = config
        self.set_default_size(300, -1)
        self.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                         Gtk.STOCK_SAVE, Gtk.ResponseType.OK)

        box = self.get_content_area()
        box.set_spacing(6)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(18)
        box.set_margin_end(18)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        box.add(grid)

        grid.attach(Gtk.Label(label="Refresh interval (min):", halign=Gtk.Align.END), 0, 0, 1, 1)
        self.refresh_combo = Gtk.ComboBoxText()
        for opt in REFRESH_OPTIONS:
            self.refresh_combo.append_text(str(opt))
        cur = config.refresh_minutes if config.refresh_minutes in REFRESH_OPTIONS else 5
        self.refresh_combo.set_active(REFRESH_OPTIONS.index(cur))
        grid.attach(self.refresh_combo, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Icon color:", halign=Gtk.Align.END), 0, 1, 1, 1)
        self.color_btn = Gtk.ColorButton()
        rgba = Gdk.RGBA()
        rgba.parse(config.icon_color or "#ffffff")
        self.color_btn.set_rgba(rgba)
        grid.attach(self.color_btn, 1, 1, 1, 1)

        grid.attach(Gtk.Label(label="Show extra usage:", halign=Gtk.Align.END), 0, 2, 1, 1)
        self.extra_check = Gtk.CheckButton()
        self.extra_check.set_active(bool(config.show_extra_usage))
        self.extra_check.connect("toggled", self._on_extra_toggled)
        grid.attach(self.extra_check, 1, 2, 1, 1)

        grid.attach(Gtk.Label(label="Display as:", halign=Gtk.Align.END), 0, 3, 1, 1)
        self.extra_mode_combo = Gtk.ComboBoxText()
        self.extra_mode_combo.append_text("Amount spent")
        self.extra_mode_combo.append_text("Remaining balance")
        mode = config.extra_usage_mode or "spent"
        self.extra_mode_combo.set_active(0 if mode == "spent" else 1)
        self.extra_mode_combo.set_sensitive(bool(config.show_extra_usage))
        grid.attach(self.extra_mode_combo, 1, 3, 1, 1)

        self.show_all()

    def _on_extra_toggled(self, btn):
        self.extra_mode_combo.set_sensitive(btn.get_active())

    def get_values(self):
        rgba = self.color_btn.get_rgba()
        color = f"#{int(rgba.red*255):02x}{int(rgba.green*255):02x}{int(rgba.blue*255):02x}"
        return {
            "refresh_minutes": REFRESH_OPTIONS[self.refresh_combo.get_active()],
            "icon_color": color,
            "show_extra_usage": self.extra_check.get_active(),
            "extra_usage_mode": "spent" if self.extra_mode_combo.get_active() == 0 else "remaining",
        }


class ClaudeWatchIndicator:
    def __init__(self):
        self.config = Config()
        self.scraper = ClaudeAIScraper()
        self.usage: Optional[UsageData] = None
        self._refresh_timer_id: Optional[int] = None
        self._icon_slot = 0

        initial_path = _ICON_PATHS[self._icon_slot]
        render_tray_icon("...", initial_path, self.config.icon_color or "#ffffff")

        self.indicator = AppIndicator3.Indicator.new(
            "claude-watch",
            initial_path,
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

        self.menu = Gtk.Menu()
        self._build_menu()
        self.indicator.set_menu(self.menu)

        GLib.idle_add(self._refresh)
        self._schedule_refresh()

    def _build_menu(self):
        for child in self.menu.get_children():
            self.menu.remove(child)

        title = Gtk.MenuItem(label="Claude Watch")
        title.set_sensitive(False)
        self.menu.append(title)
        self.menu.append(Gtk.SeparatorMenuItem())

        u = self.usage

        if u is None:
            item = Gtk.MenuItem(label="Loading...")
            item.set_sensitive(False)
            self.menu.append(item)
        elif u.error:
            err = Gtk.MenuItem(label=u.error)
            err.set_sensitive(False)
            self.menu.append(err)
            open_item = Gtk.MenuItem(label="Open claude.ai in browser")
            open_item.connect("activate", self._on_open_browser)
            self.menu.append(open_item)
        else:
            item_5h = Gtk.MenuItem(label=f"Last 5 hours:   {u.pct_5h:.0f}%  —  resets {fmt_duration(u.reset_5h)}")
            item_5h.set_sensitive(False)
            self.menu.append(item_5h)

            item_week = Gtk.MenuItem(label=f"This week:      {u.pct_week:.0f}%  —  resets {fmt_duration(u.reset_week)}")
            item_week.set_sensitive(False)
            self.menu.append(item_week)

            if self.config.show_extra_usage and u.extra_enabled:
                if (self.config.extra_usage_mode or "spent") == "remaining":
                    sym = _currency_symbol(u.prepaid_currency or u.extra_currency)
                    label_ex = f"Extra usage: {sym}{u.prepaid_balance:.2f} remaining"
                else:
                    sym = _currency_symbol(u.extra_currency)
                    label_ex = f"Extra usage: {sym}{u.extra_used:.2f} spent"
                item_ex = Gtk.MenuItem(label=label_ex)
                item_ex.set_sensitive(False)
                self.menu.append(item_ex)

        self.menu.append(Gtk.SeparatorMenuItem())

        if u and u.last_updated:
            status_label = "Updated: " + u.last_updated.astimezone().strftime("%H:%M")
        else:
            status_label = "Not yet loaded"
        status_item = Gtk.MenuItem(label=status_label)
        status_item.set_sensitive(False)
        self.menu.append(status_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        refresh_item = Gtk.MenuItem(label="Refresh now")
        refresh_item.connect("activate", self._on_refresh_now)
        self.menu.append(refresh_item)

        settings_item = Gtk.MenuItem(label="Settings")
        settings_item.connect("activate", self._on_settings)
        self.menu.append(settings_item)

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", self._on_quit)
        self.menu.append(quit_item)

        self.menu.show_all()

    def _update_icon(self):
        u = self.usage
        if u is None:
            text = "..."
        elif u.error:
            text = "! Error"
        else:
            text = f"5h: {u.pct_5h:.0f}%  W: {u.pct_week:.0f}%"
            if self.config.show_extra_usage and u.extra_enabled:
                if (self.config.extra_usage_mode or "spent") == "remaining":
                    sym = _currency_symbol(u.prepaid_currency or u.extra_currency)
                    text += f"  E: {sym}{u.prepaid_balance:.2f}"
                else:
                    sym = _currency_symbol(u.extra_currency)
                    text += f"  E: {sym}{u.extra_used:.2f}"

        self._icon_slot = 1 - self._icon_slot
        path = _ICON_PATHS[self._icon_slot]
        render_tray_icon(text, path, self.config.icon_color or "#ffffff")
        self.indicator.set_icon_full(path, text)

    def _refresh(self):
        try:
            self.usage = self.scraper.fetch()
        except Exception as e:
            print(f"[claude-watch] Fetch error: {e}", file=sys.stderr)
            if self.usage is None:
                self.usage = UsageData(error=str(e), last_updated=datetime.now(timezone.utc))
        self._update_icon()
        self._build_menu()
        return False

    def _schedule_refresh(self):
        if self._refresh_timer_id is not None:
            GLib.source_remove(self._refresh_timer_id)
        interval_sec = (self.config.refresh_minutes or 5) * 60
        self._refresh_timer_id = GLib.timeout_add_seconds(interval_sec, self._on_timer)

    def _on_timer(self):
        self._refresh()
        self._schedule_refresh()
        return False

    def _on_refresh_now(self, _item):
        self._refresh()
        self._schedule_refresh()

    def _on_open_browser(self, _item):
        subprocess.Popen(["xdg-open", "https://claude.ai"])

    def _on_settings(self, _item):
        dialog = SettingsDialog(None, self.config)
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            values = dialog.get_values()
            for k, v in values.items():
                setattr(self.config, k, v)
            self.config.save()
            self._refresh()
            self._schedule_refresh()
        dialog.destroy()

    def _on_quit(self, _item):
        Gtk.main_quit()


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    _app = ClaudeWatchIndicator()
    Gtk.main()


if __name__ == "__main__":
    main()
