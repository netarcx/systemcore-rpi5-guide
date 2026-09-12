"""Pure text transforms for the minified React dashboard bundle.

Everything here operates on the bundle's text and nothing else — no mounting,
no debugfs, no file I/O — so the Linux patcher (`patcher.core`) and the
Windows patcher (`patcher_win.core`) can share one implementation and stay
behaviourally identical.  Both also use it from `validate()` to check that the
patches actually landed.

The bundle is re-minified on every upstream build, so local variable names and
the react/jsx-runtime alias change between releases.  Nothing here may hardcode
a minified identifier: each one is read back out of the surrounding code.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional


# --- WLAN0 AP unlock -------------------------------------------------------

# Head of the network-config renderer.  Every disabled-when-wlan0 field lives
# inside this one function.
WLAN_FUNC_RE = r"renderInterfaceConfig=function\((\w+)\)\{var "

# How much of the function head to scan for the local variable declarations.
WLAN_HEAD_WINDOW = 600

# Fallback span length when the end of the function can't be located.
WLAN_SPAN_LIMIT = 20000


@dataclass
class WlanLockSite:
    """Where the WLAN0 AP lock lives in a particular bundle.

    `busy` is the local bound to `<this>.state.waiting||...` (true while the
    form is saving) and `wlan` the local bound to `"wlan0"===<param>`.  The
    lock is `disabled:<busy>||<wlan>`; the eth0 lock in the same function is
    `disabled:<busy>||<eth0>` and must be left alone, which is why the wlan
    local has to be derived rather than guessed.
    """

    start: int          # first char after the function head
    end: int            # end of the function's span
    busy: str
    wlan: str
    self_alias: str     # the minified `this` alias (`r` in Beta 10-14)

    @property
    def pattern(self) -> str:
        return rf"disabled:{re.escape(self.busy)}\|\|{re.escape(self.wlan)}"

    @property
    def replacement(self) -> str:
        return f"disabled:{self.busy}"


def find_wlan_lock(text: str) -> Optional[WlanLockSite]:
    """Locate `renderInterfaceConfig` and derive its minified locals.

    Returns None if the function or either local can't be found — the caller
    decides whether that's a warning (patching) or a failure (validating).
    """
    m = re.search(WLAN_FUNC_RE, text)
    if not m:
        return None
    param = m.group(1)
    start = m.end()
    head = text[start:start + WLAN_HEAD_WINDOW]

    wlan_m = re.search(rf'(\w+)="wlan0"==={re.escape(param)}', head)
    busy_m = re.search(r"(\w+)=(\w+)\.state\.waiting\|\|", head)
    if not wlan_m or not busy_m:
        return None
    self_alias = busy_m.group(2)

    # The function ends where the next method is assigned onto the same
    # object (`,r.getApiUrl=function` in Beta 10-14).  Bounding the span
    # keeps the substitution from touching identically-named locals in
    # unrelated components.
    end_m = re.search(rf",{re.escape(self_alias)}\.\w+=function", text[start:])
    end = start + end_m.start() if end_m else min(len(text), start + WLAN_SPAN_LIMIT)

    return WlanLockSite(
        start=start,
        end=end,
        busy=busy_m.group(1),
        wlan=wlan_m.group(1),
        self_alias=self_alias,
    )


def count_wlan_locks(text: str) -> Optional[int]:
    """Number of `disabled:<busy>||<wlan>` sites left, or None if the
    renderer couldn't be located at all."""
    site = find_wlan_lock(text)
    if site is None:
        return None
    return len(re.findall(site.pattern, text[site.start:site.end]))


def unlock_wlan_ap(text: str, log: logging.Logger, label: str = "") -> tuple[str, int]:
    """Drop the wlan0 condition from every disabled= in the network renderer.

    Returns (new_text, substitutions).
    """
    tag = f"[{label}] " if label else ""
    site = find_wlan_lock(text)
    if site is None:
        log.warning("%sWLAN0 AP unlock: renderInterfaceConfig not found (or its "
                    "locals changed shape) — dashboard AP fields stay locked", tag)
        return text, 0

    span = text[site.start:site.end]
    patched, count = re.subn(site.pattern, site.replacement, span)
    if count == 0:
        log.warning("%sWLAN0 AP unlock: no %r sites in renderInterfaceConfig "
                    "— already unlocked, or upstream changed the condition",
                    tag, f"disabled:{site.busy}||{site.wlan}")
        return text, 0
    log.info("%sWLAN0 AP unlock: %d field(s) (busy=%s, wlan0=%s, self=%s)",
             tag, count, site.busy, site.wlan, site.self_alias)
    return text[:site.start] + patched + text[site.end:], count


# --- Static AP address literal ---------------------------------------------

# The dashboard resets the AP to 172.30.0.1 whenever the form is submitted;
# dropping the literal lets a changed address stick.
STATIC_IP_RE = r',\{static_ip:"172\.30\.0\.1",gateway:"172\.30\.0\.1",use_dhcp:!1\}'
STATIC_IP_REPL = ",{}"


# --- Fault count baseline + reset button -----------------------------------

# Frontend-only baseline offset for the fault counters.
FAULT_COUNTS_RE = r"faultCounts:t\.fc\|\|\[0,0,0,0,0,0\]"
FAULT_COUNTS_REPL = (
    "faultCounts:(window.__rawFC=t.fc||[0,0,0,0,0,0]).map(function(v,j){"
    "return Math.max(0,v-((window.__faultBL||[])[j]||0))})"
)

# Substring of FAULT_COUNTS_REPL, used to check the substitution landed.
FAULT_COUNTS_MARKER = "window.__rawFC=t.fc"

# Marker used to make the fault-button injection idempotent.
FAULT_RESET_MARKER = "window.__faultBL=window.__rawFC"

# End of the historical fault list — the `]` closes the tooltip's children
# array, which is where the button gets appended.
FAULT_LIST_TAIL_RE = r'"historical-"\.concat\(t\)\)\}\)\)\]'

# The bundler minifies the react/jsx-runtime import to a different name in
# every build ("xo" in Beta 10, "bo" in Beta 14 build 201, "Ro" in build 210),
# so the alias has to be read out of the surrounding code rather than
# hardcoded — the injected markup throws a ReferenceError and blanks the
# dashboard if it names the wrong one.
JSX_ALIAS_RE = r"\(0,(\w+)\.jsx\)"


def inject_fault_reset_button(text: str, log: logging.Logger,
                              label: str = "") -> tuple[str, int]:
    """Append a "Reset Fault Counts" button to the dashboard fault tooltip.

    Returns (new_text, 1 if injected else 0).  Idempotent.
    """
    tag = f"[{label}] " if label else ""
    if FAULT_RESET_MARKER in text:
        log.info("%sFault reset button already present, skipping", tag)
        return text, 0

    m = re.search(FAULT_LIST_TAIL_RE, text)
    if not m:
        log.warning("%sFault history list not found — dashboard layout changed "
                    "upstream; skipping fault reset button", tag)
        return text, 0

    # The nearest jsx() call before the list is the one rendering it.
    aliases = re.findall(JSX_ALIAS_RE, text[max(0, m.start() - 500):m.start()])
    if not aliases:
        log.warning("%sCould not determine the jsx alias near the fault list; "
                    "skipping fault reset button", tag)
        return text, 0

    jsx = aliases[-1]
    button = (
        f'"historical-".concat(t))}})),'
        f'(0,{jsx}.jsx)("div",{{style:{{marginTop:"8px",textAlign:"center"}},'
        f'children:(0,{jsx}.jsx)("button",{{onClick:function(){{'
        f'{FAULT_RESET_MARKER}?window.__rawFC.slice():[]}},'
        f'style:{{fontSize:"11px",padding:"2px 8px",cursor:"pointer",'
        f'background:"#333",color:"#fff",border:"1px solid #666",'
        f'borderRadius:"3px"}},children:"Reset Fault Counts"}})}})]'
    )
    log.info("%sInjecting fault reset button (jsx alias: %s)", tag, jsx)
    return text[:m.start()] + button + text[m.end():], 1


# --- Driver ----------------------------------------------------------------


def _sub(text: str, pattern: str, repl: str, log: logging.Logger, tag: str,
         what: str) -> str:
    """re.subn with a WARNING when nothing matched.

    A dashboard regex that silently matches nothing is the whole failure mode
    this module exists to catch: upstream re-minifies the bundle on every
    build, and a stale pattern looks exactly like a successful patch run.
    """
    patched, count = re.subn(pattern, repl, text)
    if count == 0:
        log.warning("%s%s: no match for %r — already patched, or upstream "
                    "re-minified the bundle and the patch did NOT apply",
                    tag, what, pattern)
        return text
    log.info("%s%s: %d substitution(s)", tag, what, count)
    return patched


def apply_patches(text: str, log: logging.Logger, label: str = "",
                  wlan: bool = True, faults: bool = True) -> str:
    """Apply every enabled dashboard patch to the bundle text."""
    tag = f"[{label}] " if label else ""
    if wlan:
        text, _ = unlock_wlan_ap(text, log, label)
        text = _sub(text, STATIC_IP_RE, STATIC_IP_REPL, log, tag,
                    "AP static address literal")
    if faults:
        text = _sub(text, FAULT_COUNTS_RE, FAULT_COUNTS_REPL, log, tag,
                    "fault count baseline")
        text, _ = inject_fault_reset_button(text, log, label)
    return text


# --- Validation ------------------------------------------------------------


def check_patched(text: str) -> list[str]:
    """Return a list of problems with an already-patched bundle. Empty = good.

    Catches the failure mode that made this module necessary: a regex that
    silently matched nothing after upstream re-minified the bundle.
    """
    problems: list[str] = []
    if FAULT_RESET_MARKER not in text:
        problems.append("fault reset button missing (no %s)" % FAULT_RESET_MARKER)
    if FAULT_COUNTS_MARKER not in text:
        problems.append("fault count baseline missing (no %s)" % FAULT_COUNTS_MARKER)
    if re.search(STATIC_IP_RE, text):
        problems.append("AP static_ip literal still present (AP address will "
                        "reset to 172.30.0.1 on save)")
    remaining = count_wlan_locks(text)
    if remaining is None:
        problems.append("renderInterfaceConfig not found — cannot tell whether "
                        "the WLAN0 AP fields are unlocked")
    elif remaining:
        problems.append(f"WLAN0 AP fields still locked ({remaining} disabled= "
                        "site(s) still test wlan0)")
    return problems
