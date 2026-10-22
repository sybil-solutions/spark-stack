#!/usr/bin/python3
"""A desktop window around a local web UI (Spark Stack, the DeepSeek harness).

usage: webapp.py <app-id> <title> <icon-path> <url>|-      ("-": read the URL from stdin, for URLs carrying a token)

<app-id> is the window class: the dock matches it to <app-id>.desktop (StartupWMClass), so the pinned icon
lights up instead of a second, generic one. One window per app: launching again raises it. Cookies and local
storage persist under ~/.local/share/spark-stack/webapp/<app-id>. Links to other sites open in the browser.
Falls back to the default browser if GTK/WebKit is missing.
"""
import os
import sys
from urllib.parse import urlsplit

app_id, title, icon, url = sys.argv[1:5]
if url == "-":
    url = sys.stdin.readline().strip()
# WebKit's DMA-BUF renderer cannot allocate GBM buffers on the GB10 driver (blank window, "CREATE_DUMB failed").
os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")

try:
    import gi
    gi.require_version("Gdk", "3.0")
    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gdk, GLib
    # X11 WM_CLASS, matched by the dock; must be set before importing Gtk, which opens the display.
    GLib.set_prgname(app_id)
    GLib.set_application_name(title)
    Gdk.set_program_class(app_id)
    from gi.repository import Gio, Gtk, WebKit2
except (ImportError, ValueError):
    os.execvp("xdg-open", ["xdg-open", url])

ORIGIN = urlsplit(url)[:2]
DATA = os.path.expanduser(f"~/.local/share/spark-stack/webapp/{app_id}")
RETRY_PAGE = """<!doctype html><meta charset=utf-8><body style="font:15px sans-serif;margin:3em;color:#222">
<h2 style="font-weight:600">{title}</h2><p>Not answering at <code>{url}</code> yet; retrying every 3 seconds.</p>
<p style="color:#666">If this stays: open a terminal and run <code>sudo docker ps</code> (Spark Stack) or
<code>systemctl status spark-stack-dsh</code> (DeepSeek Harness).</p>"""


def external(uri: str) -> bool:
    return urlsplit(uri)[:2] != ORIGIN and not uri.startswith(("about:", "data:"))


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="ai.sybilsolutions." + app_id.replace("-", "_"),
                         flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.window = None

    def do_activate(self):
        if self.window:
            self.window.present()
            return
        ctx = WebKit2.WebContext.new_with_website_data_manager(
            WebKit2.WebsiteDataManager(base_data_directory=DATA, base_cache_directory=DATA + "/cache"))
        ctx.get_cookie_manager().set_persistent_storage(DATA + "/cookies.sqlite",
                                                       WebKit2.CookiePersistentStorage.SQLITE)
        view = WebKit2.WebView.new_with_context(ctx)
        view.connect("decide-policy", self.on_policy)
        view.connect("create", self.on_create)
        view.connect("load-failed", self.on_failed)
        win = Gtk.ApplicationWindow(application=self, title=title)
        win.set_default_size(1280, 860)
        win.set_wmclass(app_id, app_id)   # explicit WM_CLASS; the .desktop file's StartupWMClass matches it
        if os.path.exists(icon):
            win.set_icon_from_file(icon)
        win.add(view)
        win.show_all()
        view.load_uri(url)
        self.window = win

    def on_policy(self, view, decision, kind):
        if kind == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if external(uri):
                Gio.AppInfo.launch_default_for_uri(uri, None)
                decision.ignore()
                return True
        return False

    def on_create(self, view, action):
        # target=_blank / window.open: same app → same window, other sites → browser
        uri = action.get_request().get_uri()
        if external(uri):
            Gio.AppInfo.launch_default_for_uri(uri, None)
        else:
            view.load_uri(uri)
        return None

    def on_failed(self, view, event, failing_uri, error):
        if failing_uri.startswith(url.split("?")[0]) and not error.matches(
                WebKit2.network_error_quark(), WebKit2.NetworkError.CANCELLED):
            view.load_alternate_html(RETRY_PAGE.format(title=title, url=url.split("?")[0]), failing_uri, None)
            GLib.timeout_add_seconds(3, lambda: view.load_uri(url) and False)
            return True
        return False


if __name__ == "__main__":
    sys.exit(App().run([app_id]))
