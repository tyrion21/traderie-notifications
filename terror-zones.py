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

# --- Rotacion ---
#
# La API no dice a que hora rota: solo que zona esta activa y cual sigue. La
# hora de los avisos hay que calcularla, y antes se calculaba a puras
# suposiciones, dos de ellas equivocadas: que el minuto de rotacion era fijo y
# que una zona duraba una hora. Rotan cada media hora, a las :00 y a las :30,
# asi que el aviso de zona activa regalaba 30 minutos que no existian.
#
# Ahora las dos cosas se aprenden mirando. Cada vez que el proceso ve cambiar
# la zona actual cuadra ese instante hacia abajo contra la grilla y lo guarda:
# eso fija la fase. La distancia entre dos rotaciones seguidas fija el ciclo.
# Como el polling detecta el cambio a lo mas un par de minutos tarde,
# redondear hacia abajo cae exacto en la rotacion, sea :00 o :30, y el aviso
# se corrige solo sin tocar configuracion.
#
# Todo el calculo vive en UTC y recien se convierte a la zona local para
# imprimir. Asi el cambio de hora de Chile mueve como se ve la hora, no cuando
# ocurre la rotacion.

# Minutos de la grilla contra la que se cuadra una rotacion observada. Tiene
# que dividir 60 exacto.
GRID_MINUTES = int(os.environ.get("TZ_ROTATION_GRID", "30"))
if GRID_MINUTES <= 0 or 60 % GRID_MINUTES:
    log.warning("TZ_ROTATION_GRID=%s no divide 60; uso 30.", GRID_MINUTES)
    GRID_MINUTES = 30

# Cuanto dura una Terror Zone: media hora. Rotan a las :00 y a las :30, no
# cada hora en punto. Suponer 60 es lo que hacia que el aviso de zona activa
# regalara 30 minutos de mas ("hasta las 14:30" cuando terminaba a las 14:00).
# Solo se usa mientras no se hayan presenciado dos rotaciones seguidas: con
# dos, el ciclo se mide en vez de suponerse.
CICLO_DEFAULT = max(1, int(os.environ.get("TZ_ROTATION_CYCLE", "30")))

# Brechas que aceptamos como ciclo medido. Cualquier otra significa que nos
# perdimos una rotacion (proceso reiniciado, API atrasada) y no queremos
# aprender un ciclo inventado a partir de un hueco.
CICLOS_VALIDOS = {GRID_MINUTES, 60}

# Fase de respaldo, para el rato en que todavia no se presencia ninguna
# rotacion. Se interpreta en UTC; para Chile, que esta a horas completas de
# UTC, es el mismo minuto que en local. Con un ciclo de 30 da lo mismo que
# valga :00 o :30 (los bordes caen igual en ambos), pero los avisos que salen
# antes de observar nada se marcan como aproximados igual: el ciclo todavia no
# esta medido y preferimos no mentir con precision.
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


def _local(momento: datetime) -> datetime:
    """Un instante UTC visto desde la zona horaria de los avisos."""
    return momento.astimezone(TZINFO) if TZINFO else momento.astimezone()


def _leer_utc(texto):
    if not texto:
        return None
    try:
        momento = datetime.fromisoformat(texto)
    except (TypeError, ValueError):
        return None
    # Un estado escrito por una version vieja podria venir sin offset, y
    # comparar naive con aware revienta. Se guarda siempre en UTC.
    return momento if momento.tzinfo else momento.replace(tzinfo=timezone.utc)


def _cuadrar(momento: datetime) -> datetime:
    """Redondea hacia abajo contra la grilla. En UTC, que no tiene saltos de
    horario de verano y hace que .replace() sea seguro.
    """
    return momento.replace(
        minute=(momento.minute // GRID_MINUTES) * GRID_MINUTES, second=0, microsecond=0
    )


def ciclo(state: dict) -> timedelta:
    """Cuanto dura una TZ: medido si se presenciaron dos rotaciones, supuesto si no."""
    brechas = [b for b in state.get("brechas", []) if b in CICLOS_VALIDOS]
    if not brechas:
        return timedelta(minutes=CICLO_DEFAULT)
    # El minimo, no el ultimo: perderse una rotacion (API caida, proceso
    # dormido) solo puede AGRANDAR una brecha, nunca acortarla. Asi un hueco
    # de 60 no nos convence de que el ciclo dejo de ser de 30.
    return timedelta(minutes=min(brechas))


def next_rotation(state: dict, ahora: datetime = None):
    """(instante UTC de la proxima rotacion, si la fase se aprendio observando).

    La proxima rotacion es a la vez el fin de la zona actual y el arranque de
    la siguiente: el mismo instante sirve para los dos avisos.
    """
    ahora = ahora or datetime.now(timezone.utc)
    paso = ciclo(state)

    ancla = _leer_utc(state.get("rotacion_utc"))
    aprendida = ancla is not None
    if ancla is None:
        # Todavia sin observar ninguna rotacion: la ultima vez que el reloj
        # paso por el minuto de respaldo.
        ancla = ahora.replace(minute=ROTATION_MINUTE, second=0, microsecond=0)
        if ancla > ahora:
            ancla -= timedelta(hours=1)

    saltos = (ahora - ancla) // paso + 1
    return ancla + saltos * paso, aprendida


# True cuando la fase guardada la observo este proceso, no una corrida previa.
_ANCLA_PROPIA = False


def anclar_rotacion(state: dict, ahora: datetime = None) -> bool:
    """Guarda una rotacion recien presenciada como la nueva fase. True si cambio algo."""
    global _ANCLA_PROPIA

    marca = _cuadrar(ahora or datetime.now(timezone.utc))
    previa = _leer_utc(state.get("rotacion_utc"))

    if previa and marca <= previa:
        # Dos cambios dentro de la misma celda de la grilla. La API es
        # crowd-sourced y a veces titubea entre dos zonas; eso no es una
        # rotacion del juego y no vale la pena pisar el ancla buena.
        log.info("Cambio de zona dentro de la misma media hora; no lo cuento como rotacion.")
        return False

    # La brecha solo sirve si ESTA corrida vio las dos rotaciones: una guardada
    # antes de un reinicio puede tener horas en medio y nos haria medir un
    # ciclo que no existe.
    if previa and _ANCLA_PROPIA:
        brecha = round((marca - previa).total_seconds() / 60)
        if brecha in CICLOS_VALIDOS:
            brechas = [b for b in state.get("brechas", []) if b in CICLOS_VALIDOS]
            state["brechas"] = (brechas + [brecha])[-6:]
            log.info("Ciclo medido: %d min", brecha)

    state["rotacion_utc"] = marca.isoformat()
    _ANCLA_PROPIA = True
    log.info("Rotacion observada: %s", _local(marca).strftime("%H:%M"))
    return True


def _restante(borde: datetime, ahora: datetime) -> str:
    minutos = max(0, round((borde - ahora).total_seconds() / 60))
    if minutos < 1:
        return "menos de 1 min"
    if minutos < 60:
        return f"{minutos} min"
    horas, resto = divmod(minutos, 60)
    return f"{horas}h {resto:02d}m"


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
    """(actual, siguiente). (None, None) si la API falla.

    Ojo con los errores justo en el borde: medido en vivo, la API devuelve algo
    que no es JSON durante ~40s exactos en cada rotacion (se recupera sola a la
    consulta siguiente). No es un bug nuestro ni vale la pena reintentar en
    caliente: saltamos la pasada y el proximo poll la agarra. Como el anclaje
    redondea hacia abajo contra la grilla, detectar a las 15:01 igual fija la
    rotacion en las 15:00.
    """
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


def announce(zone: str, hits: list, cuando: str, state: dict) -> bool:
    ahora = datetime.now(timezone.utc)
    borde, aprendida = next_rotation(state, ahora)
    hora = _local(borde).strftime("%H:%M")
    if not aprendida:
        # Fase supuesta: mejor decirlo que dar una hora exacta que puede estar
        # corrida. Se cae solo apenas el watcher presencie una rotacion.
        hora += " aprox."
    falta = _restante(borde, ahora)

    if cuando == "actual":
        msg = tn.Message(
            f"🔥 Terror Zone activa: {zone}",
            body=f"Ya esta corriendo, la tienes hasta las {hora} (quedan {falta}).",
            fields=[("Coincide con", ", ".join(hits))],
            url="https://d2runewizard.com/terror-zone-tracker",
        )
    else:
        msg = tn.Message(
            f"⏳ Proxima Terror Zone: {zone}",
            body=f"Arranca a las {hora} (en {falta}).",
            fields=[("Coincide con", ", ".join(hits))],
            url="https://d2runewizard.com/terror-zone-tracker",
        )
    return tn.notify(msg)


def poll_once(state: dict, primera: bool = False) -> bool:
    """Una pasada. True si state cambio."""
    actual, siguiente = fetch_tz()
    if actual is None:
        return False

    dirty = False

    # Aprender la fase: solo anclamos rotaciones presenciadas con el proceso ya
    # corriendo. En la primera pasada el estado viene de la cache y puede tener
    # horas encima, o sea que el cambio no ocurrio recien y anclarlo aca nos
    # dejaria con la fase equivocada justo por lo que queriamos arreglar.
    anterior = state.get("actual")
    if anterior and actual != anterior and not primera:
        dirty = anclar_rotacion(state)

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
        if not announce(zona, hits, cuando, state):
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

        # El bloque de relojes es para revisar los dos bugs clasicos de este
        # script sin tener que esperar un aviso: si la hora local esta corrida
        # (zona horaria) y si el minuto de rotacion cuadra (fase).
        state = load_state()
        ahora = datetime.now(timezone.utc)
        aca = _local(ahora)
        borde, aprendida = next_rotation(state, ahora)
        print(f"\nHora local {aca:%Y-%m-%d %H:%M} {aca:%z} ({DISPLAY_TZ}), UTC {ahora:%H:%M}")
        print(
            f"Rotacion   {_local(borde):%H:%M} (en {_restante(borde, ahora)}), "
            f"cada {int(ciclo(state).total_seconds() // 60)} min, "
            f"fase {'observada' if aprendida else f'supuesta en :{ROTATION_MINUTE:02d}'}"
        )

        print(f"\nVigilando {len(WATCH)} terminos: {', '.join(WATCH)}")
        if EXCLUDE:
            print(f"Excluidos: {', '.join(EXCLUDE)}")
        return 0

    if args.test:
        ok = announce("Chaos Sanctuary", ["chaos sanctuary"], "siguiente", load_state())
        log.info("Prueba %s", "OK" if ok else "FALLO (revisa los errores arriba)")
        return 0 if ok else 1

    log.info("Vigilando %d terminos cada %ds.", len(WATCH), POLL_SECONDS)
    state = load_state()
    primera = True

    while True:
        if poll_once(state, primera):
            save_state(state)
        primera = False
        if args.once:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.info("Chao.")
