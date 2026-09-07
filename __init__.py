"""
Study Bins
==========

Group decks/subdecks into named "bins" you can switch between, optionally
"Focus" the deck browser so it only shows the active bin's decks, and
pull up just that bin's due cards — each deck kept separate, no new
cards, no daily cap — without touching anything about the real decks or
their scheduling.

Menu: Tools > Study Bins
Shortcuts: Ctrl+Shift+B (anywhere), Ctrl+Shift+F / Left / Right / D
(deck browser only) — see the reference in the bin manager.
"""

import os
import re
from typing import Optional

from anki.decks import DeckId
from anki.decks_pb2 import Deck as DeckPb
from aqt import gui_hooks, mw
from aqt.deckbrowser import DeckBrowser, DeckBrowserContent
from aqt.qt import *
from aqt.utils import getText, showInfo, showWarning, tooltip

CONFIG_DEFAULTS = {
    "bins": {},
    "active_bin": None,
    "due_mode": False,
    "last_focused_bin": None,
}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def get_config() -> dict:
    conf = mw.addonManager.getConfig(__name__)
    if conf is None:
        conf = dict(CONFIG_DEFAULTS)
    conf.setdefault("bins", {})
    conf.setdefault("active_bin", None)
    conf.setdefault("due_mode", False)
    conf.setdefault("last_focused_bin", None)
    return conf


def save_config(conf: dict) -> None:
    mw.addonManager.writeConfig(__name__, conf)


def focus_bin(conf: dict, name: str) -> None:
    """The one place 'what's focused' changes — keeps active_bin and
    last_focused_bin (used by the Ctrl+Shift+F toggle) in lockstep."""
    conf["active_bin"] = name
    conf["last_focused_bin"] = name
    conf["due_mode"] = False


def clear_focus(conf: dict) -> None:
    """Un-focus, but deliberately leave last_focused_bin alone — that's
    what Ctrl+Shift+F restores when you toggle focus back on."""
    conf["active_bin"] = None
    conf["due_mode"] = False


# ---------------------------------------------------------------------------
# Deck id resolution
#
# A bin only ever stores the deck ids you explicitly checked. Everything
# else (which subdecks that implies, which parent rows need to stay
# visible so the tree doesn't look orphaned) is derived fresh every time
# from the collection, so editing a bin later — checking or unchecking
# any deck — is always safe and immediate.
# ---------------------------------------------------------------------------

def deck_name(col, did: int) -> Optional[str]:
    deck = col.decks.get(did, default=False)
    return deck["name"] if deck else None


def descendant_ids(col, did: int) -> set:
    """did plus every subdeck underneath it."""
    try:
        return set(col.decks.deck_and_child_ids(DeckId(did)))
    except AttributeError:
        # Older/newer API shapes: fall back to a name-prefix scan.
        name = deck_name(col, did)
        if not name:
            return {did}
        ids = {did}
        for name_id in col.decks.all_names_and_ids():
            if name_id.name.startswith(name + "::"):
                ids.add(name_id.id)
        return ids


def ancestor_ids(col, did: int) -> set:
    """Every parent deck above did, derived from the '::' naming convention."""
    name = deck_name(col, did)
    if not name or "::" not in name:
        return set()
    parts = name.split("::")
    ids = set()
    for i in range(1, len(parts)):
        parent = col.decks.by_name("::".join(parts[:i]))
        if parent:
            ids.add(parent["id"])
    return ids


def bin_keep_ids(col, deck_ids: list) -> set:
    """Everything that should stay visible in normal Focus Mode: the
    decks you picked, their subdecks, and their ancestor decks (so a
    picked subdeck doesn't show up looking orphaned under a hidden
    parent row)."""
    keep = set(deck_ids)
    for did in deck_ids:
        keep |= descendant_ids(col, did)
        keep |= ancestor_ids(col, did)
    return keep


def bin_study_ids(col, deck_ids: list) -> set:
    """Every deck that should get its own due-only companion: the decks
    you picked plus their subdecks. Deliberately excludes ancestors —
    unlike Focus Mode's tree display, due-mode companions are always
    flat (never nested), so there's no 'orphaned subdeck' concern to
    protect against, and an ancestor's companion is almost always just
    an empty, unhelpful entry (parent decks rarely hold cards directly)."""
    keep = set(deck_ids)
    for did in deck_ids:
        keep |= descendant_ids(col, did)
    return keep


# ---------------------------------------------------------------------------
# Due-cards companion decks — one per original deck, not one merged deck
# for the whole bin
#
# Ctrl+Shift+D (or the banner link) builds a real Anki filtered deck for
# EVERY deck the bin actually targets for study — each pulling in only
# that one deck's own due cards (subdecks explicitly excluded via the
# "-deck:X::*" wildcard, since the subdeck gets its own separate
# companion) — no new cards (they don't match "is:due"), no daily cap.
# Reschedule stays ON, so answering these is real study; cards return to
# their normal decks once a companion is emptied or removed.
#
# A companion's identity is its NAME, derived deterministically from its
# source deck's name — nothing is tracked in config. That means: no
# stale ids to clean up, reusing the same source deck from two different
# bins naturally reuses the same companion, and a bin rename doesn't
# orphan anything (companions are torn down before you'd ever notice).
#
# Verified end-to-end against a real Anki collection while building
# this: get_or_create_filtered_deck(0) hands back a template pre-loaded
# with Anki's own default search terms, which must be cleared before
# adding this one — and allow_empty must be set, or saving fails
# outright on a day nothing happens to be due for that specific deck.
# ---------------------------------------------------------------------------

def due_deck_name_for(source_deck_name: str) -> str:
    # Anki treats "::" as a hierarchy separator no matter where it shows
    # up in a deck name — a subdeck's own name always contains it, so
    # naming the companion "🔖 Radiology::Physics — Due" silently nests
    # it under an auto-created, empty "🔖 Radiology" placeholder deck
    # instead of creating one flat deck. This was the actual cause of
    # companions showing up empty: the visible row was that auto-created
    # placeholder, not the real (correctly populated) companion nested
    # underneath it. Swap "::" for a plain separator that still reads as
    # a path but can't be parsed as one.
    safe_name = source_deck_name.replace("::", " \u203a ")
    return f"\U0001F516 {safe_name} \u2014 Due"


def per_deck_due_search(source_deck_name: str) -> str:
    # Excludes subdecks — they get their own separate companion, so this
    # avoids double-counting the same card in two different companions.
    return f'deck:"{source_deck_name}" -deck:"{source_deck_name}::*" is:due'


def build_due_deck_for(col, source_deck_name: str) -> int:
    companion_name = due_deck_name_for(source_deck_name)
    existing_id = col.decks.id_for_name(companion_name)

    fd = col.sched.get_or_create_filtered_deck(DeckId(existing_id or 0))
    fd.name = companion_name
    del fd.config.search_terms[:]
    term = fd.config.search_terms.add()
    term.search = per_deck_due_search(source_deck_name)
    term.limit = 99999  # the whole backlog, not just today's normal cap
    term.order = DeckPb.Filtered.SearchTerm.Order.DUE
    fd.config.reschedule = True
    fd.allow_empty = True

    result = col.sched.add_or_update_filtered_deck(fd)
    col.sched.rebuild_filtered_deck(DeckId(result.id))
    return result.id


def build_due_decks_for_bin(col, bin_record: dict) -> dict:
    """Builds (or refreshes) one companion per deck this bin targets for
    study. Returns {source_deck_id: companion_deck_id}."""
    mapping = {}
    for did in bin_study_ids(col, bin_record["deck_ids"]):
        name = deck_name(col, did)
        if name:
            mapping[did] = build_due_deck_for(col, name)
    return mapping


def discard_due_decks_for_bin(col, bin_record: dict) -> None:
    """Empties and removes every companion for this bin's decks, if any
    exist — returns any lingering cards to their real decks first.
    Safe to call even when no companions currently exist."""
    for did in bin_study_ids(col, bin_record["deck_ids"]):
        name = deck_name(col, did)
        if not name:
            continue
        due_id = col.decks.id_for_name(due_deck_name_for(name))
        if not due_id:
            continue
        try:
            col.sched.empty_filtered_deck(DeckId(due_id))
            col.decks.remove([DeckId(due_id)])
        except Exception:
            pass  # best-effort cleanup only


def due_companion_ids_for_bin(col, bin_record: dict) -> set:
    """Which companion deck ids currently exist for this bin — used by
    Focus Mode to know which rows to keep visible in due-only mode."""
    ids = set()
    for did in bin_study_ids(col, bin_record["deck_ids"]):
        name = deck_name(col, did)
        if not name:
            continue
        due_id = col.decks.id_for_name(due_deck_name_for(name))
        if due_id:
            ids.add(due_id)
    return ids


# ---------------------------------------------------------------------------
# Centralized focus/due-mode state transitions
#
# Every place that changes what's focused or toggles due mode funnels
# through these few functions, so a companion set is never left orphaned
# mid-collection — switching bins, clearing focus, or re-entering due
# mode always tears down whatever due-mode companions existed first.
# ---------------------------------------------------------------------------

def _teardown_due_mode(conf: dict) -> None:
    if not conf.get("due_mode"):
        return
    active = conf.get("active_bin")
    if active and active in conf["bins"]:
        discard_due_decks_for_bin(mw.col, conf["bins"][active])
    conf["due_mode"] = False


def set_focus(conf: dict, name: str) -> None:
    _teardown_due_mode(conf)
    focus_bin(conf, name)


def set_no_focus(conf: dict) -> None:
    _teardown_due_mode(conf)
    clear_focus(conf)


def enable_due_mode(conf: dict, name: str) -> bool:
    """Focuses `name` and turns on due mode for it. Returns False (and
    shows a warning) if building the companion decks failed."""
    if conf.get("active_bin") == name and conf.get("due_mode"):
        return True  # already there
    _teardown_due_mode(conf)
    focus_bin(conf, name)
    try:
        build_due_decks_for_bin(mw.col, conf["bins"][name])
    except Exception as e:
        showWarning(f"Couldn't build due-cards decks for \u201c{name}\u201d:\n{e}")
        return False
    conf["due_mode"] = True
    return True


def disable_due_mode(conf: dict) -> None:
    _teardown_due_mode(conf)


# ---------------------------------------------------------------------------
# Deck picker — a checkbox tree over every deck in the collection.
#
# This same dialog is used both to create a bin and to edit one later:
# pass in the bin's current deck ids, check or uncheck anything, save.
# That's the whole mechanism for adding/removing decks from a bin at any
# time — there's no separate "add" vs "remove" flow to keep in sync.
# ---------------------------------------------------------------------------

def _current_due_companion_names(conf: dict) -> set:
    """Companion deck names that exist right now (at most one bin's
    worth, since due mode is always torn down before another begins) —
    kept out of the deck picker, since binning a companion makes no
    sense."""
    if not conf.get("due_mode"):
        return set()
    active = conf.get("active_bin")
    if not active or active not in conf["bins"]:
        return set()
    names = set()
    for did in bin_study_ids(mw.col, conf["bins"][active]["deck_ids"]):
        name = deck_name(mw.col, did)
        if name:
            names.add(due_deck_name_for(name))
    return names


class DeckPickerDialog(QDialog):
    def __init__(self, parent, checked_ids: Optional[set] = None):
        super().__init__(parent)
        self.setWindowTitle("Choose decks for this bin")
        self.resize(420, 520)
        checked_ids = checked_ids or set()

        layout = QVBoxLayout(self)
        info = QLabel(
            "Check every deck or subdeck you want in this bin. "
            "Checking a parent deck's subdecks individually is fine too — "
            "pick exactly what you plan to study."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        layout.addWidget(self.tree)

        self._items_by_did: dict = {}
        self._build_tree(checked_ids)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _build_tree(self, checked_ids: set) -> None:
        due_names = _current_due_companion_names(get_config())

        nodes: dict = {}  # "A::B" -> QTreeWidgetItem

        def get_or_create(parts: list):
            path = "::".join(parts)
            if path in nodes:
                return nodes[path]
            if len(parts) == 1:
                item = QTreeWidgetItem([parts[-1]])
                self.tree.addTopLevelItem(item)
            else:
                parent_item = get_or_create(parts[:-1])
                item = QTreeWidgetItem(parent_item, [parts[-1]])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked)
            nodes[path] = item
            return item

        names_ids = sorted(mw.col.decks.all_names_and_ids(), key=lambda n: n.name)
        for name_id in names_ids:
            if name_id.name in due_names:
                continue
            item = get_or_create(name_id.name.split("::"))
            item.setData(0, Qt.ItemDataRole.UserRole, name_id.id)
            self._items_by_did[name_id.id] = item
            item.setCheckState(
                0,
                Qt.CheckState.Checked
                if name_id.id in checked_ids
                else Qt.CheckState.Unchecked,
            )

        self.tree.expandAll()

    def checked_deck_ids(self) -> list:
        return [
            did
            for did, item in self._items_by_did.items()
            if item.checkState(0) == Qt.CheckState.Checked
        ]


# ---------------------------------------------------------------------------
# Bin manager — create, edit, rename, delete bins; set which one (if any)
# is currently focused; jump into due-only mode; a plain, always-visible
# shortcut reference (no extra popup) at the bottom.
# ---------------------------------------------------------------------------

SHORTCUTS_HTML = (
    "Ctrl+Shift+B \u2014 open this manager (works anywhere)<br>"
    "Ctrl+Shift+F \u2014 toggle focus, remembering your last bin<br>"
    "Ctrl+Shift+\u2190 / Ctrl+Shift+\u2192 \u2014 cycle through bins<br>"
    "Ctrl+Shift+D \u2014 toggle due-cards-only for the focused bin"
    "<br><br><i>F, \u2190/\u2192, and D only work from the deck browser; "
    "B works anywhere in Anki.</i>"
)


class BinManagerDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Study Bins")
        self.resize(400, 520)
        self.conf = get_config()

        layout = QVBoxLayout(self)
        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)
        self._refresh_list()

        edit_row = QHBoxLayout()
        new_btn = QPushButton("New Bin\u2026")
        edit_btn = QPushButton("Edit Decks\u2026")
        rename_btn = QPushButton("Rename\u2026")
        delete_btn = QPushButton("Delete")
        for b in (new_btn, edit_btn, rename_btn, delete_btn):
            edit_row.addWidget(b)
        layout.addLayout(edit_row)

        new_btn.clicked.connect(self.new_bin)
        edit_btn.clicked.connect(self.edit_bin)
        rename_btn.clicked.connect(self.rename_bin)
        delete_btn.clicked.connect(self.delete_bin)

        focus_row = QHBoxLayout()
        focus_btn = QPushButton("Focus Selected")
        due_btn = QPushButton("Due Cards Only")
        clear_btn = QPushButton("Show All Decks")
        focus_row.addWidget(focus_btn)
        focus_row.addWidget(due_btn)
        focus_row.addWidget(clear_btn)
        layout.addLayout(focus_row)

        focus_btn.clicked.connect(self.focus_selected)
        due_btn.clicked.connect(self.due_selected)
        clear_btn.clicked.connect(self.clear_focus_clicked)

        # A bordered, titled group box rather than a plain label — this is
        # what was actually missing before: a QLabel styled with
        # "color: palette(mid)" for a muted look, which on several Anki
        # themes renders low-contrast enough to be effectively invisible.
        # A QGroupBox uses the theme's normal border/title rendering, so
        # it can't blend into the background the same way.
        shortcuts_box = QGroupBox("Shortcuts")
        shortcuts_box_layout = QVBoxLayout(shortcuts_box)
        shortcuts_label = QLabel(SHORTCUTS_HTML)
        shortcuts_label.setWordWrap(True)
        shortcuts_box_layout.addWidget(shortcuts_label)
        layout.addWidget(shortcuts_box)

        close_btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_btns.rejected.connect(self.close)
        layout.addWidget(close_btns)

    def _refresh_list(self) -> None:
        self.list_widget.clear()
        active = self.conf.get("active_bin")
        due_mode = self.conf.get("due_mode")
        for name in sorted(self.conf["bins"].keys()):
            if name == active and due_mode:
                label = f"\U0001F516 {name} (due only)"
            elif name == active:
                label = f"\U0001F516 {name}"
            else:
                label = name
            self.list_widget.addItem(label)

    def _selected_name(self) -> Optional[str]:
        item = self.list_widget.currentItem()
        if not item:
            return None
        text = item.text()
        if text.startswith("\U0001F516 "):
            text = text[2:]
        if text.endswith(" (due only)"):
            text = text[: -len(" (due only)")]
        return text

    def new_bin(self) -> None:
        name, ok = getText("Name this bin:", parent=self)
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in self.conf["bins"]:
            showWarning("A bin with that name already exists.")
            return
        dlg = DeckPickerDialog(self)
        if dlg.exec():
            self.conf["bins"][name] = {"deck_ids": dlg.checked_deck_ids()}
            save_config(self.conf)
            self._refresh_list()

    def edit_bin(self) -> None:
        name = self._selected_name()
        if not name:
            return
        current_ids = set(self.conf["bins"][name]["deck_ids"])
        dlg = DeckPickerDialog(self, checked_ids=current_ids)
        if dlg.exec():
            # Editing which decks are in the bin can change what a due
            # companion should even mean, so drop out of due mode rather
            # than risk it referring to a stale deck selection.
            if self.conf.get("active_bin") == name and self.conf.get("due_mode"):
                _teardown_due_mode(self.conf)
            self.conf["bins"][name]["deck_ids"] = dlg.checked_deck_ids()
            save_config(self.conf)
            if self.conf.get("active_bin") == name:
                mw.deckBrowser.refresh()
            tooltip(f"Updated \u201c{name}\u201d")

    def rename_bin(self) -> None:
        name = self._selected_name()
        if not name:
            return
        new_name, ok = getText("New name:", parent=self, default=name)
        if not ok or not new_name.strip() or new_name.strip() == name:
            return
        new_name = new_name.strip()
        if new_name in self.conf["bins"]:
            showWarning("A bin with that name already exists.")
            return
        self.conf["bins"][new_name] = self.conf["bins"].pop(name)
        if self.conf.get("active_bin") == name:
            self.conf["active_bin"] = new_name
        if self.conf.get("last_focused_bin") == name:
            self.conf["last_focused_bin"] = new_name
        save_config(self.conf)
        self._refresh_list()
        tooltip(f"Renamed to \u201c{new_name}\u201d")

    def delete_bin(self) -> None:
        name = self._selected_name()
        if not name:
            return
        discard_due_decks_for_bin(mw.col, self.conf["bins"][name])
        del self.conf["bins"][name]
        if self.conf.get("active_bin") == name:
            self.conf["active_bin"] = None
            self.conf["due_mode"] = False
            mw.deckBrowser.refresh()
        if self.conf.get("last_focused_bin") == name:
            self.conf["last_focused_bin"] = None
        save_config(self.conf)
        self._refresh_list()

    def focus_selected(self) -> None:
        name = self._selected_name()
        if not name:
            return
        set_focus(self.conf, name)
        save_config(self.conf)
        self._refresh_list()
        mw.deckBrowser.refresh()
        tooltip(f"Focused on \u201c{name}\u201d")

    def due_selected(self) -> None:
        name = self._selected_name()
        if not name:
            return
        if not enable_due_mode(self.conf, name):
            return
        save_config(self.conf)
        self._refresh_list()
        mw.deckBrowser.refresh()
        tooltip(f"Due cards only \u2014 \u201c{name}\u201d")

    def clear_focus_clicked(self) -> None:
        set_no_focus(self.conf)
        save_config(self.conf)
        self._refresh_list()
        mw.deckBrowser.refresh()
        tooltip("Showing all decks")


# ---------------------------------------------------------------------------
# Focus Mode — the deck-browser render hook
#
# Anki renders each deck-browser row as (verified against the installed
# aqt/deckbrowser.py source, not just assumed):
#     <tr class='deck' id='<did>' onclick=...>...</tr>
# (or class='deck current' for whichever deck is currently open). It's
# still undocumented, private markup — the one thing here that could
# shift in a future Anki release. If rows ever stop being hidden, use
# Tools > Study Bins > Debug: Dump Deck Browser HTML to see the current
# markup and adjust ROW_RE below to match — nothing else in this add-on
# depends on it, so nothing else would break.
# ---------------------------------------------------------------------------

ROW_RE = re.compile(r"<tr class='deck[^']*' id='(\d+)'.*?</tr>", re.DOTALL)

_last_tree_html = ""  # kept only for the debug dump helper


def _filter_tree_html(html: str, keep_ids: set) -> str:
    def repl(match: re.Match) -> str:
        did = int(match.group(1))
        return match.group(0) if did in keep_ids else ""

    return ROW_RE.sub(repl, html)


def _render_banner(title: str, hidden: int, due_mode: bool) -> str:
    link_label = "Show all decks" if due_mode else "Due cards only"
    link = f"<a href=\"#\" onclick=\"return pycmd('studybins:due')\">{link_label}</a>"
    hidden_part = f" \u2014 {hidden} other deck(s) hidden" if hidden else ""
    return (
        '<div style="padding:6px 12px 10px;opacity:0.75;font-size:12px;">'
        f"\U0001F516 Focus: <b>{title}</b>{hidden_part}"
        f"  \u00b7  {link}"
        "  \u00b7  Tools \u2192 Study Bins"
        "</div>"
    )


def on_deck_browser_will_render_content(
    deck_browser: DeckBrowser, content: DeckBrowserContent
) -> None:
    global _last_tree_html
    _last_tree_html = content.tree

    conf = get_config()
    active = conf.get("active_bin")
    if not active or active not in conf["bins"]:
        return

    bin_record = conf["bins"][active]
    due_mode = conf.get("due_mode")
    if due_mode:
        keep_ids = due_companion_ids_for_bin(mw.col, bin_record)
        title = f"Due cards \u2014 {active}"
    else:
        deck_ids = bin_record["deck_ids"]
        if not deck_ids:
            return
        keep_ids = bin_keep_ids(mw.col, deck_ids)
        title = active

    before = len(ROW_RE.findall(content.tree))
    content.tree = _filter_tree_html(content.tree, keep_ids)
    after = len(ROW_RE.findall(content.tree))
    hidden = before - after

    content.tree = _render_banner(title, hidden, due_mode) + content.tree


gui_hooks.deck_browser_will_render_content.append(on_deck_browser_will_render_content)


def debug_dump_deck_browser_html() -> None:
    path = os.path.join(os.path.dirname(__file__), "last_deck_tree_dump.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_last_tree_html)
    showInfo(
        "Dumped the current deck-browser HTML to:\n\n"
        f"{path}\n\n"
        "Check that each deck row still looks like "
        "<tr class='deck' id='...'>. If Anki has changed it, update "
        "ROW_RE near the top of __init__.py to match."
    )


def debug_due_companion_counts() -> None:
    """Diagnostic for 'the due deck shows up but has no cards': reports,
    per source deck, how many cards its companion actually holds and how
    many the scheduler will actually queue up to study."""
    conf = get_config()
    active = conf.get("active_bin")
    if not active or active not in conf["bins"] or not conf.get("due_mode"):
        showInfo("Turn on Due Cards Only for a bin first, then run this again.")
        return

    col = mw.col
    lines = []
    for did in sorted(bin_study_ids(col, conf["bins"][active]["deck_ids"])):
        name = deck_name(col, did)
        if not name:
            continue
        companion_name = due_deck_name_for(name)
        due_id = col.decks.id_for_name(companion_name)
        if not due_id:
            lines.append(f"\u2022 {name}: no companion deck exists")
            continue
        card_count = len(col.find_cards(f'deck:"{companion_name}"'))
        col.decks.select(DeckId(due_id))
        queued = col.sched.get_queued_cards(fetch_limit=9999)
        lines.append(
            f"\u2022 {name}: {card_count} card(s) assigned to the companion, "
            f"{len(queued.cards)} queued to study right now "
            f"(new={queued.new_count} learning={queued.learning_count} "
            f"review={queued.review_count})"
        )
    showInfo(
        "\n".join(lines) or "No decks found for this bin.",
        title="Due Companion Debug",
    )


# ---------------------------------------------------------------------------
# The banner's "Due cards only" / "Show all decks" link
#
# This wraps DeckBrowser._linkHandler (also private, also verified
# against the installed source) to catch our own "studybins:due" bridge
# command and pass everything else straight through to Anki's normal
# handler. If a future Anki version renames or restructures this method,
# the worst case is the banner link stops doing anything — Ctrl+Shift+D
# is a completely independent code path and keeps working regardless.
# ---------------------------------------------------------------------------

_original_link_handler = DeckBrowser._linkHandler


def _patched_link_handler(self, url):
    if url == "studybins:due":
        toggle_due_mode()
        return False
    return _original_link_handler(self, url)


DeckBrowser._linkHandler = _patched_link_handler


# ---------------------------------------------------------------------------
# Shared actions — used by the menu, the dialog, the banner link, and the
# keyboard shortcuts alike, so all four stay in sync automatically.
# ---------------------------------------------------------------------------

def toggle_due_mode() -> None:
    conf = get_config()
    active = conf.get("active_bin")
    if not active or active not in conf["bins"]:
        tooltip("Focus a bin first")
        return

    if conf.get("due_mode"):
        disable_due_mode(conf)
        save_config(conf)
        mw.deckBrowser.refresh()
        tooltip(f"Back to \u201c{active}\u201d")
    else:
        if not enable_due_mode(conf, active):
            return
        save_config(conf)
        mw.deckBrowser.refresh()
        tooltip(f"Due cards only \u2014 \u201c{active}\u201d")


def quick_focus(name: str) -> None:
    conf = get_config()
    set_focus(conf, name)
    save_config(conf)
    mw.deckBrowser.refresh()
    tooltip(f"Focused on \u201c{name}\u201d")


def quick_clear() -> None:
    conf = get_config()
    set_no_focus(conf)
    save_config(conf)
    mw.deckBrowser.refresh()
    tooltip("Showing all decks")


def open_bin_manager() -> None:
    BinManagerDialog(mw).exec()


# ---------------------------------------------------------------------------
# Keyboard shortcuts
#
# Ctrl+Shift+B is bound globally (works no matter which of Anki's states
# the main window is in). The other three only make sense at the deck
# browser, so each starts with a state check rather than trying to
# register scoped shortcuts through Anki's per-state shortcut hook —
# that hook (state_shortcuts_will_change) turns out to only ever fire
# for the overview and review states, not the deck browser, when checked
# against the installed source, so it wouldn't have fired for these at
# all.
# ---------------------------------------------------------------------------

def _in_deck_browser() -> bool:
    return mw.state == "deckBrowser"


def _shortcut_toggle_focus() -> None:
    if not _in_deck_browser():
        return
    conf = get_config()
    if conf.get("active_bin"):
        set_no_focus(conf)
        save_config(conf)
        mw.deckBrowser.refresh()
        tooltip("Showing all decks")
    else:
        target = conf.get("last_focused_bin")
        if not target or target not in conf["bins"]:
            tooltip("No bin to focus yet \u2014 Ctrl+Shift+B to create one")
            return
        set_focus(conf, target)
        save_config(conf)
        mw.deckBrowser.refresh()
        tooltip(f"Focused on \u201c{target}\u201d")


def _shortcut_cycle(direction: int) -> None:
    if not _in_deck_browser():
        return
    conf = get_config()
    names = sorted(conf["bins"].keys())
    if not names:
        tooltip("No bins yet \u2014 Ctrl+Shift+B to create one")
        return
    current = conf.get("active_bin")
    if current in names:
        idx = (names.index(current) + direction) % len(names)
    else:
        idx = 0 if direction > 0 else -1
    set_focus(conf, names[idx])
    save_config(conf)
    mw.deckBrowser.refresh()
    tooltip(f"Focused on \u201c{names[idx]}\u201d")


def _shortcut_toggle_due() -> None:
    if not _in_deck_browser():
        return
    toggle_due_mode()


def _install_shortcuts() -> None:
    QShortcut(QKeySequence("Ctrl+Shift+B"), mw, activated=open_bin_manager)
    QShortcut(QKeySequence("Ctrl+Shift+F"), mw, activated=_shortcut_toggle_focus)
    QShortcut(QKeySequence("Ctrl+Shift+Right"), mw, activated=lambda: _shortcut_cycle(1))
    QShortcut(QKeySequence("Ctrl+Shift+Left"), mw, activated=lambda: _shortcut_cycle(-1))
    QShortcut(QKeySequence("Ctrl+Shift+D"), mw, activated=_shortcut_toggle_due)


gui_hooks.main_window_did_init.append(_install_shortcuts)


# ---------------------------------------------------------------------------
# Menu integration — rebuilt every time Tools is opened, so newly
# created/renamed/deleted bins always show up without a restart.
# ---------------------------------------------------------------------------

_study_bins_menu: Optional[QMenu] = None


def _rebuild_menu() -> None:
    global _study_bins_menu
    if _study_bins_menu is not None:
        mw.form.menuTools.removeAction(_study_bins_menu.menuAction())
        _study_bins_menu.deleteLater()

    menu = QMenu("Study Bins", mw)

    manage_action = QAction("Manage Bins\u2026", mw)
    manage_action.triggered.connect(open_bin_manager)
    menu.addAction(manage_action)
    menu.addSeparator()

    conf = get_config()
    active = conf.get("active_bin")
    due_mode = conf.get("due_mode")
    for name in sorted(conf["bins"].keys()):
        action = QAction(name, mw)
        action.setCheckable(True)
        action.setChecked(name == active and not due_mode)
        action.triggered.connect(lambda _checked, n=name: quick_focus(n))
        menu.addAction(action)

    if conf["bins"]:
        menu.addSeparator()

    due_action = QAction("Due Cards Only (Focused Bin)", mw)
    due_action.setCheckable(True)
    due_action.setChecked(bool(due_mode))
    due_action.triggered.connect(toggle_due_mode)
    menu.addAction(due_action)

    clear_action = QAction("Show All Decks", mw)
    clear_action.triggered.connect(quick_clear)
    menu.addAction(clear_action)

    menu.addSeparator()
    debug_action = QAction("Debug: Dump Deck Browser HTML", mw)
    debug_action.triggered.connect(debug_dump_deck_browser_html)
    menu.addAction(debug_action)

    debug_due_action = QAction("Debug: Due Companion Card Counts", mw)
    debug_due_action.triggered.connect(debug_due_companion_counts)
    menu.addAction(debug_due_action)

    mw.form.menuTools.addMenu(menu)
    _study_bins_menu = menu


def _init_menu() -> None:
    mw.form.menuTools.aboutToShow.connect(_rebuild_menu)
    _rebuild_menu()


gui_hooks.main_window_did_init.append(_init_menu)
