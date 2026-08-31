#!/usr/bin/env python3
"""
traderie-notifications.py

Te avisa al celular (Telegram, WhatsApp o mail) cuando pasa algo con tus
ofertas en Traderie: te aceptan una, te ofertan por un listing tuyo, etc.

Lee GET /api/<juego>/notifications, que es la campanita del sitio: ya viene
filtrada a tu usuario, con la frase armada y el listing_id para el link.

Dos cosas no obvias sobre esa API:

  1. Traderie está detrás del anti-bot de Cloudflare y bloquea a `requests`
     por su huella TLS (403 "Just a moment...", incluso sin token). Por eso
     las llamadas van con curl_cffi imitando a Chrome.
  2. GET /offers IGNORA los filtros (buyer, seller, user_id, accepted...) y
     siempre devuelve el feed global del juego. No sirve para "lo mío".

Config: variables de entorno, o un archivo .env junto a este script.
Ver .env.example.

Uso:
    pip install requests curl_cffi
    python traderie-notifications.py              # loop infinito
    python traderie-notifications.py --once       # una pasada (Task Scheduler)
    python traderie-notifications.py --test       # manda un mensaje de prueba
    python traderie-notifications.py --chatid     # descubre tu TELEGRAM_CHAT_ID
    python traderie-notifications.py --dump       # imprime el JSON crudo
"""

import argparse
import html
import json
import logging
import os
import smtplib
import ssl
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import requests  # para Telegram / CallMeBot (no pasan por Cloudflare)
from curl_cffi import requests as cffi  # para Traderie

HERE = Path(__file__).resolve().parent


# --- .env --------------------------------------------------------------------


def load_dotenv(path: Path) -> None:
    """Parser mínimo de .env; no pisa variables ya presentes en el entorno."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ.setdefault(key, val)


load_dotenv(Path(os.environ.get("ENV_FILE", HERE / ".env")))


# --- Config ------------------------------------------------------------------

AUTH = os.environ.get("TRADERIE_AUTH", "")
GAME = os.environ.get("TRADERIE_GAME", "diablo2resurrected")
IMPERSONATE = os.environ.get("TRADERIE_IMPERSONATE", "chrome")


def _csv(name: str, default: str) -> set:
    return {c.strip().lower() for c in os.environ.get(name, default).split(",") if c.strip()}


CHANNELS = _csv("NOTIFY_CHANNELS", "telegram")
WATCH = _csv("WATCH_EVENTS", "accepted")

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

WA_PROVIDER = os.environ.get("WHATSAPP_PROVIDER", "callmebot").strip().lower()
WA_TO = os.environ.get("WHATSAPP_TO", "")
WA_CALLMEBOT_KEY = os.environ.get("WHATSAPP_CALLMEBOT_APIKEY", "")
WA_CLOUD_TOKEN = os.environ.get("WHATSAPP_CLOUD_TOKEN", "")
WA_CLOUD_PHONE_ID = os.environ.get("WHATSAPP_CLOUD_PHONE_ID", "")
WA_CLOUD_VERSION = os.environ.get("WHATSAPP_CLOUD_VERSION", "v21.0")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_TO = os.environ.get("MAIL_TO", "") or SMTP_USER

POLL_SECONDS = max(30, int(os.environ.get("POLL_SECONDS", "60")))
STATE_FILE = Path(os.environ.get("STATE_FILE", HERE / "seen_offers.json"))
STATE_MAX = int(os.environ.get("STATE_MAX", "1000"))

# Arranque en frío (sin estado previo): en vez de marcar TODO como visto, avisa
# lo de las últimas N horas. 0 = no avisar nada (default, para correr local).
# En GitHub Actions ponlo en ~2: si se pierde la caché no te quedas sin avisos.
COLD_START_HOURS = float(os.environ.get("COLD_START_HOURS", "0"))

API = f"https://traderie.com/api/{GAME}"

HEADERS = {
    "accept": "application/json",
    "authorization": AUTH,
    "referer": f"https://traderie.com/{GAME}",
}

VALID_CHANNELS = {"telegram", "whatsapp", "email"}

# Nombre amigable -> 'type' de la notificación, y el título del aviso.
EVENTS = {
    "accepted": ("offer-accepted", "✅ Te aceptaron la oferta"),
    "incoming": ("offer-new", "🔔 Te hicieron una oferta"),
    "completed": ("offer-complete", "📦 Trade completado"),
    "denied": ("offer-denied", "❌ Te rechazaron la oferta"),
    "cancelled": ("offer-buyer-cancel", "🚫 El comprador canceló"),
    "reopened": ("offer-reopen", "🔄 Oferta reabierta"),
    "review": ("review", "⭐ Te dejaron una reseña"),
    "message": ("message", "✉️ Mensaje nuevo"),
}
TYPE_TO_EVENT = {t: name for name, (t, _) in EVENTS.items()}

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")
log = logging.getLogger("traderie")

# La consola de Windows es cp1252 y los datos reales traen CJK y emoji
# (ej. un username chino): sin esto el log revienta con UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


class TokenExpired(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


# --- Estado ------------------------------------------------------------------
#
# dict ordenado {notification_id: 1}. Dict porque Python preserva el orden de
# inserción y podar es quitar del principio, que sí son los más antiguos.


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("No pude leer %s (%s); parto de cero", STATE_FILE, exc)
        return {}
    if isinstance(data, list):  # formato viejo (lista de offer_ids)
        return {str(i): 1 for i in data}
    return dict(data) if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    if len(state) > STATE_MAX:
        for key in list(state)[: len(state) - STATE_MAX]:
            del state[key]
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(STATE_FILE)


# --- Mensaje -----------------------------------------------------------------


@dataclass
class Message:
    """Se arma una vez y cada canal la renderiza a su formato."""

    title: str
    body: str = ""
    fields: list = field(default_factory=list)  # [(label, value)]
    url: str = ""

    def as_html(self) -> str:
        out = [f"<b>{html.escape(self.title)}</b>", ""]
        if self.body:
            out += [html.escape(self.body)]
        out += [f"<b>{html.escape(k)}:</b> {html.escape(str(v))}" for k, v in self.fields]
        if self.url:
            out += ["", self.url]
        return "\n".join(out)

    def as_text(self) -> str:
        """WhatsApp usa *negrita*; también sirve de fallback plano para el mail."""
        out = [f"*{self.title}*", ""]
        if self.body:
            out += [self.body]
        out += [f"{k}: {v}" for k, v in self.fields]
        if self.url:
            out += ["", self.url]
        return "\n".join(out)

    @property
    def subject(self) -> str:
        return f"[Traderie] {self.title}"


# --- Traderie ----------------------------------------------------------------


def fetch_notifications() -> list | None:
    """La campanita de Traderie. None si falló (el caller reintenta)."""
    try:
        r = cffi.get(
            f"{API}/notifications", headers=HEADERS, impersonate=IMPERSONATE, timeout=30
        )
    except Exception as exc:  # curl_cffi levanta su propia jerarquía
        log.error("Fallo de red consultando notificaciones: %s", exc)
        return None

    if r.status_code in (401, 403):
        # 403 con HTML es Cloudflare, no el token: conviene distinguirlo.
        if r.text.lstrip().startswith("<"):
            log.error(
                "Cloudflare nos bloqueó. Prueba otro TRADERIE_IMPERSONATE "
                "(chrome131, safari17_0) o sube POLL_SECONDS."
            )
            return None
        raise TokenExpired(r.status_code)
    if r.status_code == 429:
        log.warning("Traderie nos está limitando (429).")
        return None
    if r.status_code != 200:
        log.error("API respondió %s", r.status_code)
        return None

    try:
        body = r.json()
    except ValueError:
        log.error("Respuesta no-JSON desde /notifications")
        return None
    return body.get("notifications") or [] if isinstance(body, dict) else []


def _created_ts(notif: dict) -> float:
    """created_at ISO ('2026-08-30T17:23:01.098Z') -> epoch. 0 si no se puede."""
    raw = notif.get("created_at")
    if isinstance(raw, dict):  # a veces viene en formato Firestore
        return float(raw.get("_seconds", 0))
    if not isinstance(raw, str):
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def describe(notif: dict) -> Message:
    event = TYPE_TO_EVENT.get(notif.get("type"), "")
    title = EVENTS.get(event, ("", "🔔 Traderie"))[1]
    body = (notif.get("message") or notif.get("title") or "").strip()

    listing_id = (notif.get("data") or {}).get("listing_id")
    url = f"https://traderie.com/{GAME}/product/{listing_id}" if listing_id else ""
    return Message(title, body=body, url=url)


# --- Salidas -----------------------------------------------------------------


def _poll_updates(offset: int = 0, timeout: int = 0) -> dict:
    """getUpdates de Telegram. Devuelve {chat_id: nombre} de lo que llegue."""
    params = {"timeout": timeout}
    if offset:
        params["offset"] = offset
    r = requests.get(
        f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", params=params, timeout=timeout + 20
    )
    if r.status_code == 401:
        raise TokenExpired(401)
    if r.status_code != 200:
        log.error("Telegram respondió %s: %s", r.status_code, r.text[:200])
        return {}

    found = {}
    for upd in r.json().get("result", []):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (upd.get(key) or {}).get("chat")
            if chat and chat.get("id") is not None:
                found[chat["id"]] = (
                    chat.get("username") or chat.get("first_name") or chat.get("title") or ""
                )
    return found


def _write_chat_id(cid: int) -> bool:
    """Rellena TELEGRAM_CHAT_ID en el .env si está vacío. Nunca pisa un valor."""
    env = Path(os.environ.get("ENV_FILE", HERE / ".env"))
    if not env.exists():
        return False
    lines = env.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("TELEGRAM_CHAT_ID="):
            if line.split("=", 1)[1].strip():
                return False  # ya tenía algo puesto, no lo tocamos
            lines[i] = f"TELEGRAM_CHAT_ID={cid}"
            env.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True
    return False


def discover_chat_ids(wait_seconds: int = 180) -> int:
    """Saca el TELEGRAM_CHAT_ID preguntándole a tu propio bot quién le escribió.

    Evita depender de bots de terceros tipo @userinfobot, que suelen estar
    caídos. Se queda esperando a que le mandes /start al bot.
    """
    if not TG_TOKEN:
        log.error("Falta TELEGRAM_TOKEN en .env (la línea que te dio BotFather).")
        return 1

    try:
        found = _poll_updates()
        if not found:
            # El token sirve (no dio 401), solo falta que el usuario escriba.
            # Preguntamos por getMe para darle el link exacto de SU bot.
            handle = ""
            try:
                me = requests.get(
                    f"https://api.telegram.org/bot{TG_TOKEN}/getMe", timeout=20
                ).json()
                handle = (me.get("result") or {}).get("username", "")
            except (requests.RequestException, ValueError):
                pass
            print()
            if handle:
                print(f"  Abre esto en el celular:  https://t.me/{handle}")
            else:
                print("  Abre en Telegram el chat de tu bot (el @...bot que creaste)")
            print("  y pulsa START (o mándale cualquier texto).")
            print(f"\n  Esperando hasta {wait_seconds}s... (Ctrl+C para cortar)\n")

            deadline = time.time() + wait_seconds
            while not found and time.time() < deadline:
                found = _poll_updates(timeout=25)
    except TokenExpired:
        log.error(
            "Telegram rechaza el token. Copia de nuevo la línea COMPLETA de BotFather "
            "(formato 123456789:AAH...), sin espacios ni comillas."
        )
        return 1
    except requests.RequestException as exc:
        log.error("No pude hablar con Telegram: %s", exc)
        return 1

    if not found:
        log.error("No llegó ningún mensaje. Manda /start al bot y repite el comando.")
        return 1

    print()
    for cid, who in found.items():
        print(f"  TELEGRAM_CHAT_ID={cid}      <- {who}")
    if len(found) == 1 and _write_chat_id(next(iter(found))):
        print("\n  Lo dejé escrito en tu .env. Ahora prueba:")
        print("      python traderie-notifications.py --test\n")
    else:
        print("\n  Copia esa línea en tu .env\n")
    return 0


def send_telegram(msg: Message) -> bool:
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT,
        "text": msg.as_html(),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(2):
        try:
            r = requests.post(url, json=payload, timeout=20)
        except requests.RequestException as exc:
            log.error("No pude enviar a Telegram: %s", exc)
            return False
        if r.status_code == 200:
            return True
        if r.status_code == 429 and attempt == 0:
            wait = 5
            try:
                wait = int(r.json()["parameters"]["retry_after"])
            except (ValueError, KeyError, TypeError):
                pass
            log.warning("Telegram rate limit; espero %ss", wait)
            time.sleep(min(wait, 60))
            continue
        log.error("Telegram respondió %s: %s", r.status_code, r.text[:200])
        return False
    return False


def send_whatsapp(msg: Message) -> bool:
    text = msg.as_text()
    if WA_PROVIDER == "callmebot":
        # Gratis, sin app de Meta: hay que dar de alta el número una vez
        # escribiéndole a +34 621 33 15 75 el mensaje "I allow callmebot to
        # send me messages", que te devuelve el apikey.
        qs = urllib.parse.urlencode({"phone": WA_TO, "text": text, "apikey": WA_CALLMEBOT_KEY})
        try:
            r = requests.get(f"https://api.callmebot.com/whatsapp.php?{qs}", timeout=30)
        except requests.RequestException as exc:
            log.error("No pude enviar a WhatsApp (callmebot): %s", exc)
            return False
        if r.status_code != 200:
            log.error("CallMeBot respondió %s: %s", r.status_code, r.text[:200])
            return False
        return True

    if WA_PROVIDER == "cloud":
        url = f"https://graph.facebook.com/{WA_CLOUD_VERSION}/{WA_CLOUD_PHONE_ID}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": WA_TO,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }
        try:
            r = requests.post(
                url, json=payload, headers={"Authorization": f"Bearer {WA_CLOUD_TOKEN}"}, timeout=30
            )
        except requests.RequestException as exc:
            log.error("No pude enviar a WhatsApp (cloud): %s", exc)
            return False
        if r.status_code >= 300:
            log.error("WhatsApp Cloud respondió %s: %s", r.status_code, r.text[:300])
            return False
        return True

    log.error("WHATSAPP_PROVIDER desconocido: %r (usa 'callmebot' o 'cloud')", WA_PROVIDER)
    return False


def send_email(msg: Message) -> bool:
    mail = EmailMessage()
    mail["Subject"] = msg.subject
    mail["From"] = SMTP_USER
    mail["To"] = MAIL_TO
    mail.set_content(msg.as_text())
    mail.add_alternative(
        f"<html><body>{msg.as_html().replace(chr(10), '<br>')}</body></html>", subtype="html"
    )

    ctx = ssl.create_default_context()
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(mail)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
                s.starttls(context=ctx)
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(mail)
    except (smtplib.SMTPException, OSError) as exc:
        log.error("No pude enviar el correo: %s", exc)
        return False
    return True


SENDERS = {"telegram": send_telegram, "whatsapp": send_whatsapp, "email": send_email}


def notify(msg: Message) -> bool:
    """Despacha a todos los canales activos. True solo si TODOS funcionaron.

    Estricto a propósito: si falla uno, no marcamos la notificación como vista
    y se reintenta. Con un canal caído prefiero un duplicado en el otro antes
    que perderme una venta.
    """
    results = [SENDERS[c](msg) for c in sorted(CHANNELS)]
    if not any(results):
        log.error("Ningún canal pudo entregar el aviso.")
    return all(results)


# --- Loop --------------------------------------------------------------------


def poll_once(state: dict, first_run: bool) -> bool:
    """Una pasada. True si state cambió."""
    notifs = fetch_notifications()
    if notifs is None:
        return False

    wanted = {EVENTS[e][0] for e in WATCH}
    # Vienen de más nueva a más vieja; invertimos para avisar en orden cronológico.
    nuevas = [
        n
        for n in reversed(notifs)
        if n.get("type") in wanted and str(n.get("id")) not in state
    ]
    if not nuevas:
        return False

    dirty = False

    if first_run:
        # No spamear con el historial: marcamos lo viejo como visto y solo
        # avisamos lo reciente (ver COLD_START_HOURS).
        cutoff = time.time() - COLD_START_HOURS * 3600
        recientes = []
        for n in nuevas:
            if _created_ts(n) >= cutoff:
                recientes.append(n)
            else:
                state[str(n.get("id"))] = 1
                dirty = True
        log.info(
            "Arranque en frío: %d marcadas como vistas, %d por avisar.",
            len(nuevas) - len(recientes),
            len(recientes),
        )
        nuevas = recientes

    for n in nuevas:
        if notify(describe(n)):
            state[str(n.get("id"))] = 1
            dirty = True
            log.info("Avisado %s (%s)", n.get("type"), n.get("id"))
    return dirty


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Notificaciones de Traderie")
    ap.add_argument("--once", action="store_true", help="una sola pasada y salir")
    ap.add_argument("--test", action="store_true", help="manda un mensaje de prueba y sale")
    ap.add_argument("--dump", action="store_true", help="imprime el JSON crudo y sale")
    ap.add_argument("--chatid", action="store_true", help="descubre tu TELEGRAM_CHAT_ID y sale")
    args = ap.parse_args(argv)

    # Antes de validar el resto: es justamente el paso para completar la config.
    if args.chatid:
        return discover_chat_ids()

    if not CHANNELS or not CHANNELS <= VALID_CHANNELS:
        log.error("NOTIFY_CHANNELS debe ser una coma-lista de: %s", ", ".join(sorted(VALID_CHANNELS)))
        return 1
    if not WATCH or not WATCH <= set(EVENTS):
        log.error("WATCH_EVENTS debe ser una coma-lista de: %s", ", ".join(sorted(EVENTS)))
        return 1

    required = [("TRADERIE_AUTH", AUTH)]
    if "telegram" in CHANNELS:
        required += [("TELEGRAM_TOKEN", TG_TOKEN), ("TELEGRAM_CHAT_ID", TG_CHAT)]
    if "whatsapp" in CHANNELS:
        required += [("WHATSAPP_TO", WA_TO)]
        if WA_PROVIDER == "callmebot":
            required += [("WHATSAPP_CALLMEBOT_APIKEY", WA_CALLMEBOT_KEY)]
        else:
            required += [
                ("WHATSAPP_CLOUD_TOKEN", WA_CLOUD_TOKEN),
                ("WHATSAPP_CLOUD_PHONE_ID", WA_CLOUD_PHONE_ID),
            ]
    if "email" in CHANNELS:
        required += [
            ("SMTP_HOST", SMTP_HOST),
            ("SMTP_USER", SMTP_USER),
            ("SMTP_PASS", SMTP_PASS),
            ("MAIL_TO", MAIL_TO),
        ]

    missing = [name for name, val in required if not val]
    if missing:
        log.error("Faltan variables de entorno: %s", ", ".join(missing))
        return 1

    if args.test:
        msg = Message(
            "🧪 Prueba de Traderie",
            body="alguien accepted your offer for Ist rune",
            url=f"https://traderie.com/{GAME}",
        )
        ok = notify(msg)
        log.info("Prueba %s", "OK" if ok else "FALLÓ (revisa los errores arriba)")
        return 0 if ok else 1

    if args.dump:
        try:
            notifs = fetch_notifications()
        except TokenExpired as exc:
            log.error("Token rechazado (%s).", exc)
            return 1
        if notifs is None:
            return 1
        print(json.dumps(notifs, indent=2, ensure_ascii=False))
        return 0

    log.info("Canales: %s | Eventos: %s", ", ".join(sorted(CHANNELS)), ", ".join(sorted(WATCH)))

    state = load_state()
    first_run = not state
    log.info("Arrancando. %d notificaciones ya conocidas.", len(state))

    fails = 0
    while True:
        try:
            if poll_once(state, first_run):
                save_state(state)
            first_run = False
            fails = 0
        except TokenExpired as exc:
            log.error("Token rechazado (%s). Hay que renovar TRADERIE_AUTH.", exc)
            if fails == 0:  # una sola alerta, no una cada minuto
                notify(
                    Message(
                        "⚠️ Traderie: el token de sesión expiró",
                        body="Renueva TRADERIE_AUTH en el .env y reinicia el watcher.",
                    )
                )
            fails += 1

        if args.once:
            return 1 if fails else 0

        # Backoff exponencial cuando la API o el token están caídos.
        time.sleep(POLL_SECONDS * min(2**fails, 16) if fails else POLL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.info("Chao.")
