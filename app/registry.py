"""Feature registry: each feature package declares everything the core needs in one FEATURE object.

The core (main.py, services.py) is feature-agnostic: it builds menus, /help, schedules, /run, /status
sections and the shared Gmail check by looping over the registry. Adding a feature = a new package under
app/features/ with a feature.py defining FEATURE, plus one line in app/features/__init__.py.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.integrations.gmail import Mail
    from app.main import BotApp
    from app.monitoring import Alarm
    from app.services import Services

Handler = Callable[["BotApp", Any, Any], Awaitable[None]]          # (bot, update, ctx)
JobFn = Callable[..., Awaitable[None]]                              # (services, force=False, **kwargs)
Schedule = dict | str | None                                        # CronTrigger kwargs, crontab string, None = off


@dataclass(frozen=True)
class Command:
    name: str
    description: str                       # one line, shown in /help and Telegram's "/" menu
    handler: Handler
    usage: tuple[str, ...] = ()            # extra lines under the command in /help


@dataclass(frozen=True)
class Alias:
    """An old command name kept working (hidden from /help and the menu) that points to its new form."""
    name: str
    handler: Handler
    hint: str                              # e.g. "/cards <category>"


@dataclass(frozen=True)
class Job:
    id: str                                # also the /run name
    run: JobFn
    schedule: Callable[[dict], Schedule] = lambda settings: None   # None = only via /run


@dataclass(frozen=True)
class MailQuery:
    """A Gmail search a feature needs; all features' queries share ONE IMAP login per scheduled check."""
    criteria: Callable[[dict], list[str]]                 # IMAP SEARCH criteria from settings
    lookback_days: Callable[[dict], int]
    mailboxes: Callable[[dict], tuple[str, ...]]
    handle: Callable[[Services, list[Mail], bool], Awaitable[None]]   # (services, new mails, force)


@dataclass(frozen=True)
class Feature:
    name: str                              # the key used in profiles.<id>.features
    description: str                       # shown in /features
    admin_only: bool = False               # only ever available to the primary profile
    commands: tuple[Command, ...] = ()
    aliases: tuple[Alias, ...] = ()
    callbacks: tuple[tuple[str, Handler], ...] = ()                       # (callback_data regex, handler)
    documents: tuple[tuple[str, Handler], ...] = ()                       # (MIME type, handler) for files sent to the bot
    jobs: tuple[Job, ...] = ()
    profile_jobs: Callable[[Services], list[tuple[str, JobFn, dict, Schedule]]] | None = None  # per person
    mail: MailQuery | None = None
    ready: Callable[[Services, str], bool] = lambda services, pid: True  # set up for this person?
    on_startup: Callable[[Services], None] | None = None
    status_section: Callable[[Services, str], Awaitable[list[str]]] | None = None   # lines for admin /status
    alarms: Callable[[Services], Awaitable[list[Alarm]]] | None = None   # checked every 10 min (app/monitoring.py)
    extra: dict[str, Any] = field(default_factory=dict)
