"""Configuration: where RealRead keeps its browser profile and library.

Nothing is created on disk until the user has confirmed the paths in `realread init`.
Limits in the config can only make the built-in policy stricter (see policy.py).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir

APP = "realread"
# Earlier development name; `init` offers to reuse its directories (and the logins in them).
LEGACY_APP = "paper-reader"


def default_chrome() -> str:
    if sys.platform == "darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if sys.platform.startswith("win"):
        for base in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""),
                     os.environ.get("LOCALAPPDATA", "")):
            p = Path(base) / "Google/Chrome/Application/chrome.exe"
            if base and p.exists():
                return str(p)
        return r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if found := shutil.which(name):
            return found
    return "google-chrome"


def config_path() -> Path:
    override = os.environ.get("REALREAD_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path(user_config_dir(APP)) / "config.toml"


def legacy_config_path() -> Path:
    return Path(user_config_dir(LEGACY_APP)) / "config.toml"


@dataclass
class Config:
    # Dedicated Chrome profile that holds the user's publisher / library logins.
    profile_dir: str
    # Where extracted full texts (and source PDFs), quota state and the audit log live.
    library_dir: str
    chrome_path: str = ""
    cdp_port: int = 9333
    # Optional library proxy; both empty (the usual case) means publisher URLs are opened
    # directly and access comes from campus IP / VPN / "Access through your institution".
    # Prefix form: "https://login.ezproxy.example.edu/login?url=".
    ezproxy_prefix: str = ""
    # Hostname form: "ezproxy.example.edu" (ieeexplore.ieee.org -> ieeexplore-ieee-org.ezproxy.example.edu).
    ezproxy_suffix: str = ""
    # Generic link template for any library proxy / redirector: "$@" is replaced by the article
    # URL as is, "$%" by the percent-encoded URL (e.g. libproxy-db entries, OpenAthens redirector).
    # Takes precedence over the two EZproxy fields above.
    proxy_template: str = ""
    # Optional network proxy for the dedicated Chrome only (e.g. "http://proxy.uni.edu:8080",
    # "socks5://127.0.0.1:1080"), or a PAC file URL your institution publishes. Applied when
    # RealRead starts Chrome; quit the RealRead Chrome after changing them.
    browser_proxy: str = ""
    browser_proxy_pac: str = ""
    # Name of your institution as picked in `realread institution` (display only).
    institution: str = ""
    # Your institution's identity provider (SAML entity ID), learned from your own sign-ins.
    # With it, RealRead re-signs in through your institution when a publisher session expires
    # (no password involved; if your institution asks for one, you are asked to sign in).
    idp_entity_id: str = ""
    idp_federation: str = ""
    auto_sign_in: bool = True
    # Read papers anyone can open by plain HTTP first, without the browser.
    direct_open_access: bool = True
    # Optional stricter limits. Values looser than the built-in policy are ignored.
    min_interval_s: float = 0
    max_per_host_hour: int = 0
    max_per_host_day: int = 0
    max_total_day: int = 0
    max_per_issue_day: int = 0
    # How long a batch waits for you to clear a bot check or log in before it stops.
    max_wait_for_user_s: int = 1800
    # "auto", "en" or "zh".
    language: str = "auto"

    def __post_init__(self) -> None:
        if not self.chrome_path:
            self.chrome_path = default_chrome()

    @property
    def profile(self) -> Path:
        return Path(self.profile_dir).expanduser()

    @property
    def library(self) -> Path:
        return Path(self.library_dir).expanduser()


def defaults() -> Config:
    data = Path(user_data_dir(APP))
    return Config(profile_dir=str(data / "chrome-profile"), library_dir=str(data / "library"))


class NotInitialised(Exception):
    pass


def read(path: Path) -> Config:
    raw = tomllib.loads(path.read_text())
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in raw.items() if k in known})


def load() -> Config:
    path = config_path()
    if not path.exists():
        raise NotInitialised(f"RealRead is not initialised ({path} is missing). Run: realread init")
    return read(path)


def save(cfg: Config) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # json.dumps output is valid TOML for strings, ints and floats.
    lines = [f"{k} = {json.dumps(v)}" for k, v in asdict(cfg).items()]
    path.write_text("\n".join(lines) + "\n")
    return path
