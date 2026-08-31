#!/usr/bin/env python3
"""Avisa por Telegram cuando una Terror Zone que te interesa esta activa o viene.

Fuente: https://d2runewizard.com/api/trackers/terror-zone, que entrega la zona
actual y la siguiente en una sola llamada y sin pedir token. Se le mandan las
cabeceras de cortesia que documentan (D2R-Contact / D2R-Platform / D2R-Repo)
si las configuras; son opcionales.

    python terror-zones.py            # bucle continuo
    python terror-zones.py --once     # una pasada y sale
    python terror-zones.py --now      # muestra que hay ahora y si hace match
    python terror-zones.py --test     # manda un aviso de ejemplo

Reusa Message y notify() de traderie-notifications.py, asi que respeta el
NOTIFY_CHANNELS que ya tengas configurado.
"""

import argparse
import importlib.util
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent

# El modulo hermano tiene guiones en el nombre, asi que no se puede importar
# con un import normal. Cargarlo asi ademas ejecuta su load_dotenv(), o sea
# que hereda el .env y los canales sin duplicar nada.
_spec = importlib.util.spec_from_file_location(
    "traderie_notifications", HERE / "traderie-notifications.py"
)
tn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tn)

log = logging.getLogger("tz")


# --- Config ------------------------------------------------------------------

API = os.environ.get("TZ_API", "https://d2runewizard.com/api/trackers/terror-zone")

# Zonas que te interesan, separadas por coma. Se comparan por palabras
# completas contra el nombre que devuelve la API, sin distinguir mayusculas
# ni tildes.
TZ_WATCH_DEFAULT = (
    "Chaos Sanctuary,Worldstone Keep,Throne of Destruction,Travincal,"
    "Ancient Tunnels,Pit,Moo Moo Farm,Cow Level,Arcane Sanctuary,"
    "Black Marsh,Catacombs,Nihlathak,Durance of Hate"
)

# "Pit" es la trampa clasica: como palabra suelta tambien pega en "Pit of
# Acheron", que es otra zona. Lo que aparezca aca se descarta aunque haga match.
TZ_EXCLUDE_DEFAULT = "Pit of Acheron,Spider Cavern"

POLL_SECONDS = max(60, int(os.environ.get("TZ_POLL_SECONDS", "120")))
STATE_FILE = Path(os.environ.get("TZ_STATE_FILE", HERE / "seen_tz.json"))

# Zona horaria en la que se muestran las horas de los avisos. Tiene que ser
# la TUYA, no la del servidor que corre el script.
DISPLAY_TZ = os.environ.get("TZ_DISPLAY_TZ", "America/Santiago")

# Minuto de la hora en que rotan las Terror Zones. Observado en 30, no en 0:
# con el valor equivocado TODOS los avisos de "proxima" salen corridos una
# hora entera, asi que es configurable en vez de estar hardcodeado.
ROTATION_MINUTE = int(os.environ.get("TZ_ROTATION_MINUTE", "30")) % 60

# Cabeceras de cortesia que pide su documentacion. Vacias por defecto: pon tu
# correo en el .env si quieres identificarte (la API funciona igual sin ellas).
HEADERS = {"accept": "application/json"}
for _var, _hdr in (
    ("D2RW_CONTACT", "D2R-Contact"),
    ("D2RW_PLATFORM", "D2R-Platform"),
    ("D2RW_REPO", "D2R-Repo"),
):
    _val = os.environ.get(_var, "").strip()
    if _val:
        HEADERS[_hdr] = _val


def _terms(name: str, default: str) -> list:
    return [t.strip().lower() for t in os.environ.get(name, default).split(",") if t.strip()]


WATCH = _terms("TZ_WATCH", TZ_WATCH_DEFAULT)
EXCLUDE = _terms("TZ_EXCLUDE", TZ_EXCLUDE_DEFAULT)


# --- Matching ----------------------------------------------------------------


def _norm(s: str) -> str:
    """Minusculas y espacios colapsados, para comparar sin sorpresas."""
    return re.sub(r"\s+", " ", s or "").strip().lower()


def matches(zone: str) -> list:
    """Terminos de TZ_WATCH que aparecen en el nombre de la zona.

    Compara por palabras completas: "pit" pega en "The Pit" pero no en
    "Pitfall". Lo que este en TZ_EXCLUDE descarta la zona entera, que es como
    se evita que "Pit" se dispare con "Pit of Acheron".
    """
    z = _norm(zone)
    if not z:
        return []
    for bad in EXCLUDE:
        if re.search(rf"\b{re.escape(bad)}\b", z):
            return []
    return [w for w in WATCH if re.search(rf"\b{re.escape(w)}\b", z)]


def _tzinfo():
    """Zona horaria para mostrar las horas.

    Ojo: datetime.now().astimezone() usa la zona de la MAQUINA, y el runner de
    GitHub Actions corre en UTC. Sin fijarla, el aviso decia "20:00" cuando en
    Chile eran las 16:00. zoneinfo necesita la base IANA, que Linux trae de
    fabrica y Windows no: por eso tzdata esta en requirements.txt. Si aun asi
    no resuelve, preferimos una hora en la zona del sistema antes que reventar
    el aviso entero.
    """
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(DISPLAY_TZ)
    except Exception as exc:
        log.warning("No pude usar la zona horaria %s (%s); uso la del sistema.", DISPLAY_TZ, exc)
        return None


TZINFO = _tzinfo()


def next_rotation() -> str:
    """Proxima rotacion, que es a la vez el fin de la zona actual y el
    arranque de la siguiente. Es el mismo instante, asi que un solo calculo
    sirve para los dos avisos.
    """
    ahora = datetime.now(TZINFO) if TZINFO else datetime.now().astimezone()
    borde = ahora.replace(minute=ROTATION_MINUTE, second=0, microsecond=0)
    if borde <= ahora:
        borde += timedelta(hours=1)
    return borde.strftime("%H:%M")


# --- Estado ------------------------------------------------------------------


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("No pude leer %s (%s); parto de cero", STATE_FILE, exc)
        return {}


def save_state(state: dict) -> None:
    try:
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError as exc:
        log.error("No pude guardar %s: %s", STATE_FILE, exc)


# --- API ---------------------------------------------------------------------


def fetch_tz():
    """(actual, siguiente). (None, None) si la API falla."""
    try:
        r = requests.get(API, headers=HEADERS, timeout=20)
    except requests.RequestException as exc:
        log.error("No pude consultar la API de TZ: %s", exc)
        return None, None
    if r.status_code != 200:
        log.error("La API de TZ respondio %s: %s", r.status_code, r.text[:200])
        return None, None
    try:
        data = r.json()
    except ValueError:
        log.error("La API de TZ no devolvio JSON: %s", r.text[:200])
        return None, None

    # Acepta las dos formas que sirve: las claves planas y las anidadas.
    actual = data.get("current") or (data.get("currentTerrorZone") or {}).get("zone") or ""
    siguiente = data.get("next") or (data.get("nextTerrorZone") or {}).get("zone") or ""
    return actual.strip(), siguiente.strip()


# --- Avisos ------------------------------------------------------------------


def announce(zone: str, hits: list, cuando: str) -> bool:
    borde = next_rotation()
    if cuando == "actual":
        msg = tn.Message(
            f"🔥 Terror Zone activa: {zone}",
            body=f"Ya esta corriendo, la tienes hasta las {borde}.",
            fields=[("Coincide con", ", ".join(hits))],
            url="https://d2runewizard.com/terror-zone-tracker",
        )
    else:
        msg = tn.Message(
            f"⏳ Proxima Terror Zone: {zone}",
            body=f"Arranca a las {borde}.",
            fields=[("Coincide con", ", ".join(hits))],
            url="https://d2runewizard.com/terror-zone-tracker",
        )
    return tn.notify(msg)


def poll_once(state: dict) -> bool:
    """Una pasada. True si state cambio."""
    actual, siguiente = fetch_tz()
    if actual is None:
        return False

    dirty = False
    for cuando, zona in (("actual", actual), ("siguiente", siguiente)):
        if not zona:
            continue
        # Avisamos una sola vez por zona y rol: la API repite el mismo valor
        # en cada consulta durante toda la hora.
        if state.get(cuando) == zona:
            continue
        state[cuando] = zona
        dirty = True

        hits = matches(zona)
        if not hits:
            log.info("TZ %s: %s (no esta en tu lista)", cuando, zona)
            continue
        log.info("TZ %s: %s -> AVISO (%s)", cuando, zona, ", ".join(hits))
        if not announce(zona, hits, cuando):
            # Si el aviso no salio, olvidamos el estado para reintentar.
            state.pop(cuando, None)
    return dirty


# --- Main --------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Avisos de Terror Zone")
    ap.add_argument("--once", action="store_true", help="una sola pasada y salir")
    ap.add_argument("--now", action="store_true", help="muestra la TZ actual y si hace match")
    ap.add_argument("--test", action="store_true", help="manda un aviso de ejemplo y sale")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s %(message)s",
    )

    if args.now:
        actual, siguiente = fetch_tz()
        if actual is None:
            return 1
        for cuando, zona in (("Actual", actual), ("Siguiente", siguiente)):
            hits = matches(zona)
            marca = f"SI -> {', '.join(hits)}" if hits else "no"
            print(f"{cuando:10} {zona or '(desconocida)':45} match: {marca}")
        print(f"\nVigilando {len(WATCH)} terminos: {', '.join(WATCH)}")
        if EXCLUDE:
            print(f"Excluidos: {', '.join(EXCLUDE)}")
        return 0

    if args.test:
        ok = announce("Chaos Sanctuary", ["chaos sanctuary"], "siguiente")
        log.info("Prueba %s", "OK" if ok else "FALLO (revisa los errores arriba)")
        return 0 if ok else 1

    log.info("Vigilando %d terminos cada %ds.", len(WATCH), POLL_SECONDS)
    state = load_state()

    while True:
        if poll_once(state):
            save_state(state)
        if args.once:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.info("Chao.")
