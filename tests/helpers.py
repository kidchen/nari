"""Shared fixtures and fakes for the test suite."""
from pathlib import Path

from app.features.market import brief

FIXTURES = Path(__file__).parent / "fixtures"


ZIPS = {"94607": "Oakland", "94704": "Berkeley", "94103": "San Francisco"}


def _card(listing_id, price, addr, city_line, was=None, beds=3, baths=2, sqft=1500):
    was_html = f'<span style="text-decoration: line-through;">${was:,}</span>' if was else ""
    return (f'<a href="https://redmail3.redfin.com/a/click?_e=x{listing_id}"><img src="https://www.redfin.com/'
            f'stingray/do/api-get-listing-hero-shot/{listing_id}/heroshot.jpg"></a>'
            f'<h3><a>${price:,} {was_html}</a></h3><p><a>{beds} Beds {baths} Baths {sqft:,} Sq Ft</a></p>'
            f'<p><a href="https://redmail3.redfin.com/a/click?_e=addr{listing_id}">{addr}</a><br>'
            f'<a href="https://redmail3.redfin.com/a/click?_e=city{listing_id}">{city_line}</a></p>')


CARDS = {
    "disc": {"name": "Disc Card", "owner": "user_a", "unit": "%", "rates": {"other": 1},
             "rotating": {"issuer": "discover", "rate": 5, "cap_per_quarter": 1500, "requires_activation": True,
                          "confirms_activation": False, "quarters": {"2026Q4": ["Static Category"]}}},
    "frdm": {"name": "Freedom Card", "owner": "user_a", "unit": "%", "rates": {"other": 1, "dining": 3},
             "rotating": {"issuer": "chase", "rate": 5, "requires_activation": True}},
    "flat": {"name": "Flat Card", "owner": "user_b", "rates": {"other": 2}},
}


DISCOVER_EMAIL = """<html><body><p>Get 5% cash back at Restaurants, Entertainment and Utilities Oct. 1-Dec. 31</p>
<a href="https://card-e.example.discover.com/click?x=1"><span>Activate 5%</span></a>
<p>This email was sent to: someone@example.com for Discover card account number ending with 1234.</p>
<p>1. Activate to earn 5% Cashback Bonus at Restaurants, Entertainment and Utilities from 10/1/2026 (or the date on
which you activate 5%, whichever is later) through 12/31/2026, on up to $1,500 in purchases.</p></body></html>"""


CHASE_EMAIL = """<html><body><p>From: Chase Freedom <chase@mcmap.chase.com></p>
<td>Earn 5% cash back on up to $1,500 in combined purchases on Dining, Grocery Stores (excluding Walmart &reg; and
Target &reg; *) and donations to the American Red Cross from 10/1/26 to 12/31/26.*</td>
<td>Get ready for next quarter</td><td>Get ready for next quarter</td><td>grocery stores</td><td>top streaming services</td>
<td>Earn in these categories from 1/1/27 to 3/31/27</td></body></html>"""


def _facts():
    from datetime import date as d
    f1 = brief.TickerFacts("ACME", "Acme Rocket Works", "EQUITY", 152.16, -2.8, 4.1, 2.4, -1.0,
                           d(2026, 11, 3))
    f1.headlines = [brief.Headline("ACME-1", "Acme stock is down on CoreWeave troubles", "Barron's", "https://b/1", 1.7)]
    f2 = brief.TickerFacts("IDXF", "Index Fund ETF", "ETF", 707.4, -1.0)
    f3 = brief.TickerFacts("FRUT", "Fruit Computer Inc.", "EQUITY", 343.0, 0.5, earnings=d(2026, 12, 30))
    return {"ACME": f1, "IDXF": f2, "FRUT": f3}


class _FakeClock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def _fake_client(behaviour, calls):
    """behaviour: model -> 'ok' | int error code."""
    from types import SimpleNamespace

    from google.genai import errors

    def generate_content(model, contents, config=None):
        calls.append(model)
        b = behaviour[model]
        if b == "ok":
            return SimpleNamespace(text=f"answer from {model}")
        raise errors.APIError(b, {"error": {"code": b, "message": "high demand", "status": "UNAVAILABLE"}})
    return lambda: SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))


class _FakeIMAP:
    logins = 0

    def __init__(self, host):
        self.box, self.calls = None, []
        self.boxes = {
            "INBOX": {b"1": (b"<old@redfin>", b"Subject: Old\r\nFrom: listings@redfin.com\r\nMessage-ID: <old@redfin>\r\n\r\nhi"),
                      b"2": (b"<new@redfin>", b"Subject: New\r\nFrom: listings@redfin.com\r\nMessage-ID: <new@redfin>\r\n\r\nhey")},
            "[Gmail]/Spam": {b"7": (b"<card@x>", b"Subject: Fw: 5%\r\nFrom: me@outlook.example\r\nMessage-ID: <card@x>\r\n\r\nyo")},
        }

    def login(self, user, pw):
        _FakeIMAP.logins += 1

    def select(self, box, readonly=False):
        assert readonly, "must open mailboxes read-only"
        self.box = box.strip('"')
        return ("OK", [b"1"])

    def search(self, charset, *criteria):
        return ("OK", [b" ".join(self.boxes.get(self.box, {}).keys())])

    def fetch(self, nums, what):
        self.calls.append(what)
        assert "PEEK" in what, "must never mark mail as read"
        out = []
        for n in nums.split(b","):
            mid, raw = self.boxes[self.box][n]
            out.append((n + b" (BODY[...] {1}", f"Message-ID: {mid.decode()}\r\n\r\n".encode() if "HEADER" in what else raw))
            out.append(b")")
        return ("OK", out)

    def logout(self):
        pass


class _RecNotifier:
    def __init__(self, db, fail_for=()):
        self.db, self.fail_for, self.sent = db, set(fail_for), []

    async def send_text(self, profiles, text, reply_markup=None):
        for p in profiles:
            if p in self.fail_for:
                raise RuntimeError(f"telegram down for {p}")
            if self.db.chat_ids_for([p]):
                self.sent.append((p, text.splitlines()[0]))
        return 1

    async def send_photo(self, profiles, data, caption):
        return await self.send_text(profiles, caption)


def _re_jobs(db, notifier):
    """Services for real-estate delivery tests (two people, a = admin)."""
    from types import SimpleNamespace

    from app.services import Services
    settings = {"real_estate": {"zips": ZIPS, "photos": False, "catch_up_days": 3, "enabled": True,
                                "email": {"llm_fallback": False}}}
    cfg = SimpleNamespace(settings=settings, cards={}, gemini_api_key="", primary_profile="a",
                          profiles={"a": {"features": "all", "primary": True}, "b": {"features": ["realestate"]}})
    return Services(cfg, db, notifier)


def _thresholds():
    return {"disk_pct": 85, "mem_pct": 90, "load_per_core": 2.0, "swap_pct": 50, "restarts_24h": 3,
            "backup_max_age_days": 9, "job_failures": 3, "job_failures_hours": 6}


def _paper(doi, title, abstract="", cat="neuroscience", posted="2026-10-14", version=1):
    from app.features.biorxiv.biorxiv import Paper
    return Paper(doi, title, "Doe, J.; Roe, R.", abstract, cat, posted, version)


class _Msg:
    def __init__(self, text, out):
        self.text, self.out = text, out

    async def reply_text(self, text, **kw):
        self.out.append((text, kw.get("reply_markup")))


class _Chat:
    def __init__(self, cid, out):
        self.id, self.out = cid, out

    async def send_message(self, text, **kw):
        self.out.append((text, kw.get("reply_markup")))


def _bot(tmp_path, monkeypatch):
    import asyncio

    from app import config as appcfg
    from app.main import BotApp
    monkeypatch.setattr(appcfg, "DATA_DIR", tmp_path)
    settings = appcfg._load_yaml(appcfg.CONFIG_DIR / "settings.yaml")
    settings["profiles"] = {"admin": {"name": "Admin", "primary": True, "features": "all"},
                            "sci": {"name": "Sci", "features": ["realestate", "biorxiv"],
                                    "biorxiv": {"keywords": ["cortex", "zebrafish|danio"], "categories": ["neuroscience"]}}}
    cfg = appcfg.Config(settings=settings, cards={"disc": {"name": "Disc Card", "owner": "admin", "rates": {"other": 1},
                                                           "rotating": {"issuer": "discover", "rate": 5}}},
                        telegram_token="123:fake", invite_code="x", gemini_api_key="")
    b = BotApp(cfg)
    b.db.register_user(1, "admin", "A")
    b.db.register_user(2, "sci", "S")
    return b, asyncio


def _call(b, asyncio, fn, chat_id, text, out):
    from types import SimpleNamespace
    upd = SimpleNamespace(effective_message=_Msg(text, out), effective_chat=_Chat(chat_id, out), effective_user=None,
                          callback_query=None)
    ctx = SimpleNamespace(args=text.split()[1:], user_data={})
    # registered callbacks / BotApp methods take (update, ctx); raw feature handlers take (bot, update, ctx)
    import inspect
    two_args = len(inspect.signature(fn).parameters) == 2
    asyncio.run(fn(upd, ctx) if two_args else fn(b, upd, ctx))
    return upd


def _cmd(b, name):
    """The callback actually registered with Telegram for /name (exercises the registry wiring)."""
    from telegram.ext import CommandHandler
    for h in b.app.handlers[0]:
        if isinstance(h, CommandHandler) and name in h.commands:
            return h.callback
    raise KeyError(name)
