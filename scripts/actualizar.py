#!/usr/bin/env python3
"""
Radar Norte: genera data.json con noticias reales.

Qué hace:
  1. Consulta Google Noticias (RSS) con búsquedas por estado/tipo de proyecto
     y por cada uno de los 5 medios (El Economista, El Financiero, Expansión,
     Forbes México, El CEO). También lee lo que pongas en fuentes_extra.txt.
  2. Clasifica cada nota: estado, ciudad, tipo de proyecto, señal, origen, prioridad.
  3. Intenta abrir el enlace real de la nota y leer su descripción.
  4. Mezcla con el data.json anterior (así la vista Semanal se va llenando día a día).

Uso:
  python scripts/actualizar.py             # escribe data.json
  python scripts/actualizar.py --prueba    # solo muestra un resumen, no escribe nada

fuentes_extra.txt (opcional, una línea por fuente, '#' para comentarios):
  - Si la línea empieza con http, se lee como feed RSS/Atom.
  - Si no, se usa como búsqueda en Google Noticias (ej.: "torre de oficinas" Monterrey).
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import feedparser
import requests

try:  # opcional: convierte enlaces de Google Noticias en el enlace directo de la nota
    from googlenewsdecoder import gnewsdecoder
except Exception:  # pragma: no cover
    gnewsdecoder = None

RAIZ = Path(__file__).resolve().parent.parent
DATA_JSON = RAIZ / "data.json"
EXTRA_TXT = RAIZ / "fuentes_extra.txt"

UA = "Mozilla/5.0 (compatible; RadarNorte/1.0)"
TZ_MX = timezone(timedelta(hours=-6))
VENTANA = "7d"          # ventana de búsqueda en Google Noticias
DIAS_ITEMS = 14         # cuánto tiempo se conservan oportunidades en data.json
DIAS_NOTICIAS = 8
PAUSA = 1.0             # segundos entre consultas (para no ser bloqueada)
MAX_ENRIQUECER = 60     # cuántas notas por corrida se intentan abrir

ESTADOS = ["Baja California", "Baja California Sur", "Sonora", "Chihuahua", "Coahuila",
           "Nuevo León", "Tamaulipas", "Durango", "Sinaloa", "Zacatecas"]

MEDIOS = {
    "eleconomista.com.mx": "El Economista",
    "elfinanciero.com.mx": "El Financiero",
    "expansion.mx": "Expansión",
    "forbes.com.mx": "Forbes México",
    "elceo.com": "El CEO",
}

# --------------------------------------------------------------------------- utilidades

def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s or ""))
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


def limpio(s: str) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def hacer_id(prefijo: str, base: str) -> str:
    return prefijo + "-" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


def gnews(q: str) -> str:
    return ("https://news.google.com/rss/search?q=" + quote_plus(f"{q} when:{VENTANA}")
            + "&hl=es-419&gl=MX&ceid=MX:es-419")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def dias_desde(fecha_iso: str, ahora: datetime) -> float:
    return (ahora - parse_iso(fecha_iso)).total_seconds() / 86400


# --------------------------------------------------------------------------- descarga

class Estadisticas:
    def __init__(self):
        self.consultas = 0
        self.ok = 0
        self.fallos: list[str] = []


def bajar(url: str, est: Estadisticas) -> list:
    est.consultas += 1
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
        r.raise_for_status()
        entradas = feedparser.parse(r.content).entries
        est.ok += 1
        return entradas
    except Exception as e:  # una fuente caída no debe tumbar todo el proceso
        est.fallos.append(f"{url[:100]} -> {type(e).__name__}: {str(e)[:80]}")
        return []


def parse_entrada(e) -> dict | None:
    t = e.get("published_parsed") or e.get("updated_parsed")
    if not t:
        return None
    fecha = datetime(*t[:6], tzinfo=timezone.utc)
    titulo_raw = limpio(e.get("title", ""))
    src = e.get("source") or {}
    fuente = limpio(src.get("title", "")) if hasattr(src, "get") else ""
    dominio = urlparse(src.get("href", "")).netloc if hasattr(src, "get") else ""
    titulo = titulo_raw
    if fuente and titulo.endswith(" - " + fuente):
        titulo = titulo[: -(len(fuente) + 3)]
    else:
        m = re.match(r"^(.*\S) - ([^-]{2,45})$", titulo_raw)
        if m and not fuente:
            titulo, fuente = m.group(1), m.group(2)
    resumen = limpio(e.get("summary", ""))
    # Google Noticias solo repite el titular + medio: no sirve como resumen
    if len(resumen) < 60 or norm(resumen).startswith(norm(titulo)[:40]):
        resumen = ""
    url = e.get("link", "")
    medio = ""
    for d, nombre in MEDIOS.items():
        if d in dominio or d in url or norm(fuente).startswith(norm(nombre)):
            medio = nombre
            break
    return {"titulo": titulo, "fuente": fuente or (dominio or "Sin fuente"), "medio": medio,
            "url": url, "fecha": fecha, "resumen": resumen}


# --------------------------------------------------------------------------- geografía

_TERMINOS = [
    ("Baja California", r"baja california(?! sur)", ""), ("Baja California", r"tijuana", "Tijuana"),
    ("Baja California", r"mexicali", "Mexicali"), ("Baja California", r"ensenada", "Ensenada"),
    ("Baja California", r"tecate", "Tecate"), ("Baja California", r"rosarito", "Rosarito"),
    ("Baja California Sur", r"baja california sur", ""), ("Baja California Sur", r"los cabos|cabo san lucas|san jose del cabo", "Los Cabos"),
    ("Baja California Sur", r"loreto", "Loreto"),
    ("Sonora", r"sonora", ""), ("Sonora", r"hermosillo", "Hermosillo"), ("Sonora", r"nogales", "Nogales"),
    ("Sonora", r"cajeme|ciudad obregon", "Ciudad Obregón"), ("Sonora", r"guaymas", "Guaymas"),
    ("Sonora", r"san luis rio colorado", "San Luis Río Colorado"), ("Sonora", r"puerto penasco", "Puerto Peñasco"),
    ("Chihuahua", r"chihuahua", ""), ("Chihuahua", r"ciudad juarez|cd\.? juarez", "Ciudad Juárez"),
    ("Chihuahua", r"delicias", "Delicias"),
    ("Coahuila", r"coahuila", ""), ("Coahuila", r"saltillo", "Saltillo"), ("Coahuila", r"ramos arizpe", "Ramos Arizpe"),
    ("Coahuila", r"torreon", "Torreón"), ("Coahuila", r"monclova", "Monclova"),
    ("Coahuila", r"piedras negras", "Piedras Negras"), ("Coahuila", r"ciudad acuna|cd\.? acuna", "Ciudad Acuña"),
    ("Nuevo León", r"nuevo leon", ""), ("Nuevo León", r"monterrey|mty\b", "Monterrey"),
    ("Nuevo León", r"apodaca", "Apodaca"), ("Nuevo León", r"escobedo", "General Escobedo"),
    ("Nuevo León", r"san nicolas de los garza", "San Nicolás de los Garza"),
    ("Nuevo León", r"san pedro garza garcia|san pedro,? n\.?l", "San Pedro Garza García"),
    ("Nuevo León", r"santa catarina,? n\.?l", "Santa Catarina"), ("Nuevo León", r"pesqueria", "Pesquería"),
    ("Nuevo León", r"salinas victoria", "Salinas Victoria"), ("Nuevo León", r"cienega de flores", "Ciénega de Flores"),
    ("Tamaulipas", r"tamaulipas", ""), ("Tamaulipas", r"reynosa", "Reynosa"), ("Tamaulipas", r"matamoros", "Matamoros"),
    ("Tamaulipas", r"nuevo laredo", "Nuevo Laredo"), ("Tamaulipas", r"tampico", "Tampico"),
    ("Tamaulipas", r"altamira", "Altamira"), ("Tamaulipas", r"ciudad victoria", "Ciudad Victoria"),
    ("Durango", r"durango", ""), ("Durango", r"gomez palacio", "Gómez Palacio"),
    ("Sinaloa", r"sinaloa", ""), ("Sinaloa", r"culiacan", "Culiacán"), ("Sinaloa", r"mazatlan", "Mazatlán"),
    ("Sinaloa", r"los mochis|topolobampo", "Los Mochis"),
    ("Zacatecas", r"zacatecas", ""), ("Zacatecas", r"fresnillo", "Fresnillo"),
]
TERMINOS = [(e, re.compile(r"(?<![a-z])(?:" + p + r")(?![a-z])"), c) for e, p, c in _TERMINOS]


def detectar_lugar(texto: str) -> tuple[str, str]:
    """Devuelve (estado, ciudad) del primer lugar del norte mencionado, o ('','')."""
    t = norm(texto)
    mejor = None
    for estado, rx, ciudad in TERMINOS:
        m = rx.search(t)
        if m and (mejor is None or m.start() < mejor[0]):
            mejor = (m.start(), estado, ciudad)
    if not mejor:
        return "", ""
    estado, ciudad = mejor[1], mejor[2]
    if not ciudad:  # se nombró el estado; busca si además aparece una ciudad de ese estado
        for e2, rx, c2 in TERMINOS:
            if e2 == estado and c2 and rx.search(t):
                ciudad = c2
                break
    return estado, ciudad


# --------------------------------------------------------------------------- oportunidades

NEGATIVOS = re.compile(
    r"(?<![a-z])(?:homicid|asesin|balacera|ejecutad|narco|cartel|detenid|feminicid|futbol|liga mx|partido de|"
    r"clima\b|lluvia|tormenta|huracan|sismo|accidente|incendio|desaparecid|cierre de planta|despidos|"
    r"recorte de personal|quiebra|cierra sus puertas|huelga|paro de labores|"
    r"planta de tratamiento|granja solar|parque eolico|termoelectrica|gasoducto|refineria|presa\b)")

TIPOS_RX = [
    ("Oficinas", re.compile(r"oficina|corporativ|\bsede\b|headquarters|\bhq\b|centro de operaciones|centro de servicios|call center|coworking|centro de negocios")),
    ("Hotelería", re.compile(r"\bhotel|resort|hospedaje|habitaciones|hoteler")),
    ("Escuela", re.compile(r"escuela|universidad|campus|colegio|preparatoria|plantel|facultad")),
    ("Industrial", re.compile(r"\bplanta\b|plantas\b|parque industrial|nave industrial|naves industriales|manufactur|maquila|fabrica|armadora|centro de distribucion|logistic|bodega|complejo industrial|gigafactory")),
    ("Retail y otros", re.compile(r"tienda|centro comercial|plaza comercial|sucursal|restaurante|franquicia|supermercado|\bmall\b")),
]
SENALES_RX = [
    ("Licitación", re.compile(r"licitacion|concurso publico|convocatoria de obra")),
    ("Cambio de domicilio fiscal", re.compile(r"domicilio fiscal|traslada\w* su sede|cambia\w* su sede|muda\w* su sede|mueve su sede|reubica\w*|relocaliza\w*|se muda|mudanza de oficinas|cambio de sede")),
    ("Cambio de directivos", re.compile(r"nuevo director|nueva directora|director regional|directora regional|nombramiento de director|nombra (?:a )?(?:nuevo )?(?:director|directora|presidente|ceo|gerente)")),
    ("Vacantes de sede nueva", re.compile(r"vacante|reclutamiento|ofertas? de empleo|convocatoria de empleo|feria de empleo|busca\w* contratar")),
    ("Parque industrial", re.compile(r"parque industrial|nave industrial|naves industriales|industrial park")),
    ("Permiso de construcción", re.compile(r"permiso|licencia de construccion|inicia\w* (?:la )?construccion|primera piedra|arranca\w* (?:la )?construccion|construira|construiran|en construccion|arranque de obra")),
    ("Nearshoring / inversión", re.compile(r"nearshoring|inversion|invertira|invertir|invierte|invierten|mdd|millones de dolares|mmdd|nueva planta|abrira|apertura|inaugura|expansion|ampliacion|se instala|instalara|llegara a|relocalizacion")),
]
NACIONAL_RX = re.compile(r"se muda|traslada|reubica|desde la ciudad de mexico|desde cdmx|desde la cdmx|de la cdmx|deja la cdmx|sale de la|cambia\w* su sede|domicilio fiscal|muda\w* su sede|relocaliza")
EXTRANJERA_RX = re.compile(r"(?<![a-z])(extranjer\w*|internacional\w*|estadounidense\w*|aleman\w*|chin[ao]s?|japones\w*|corean[ao]s?|taiwanes\w*|canadiense\w*|europe\w*|multinacional\w*|asiatic\w*|frances\w*|italian[ao]s?|britanic\w*|suiz[ao]s?|holandes\w*|sueca?s?|estados unidos|eeuu|ee\.? ?uu)(?![a-z])")
BONO_KW = re.compile(r"corporativ|\bsede\b|oficinas|clase a|headquarters|centro de operaciones|call center")
BONO_MONTO = re.compile(r"\d[\d,\.]*\s?(?:millones|mil millones|mdd|mmdd|mdp|mmdp)|millones de (?:dolares|pesos)")

PESO_SENAL = {"Cambio de domicilio fiscal": 32, "Vacantes de sede nueva": 30, "Permiso de construcción": 30,
              "Nearshoring / inversión": 28, "Parque industrial": 25, "Licitación": 22, "Cambio de directivos": 22}
PESO_TIPO = {"Oficinas": 25, "Hotelería": 20, "Industrial": 15, "Escuela": 15, "Retail y otros": 8}
PESO_ORIGEN = {"Nacional que se muda al norte": 12, "Internacional que llega o se consolida en Monterrey": 12,
               "Ya en el norte y se expande": 6}

_NO_EMPRESA = {
    "inversion", "colegio", "escuela", "torre", "tienda", "nuevo", "nueva", "nuevos", "nuevas", "mexico", "gobierno", "sheinbaum", "nearshoring", "empresa",
    "empresas", "hotel", "planta", "parque", "oficinas", "escuela", "universidad", "campus", "sede", "corporativo",
    "anuncia", "anuncian", "invertira", "invertiran", "abre", "abrira", "abriran", "construira", "llega", "llegara",
    "buscan", "busca", "proyecta", "planea", "alista", "preve", "confirma", "presenta", "inicia", "inauguran",
    "inaugura", "arranca", "crean", "crea", "se", "con", "por", "en", "el", "la", "los", "las", "un", "una", "como",
    "habra", "hay", "que", "cuales", "asi", "este", "esta", "tres", "dos", "cinco", "mas", "ante", "tras", "sin",
    "norte", "region", "estado", "ciudad", "senado", "congreso", "sat", "imss", "banxico", "pemex", "cfe", "amlo",
    "eeuu", "ee", "uu", "t-mec", "tmec", "trump", "plan", "programa", "proyecto", "licitacion", "permiso",
}
_CONECT = {"de", "del", "la", "las", "los", "y", "&", "el"}


def extraer_empresa(titulo: str) -> str:
    def valido(c: str) -> bool:
        n = norm(c)
        if len(n) < 3 or n in _NO_EMPRESA:
            return False
        if norm(c.split()[0]) in _NO_EMPRESA:
            return False
        return not any(rx.fullmatch(n) for _, rx, _ in TERMINOS)

    nombre: list[str] = []
    for tok in titulo.split()[:6]:
        base = tok.strip(",.:;\"'“”()¿?¡!")
        if not base:
            break
        if base[0].isupper() or base[0].isdigit() or base == "&":
            nombre.append(base)
        elif norm(base) in _CONECT and nombre and len(nombre) < 4:
            nombre.append(base)
        else:
            break
    while nombre and norm(nombre[-1]) in _CONECT:
        nombre.pop()
    cand = " ".join(nombre)
    if valido(cand):
        return cand
    m = re.search(r"\b(?:de|del|por|empresa|firma|compañía|grupo)\s+((?:[A-ZÁÉÍÓÚÑ][\w&\.\-]*)(?:\s+[A-ZÁÉÍÓÚÑ][\w&\.\-]*){0,2})", titulo)
    if m and valido(m.group(1)):
        return m.group(1)
    return ""


def clasificar_oportunidad(n: dict, ahora: datetime) -> dict | None:
    texto = n["titulo"] + " " + n["resumen"]
    t = norm(texto)
    if NEGATIVOS.search(t):
        return None
    estado, ciudad = detectar_lugar(texto)
    if not estado:
        return None
    tipo = next((nom for nom, rx in TIPOS_RX if rx.search(t)), "")
    senal = next((nom for nom, rx in SENALES_RX if rx.search(t)), "")
    if not tipo or not senal:
        return None
    if NACIONAL_RX.search(t):
        origen = "Nacional que se muda al norte"
    elif EXTRANJERA_RX.search(t):
        origen = "Internacional que llega o se consolida en Monterrey"
    else:
        origen = "Ya en el norte y se expande"
    bono = (8 if BONO_KW.search(t) else 0) + (5 if BONO_MONTO.search(t) else 0) + (3 if estado == "Nuevo León" else 0)
    it = {
        "id": hacer_id("op", n["url"] or n["titulo"]),
        "empresa": extraer_empresa(n["titulo"]),
        "titulo": n["titulo"], "estado": estado, "ciudad": ciudad,
        "tipo": tipo, "origen": origen, "senal": senal,
        "fuente": n["fuente"], "url": n["url"],
        "fecha": n["fecha"].isoformat(),
        "resumen": n["resumen"] or n["titulo"],
        "bono": bono, "enriquecido": bool(n["resumen"]),
    }
    it["prioridad"] = puntaje(it, ahora)
    return it


def puntaje(it: dict, ahora: datetime) -> int:
    d = dias_desde(it["fecha"], ahora)
    rec = 12 if d < 1 else 9 if d < 2 else 6 if d < 4 else 3 if d <= 7 else 0
    p = (PESO_SENAL.get(it["senal"], 20) + PESO_TIPO.get(it["tipo"], 8)
         + PESO_ORIGEN.get(it["origen"], 6) + rec + int(it.get("bono", 0)))
    return max(0, min(100, p))


# --------------------------------------------------------------------------- noticias

TEMAS_RX = [
    ("Tipo de cambio", re.compile(r"dolar|tipo de cambio|superpeso|peso mexicano|divisa")),
    ("Tasas e inflación", re.compile(r"banxico|tasa de interes|tasas de interes|inflacion|inpc")),
    ("Nearshoring e inversión", re.compile(r"nearshoring|inversion extranjera|\bied\b|relocalizacion|plan mexico|inversion")),
    ("Comercio exterior y aranceles", re.compile(r"arancel|t-mec|tmec|exportacion|importacion|aduana|tratado comercial")),
    ("Mercado inmobiliario", re.compile(r"inmobiliari|oficinas|vivienda|suelo industrial|naves industriales|desarrollo inmobiliario|bienes raices")),
    ("Empleo y salarios", re.compile(r"empleo|salario|laboral|desempleo|subcontratacion|outsourcing|40 horas|imss")),
    ("Energía y servicios", re.compile(r"energia|electric|tarifa|\bcfe\b|\bgas\b|\bagua\b|pemex")),
    ("Leyes y regulación", re.compile(r"reforma|iniciativa|decreto|congreso|senado|regulacion|reglamento|\bley\b|\bsat\b|fiscal")),
    ("Mercados", re.compile(r"\bbolsa\b|\bbmv\b|s&p/bmv|wall street|acciones|mercados?\b|\bipc\b")),
]
CATEGORIA_POR_TEMA = {
    "Tipo de cambio": "Economía y política económica", "Tasas e inflación": "Economía y política económica",
    "Nearshoring e inversión": "Empresas y grandes negocios", "Mercado inmobiliario": "Empresas y grandes negocios",
    "Comercio exterior y aranceles": "Economía mexicana e internacional",
    "Empleo y salarios": "Gobierno e impacto económico", "Energía y servicios": "Gobierno e impacto económico",
    "Leyes y regulación": "Gobierno e impacto económico", "Mercados": "Mercados, bolsa e inversión",
}
IMPACTO_POR_TEMA = {
    "Tipo de cambio": "Un dólar más barato abarata mobiliario y acabados importados; uno más caro encarece los presupuestos de interiorismo.",
    "Tasas e inflación": "Las tasas y la inflación afectan el costo del crédito y la decisión de las empresas de arrancar o posponer obras y remodelaciones.",
    "Nearshoring e inversión": "Señal de empresas que llegan o crecen en el norte: futuras oficinas y sedes que necesitarán diseño corporativo.",
    "Comercio exterior y aranceles": "Cambia el costo de materiales importados y el ritmo de inversión industrial, con efecto en oficinas de plantas nuevas.",
    "Mercado inmobiliario": "Refleja la oferta y demanda de oficinas: menos espacio disponible suele empujar proyectos a la medida.",
    "Empleo y salarios": "Más contratación implica más espacio de trabajo; cambios laborales pueden ajustar presupuestos de proyecto.",
    "Energía y servicios": "Influye en dónde deciden instalarse las plantas y sus oficinas administrativas.",
    "Leyes y regulación": "Un cambio de reglas puede acelerar o frenar aperturas de oficinas y proyectos corporativos.",
    "Mercados": "Un mercado optimista favorece anuncios de inversión y nuevos proyectos corporativos.",
}
CEO_RX = re.compile(r"\bceo\b|director general|empresario|presidente de|fundador|dueno de|magnate")


def clasificar_noticia(n: dict) -> dict | None:
    if not n["medio"]:
        return None
    t = norm(n["titulo"] + " " + n["resumen"])
    tema = next((nom for nom, rx in TEMAS_RX if rx.search(t)), "")
    if not tema:
        return None
    categoria = "CEOs, empresarios e industrias" if CEO_RX.search(t) else CATEGORIA_POR_TEMA[tema]
    return {
        "id": hacer_id("nt", n["url"] or n["titulo"]),
        "titulo": n["titulo"], "medio": n["medio"], "categoria": categoria, "tema": tema,
        "url": n["url"], "fecha": n["fecha"].isoformat(),
        "resumen": n["resumen"], "impacto": IMPACTO_POR_TEMA[tema],
    }


# --------------------------------------------------------------------------- consultas

def consultas_base() -> list[str]:
    urls = []
    for estado in ESTADOS:
        urls += [
            gnews(f'("nueva sede" OR "nuevas oficinas" OR corporativo OR "torre de oficinas") "{estado}"'),
            gnews(f'(hotel OR resort) (inversión OR construcción OR "nuevo hotel") "{estado}"'),
            gnews(f'(planta OR "parque industrial" OR nearshoring) (inversión OR expansión) "{estado}"'),
            gnews(f'(escuela OR universidad OR campus OR colegio) (nuevo OR nueva OR construcción) "{estado}"'),
        ]
    urls.append(gnews('("domicilio fiscal" OR "traslada su sede" OR "cambia su sede") (Monterrey OR "Nuevo León" OR Tijuana OR Chihuahua)'))
    urls.append(gnews('licitación (oficinas OR remodelación OR edificio) (Sonora OR Chihuahua OR Coahuila OR "Nuevo León" OR Tamaulipas OR "Baja California")'))
    for dominio in MEDIOS:
        urls += [
            gnews(f"site:{dominio}"),
            gnews(f"site:{dominio} (dólar OR \"tipo de cambio\" OR Banxico OR inflación)"),
            gnews(f"site:{dominio} (nearshoring OR \"inversión extranjera\" OR \"parque industrial\")"),
            gnews(f"site:{dominio} (reforma OR ley OR aranceles OR T-MEC)"),
            gnews(f"site:{dominio} (oficinas OR inmobiliario OR \"Nuevo León\" OR Monterrey)"),
        ]
    return urls


def consultas_extra() -> list[str]:
    if not EXTRA_TXT.exists():
        return []
    salida = []
    for linea in EXTRA_TXT.read_text(encoding="utf-8", errors="ignore").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        salida.append(linea if linea.lower().startswith("http") else gnews(linea))
    return salida


# --------------------------------------------------------------------------- enriquecer

def url_real(url: str) -> str:
    if "news.google.com" not in url or gnewsdecoder is None:
        return url
    try:
        r = gnewsdecoder(url, interval=1)
        if r.get("status") and r.get("decoded_url"):
            return r["decoded_url"]
    except Exception:
        pass
    return url


def meta_descripcion(url: str) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return ""
        head = r.text[:200000]
        for pat in (r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\'][^>]*content=["\']([^"\']+)',
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:description|description)["\']'):
            m = re.search(pat, head, re.I)
            if m:
                d = limpio(m.group(1))
                if len(d) > 40:
                    return d[:300]
    except Exception:
        pass
    return ""


def enriquecer(elementos: list[dict], presupuesto: int) -> int:
    hechos = 0
    for el in elementos:
        if hechos >= presupuesto:
            break
        if el.get("enriquecido"):
            continue
        el["enriquecido"] = True  # se intenta una sola vez por nota
        real = url_real(el["url"])
        hechos += 1
        if real != el["url"]:
            el["url"] = real
            desc = meta_descripcion(real)
            if desc:
                el["resumen"] = desc
    return hechos


# --------------------------------------------------------------------------- principal

def similares(a: str, b: str) -> bool:
    ta = {w for w in re.findall(r"[a-z0-9]{4,}", norm(a))}
    tb = {w for w in re.findall(r"[a-z0-9]{4,}", norm(b))}
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.55


def quitar_duplicados(lista: list[dict], clave_orden) -> list[dict]:
    salida: list[dict] = []
    for el in sorted(lista, key=clave_orden, reverse=True):
        if any(el["id"] == o["id"] or similares(el["titulo"], o["titulo"]) for o in salida):
            continue
        salida.append(el)
    return salida


def cargar_previo() -> dict:
    try:
        d = json.loads(DATA_JSON.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def valido_previo(x: dict) -> bool:
    return (isinstance(x, dict) and str(x.get("url", "")).startswith("http")
            and not str(x.get("id", "")).startswith(("ejemplo", "nota-ejemplo")) and x.get("fecha"))


def main(argv: list[str]) -> int:
    prueba = "--prueba" in argv
    ahora = datetime.now(timezone.utc)
    est = Estadisticas()

    urls = consultas_base() + consultas_extra()
    items: list[dict] = []
    noticias: list[dict] = []
    for i, url in enumerate(urls):
        for e in bajar(url, est):
            n = parse_entrada(e)
            if not n or dias_desde(n["fecha"].isoformat(), ahora) > 8:
                continue
            op = clasificar_oportunidad(n, ahora)
            if op:
                items.append(op)
            nt = clasificar_noticia(n)
            if nt:
                noticias.append(nt)
        if i < len(urls) - 1:
            time.sleep(PAUSA)

    if est.ok == 0:
        print("No se pudo leer ninguna fuente; data.json NO se modificó.", file=sys.stderr)
        for f in est.fallos[:10]:
            print("  -", f, file=sys.stderr)
        return 1

    previo = cargar_previo()
    items += [x for x in previo.get("items", []) if valido_previo(x)]
    noticias += [x for x in previo.get("noticias", []) if valido_previo(x)]

    items = [x for x in items if dias_desde(x["fecha"], ahora) <= DIAS_ITEMS]
    noticias = [x for x in noticias if dias_desde(x["fecha"], ahora) <= DIAS_NOTICIAS]
    # si una nota ya existía, se conserva la versión anterior (ya enriquecida)
    vistos: dict[str, dict] = {}
    for x in items:
        vistos.setdefault(x["id"], x)
    for x in items:
        if x.get("enriquecido") and not vistos[x["id"]].get("enriquecido"):
            vistos[x["id"]] = x
    items = list(vistos.values())

    for x in items:
        x["prioridad"] = puntaje(x, ahora)
    items = quitar_duplicados(items, lambda x: (x["prioridad"], x["fecha"]))
    noticias = quitar_duplicados(noticias, lambda x: x["fecha"])[:150]

    # abrir los enlaces reales de lo más importante
    pool = sorted(items, key=lambda x: -x["prioridad"])[:40] + sorted(noticias, key=lambda x: x["fecha"], reverse=True)[:40]
    n_enr = 0 if prueba else enriquecer(pool, MAX_ENRIQUECER)

    items.sort(key=lambda x: -x["prioridad"])
    noticias.sort(key=lambda x: x["fecha"], reverse=True)

    salida = {
        "actualizado": ahora.astimezone(TZ_MX).isoformat(timespec="seconds"),
        "fuentes": {"consultas": est.consultas, "ok": est.ok, "fallidas": len(est.fallos),
                    "detalle_fallos": est.fallos[:8]},
        "noticias": noticias,
        "items": items,
    }
    print(f"Consultas: {est.consultas} (ok {est.ok}, fallidas {len(est.fallos)}) | "
          f"oportunidades: {len(items)} | noticias: {len(noticias)} | enlaces revisados: {n_enr}")
    for f in est.fallos[:5]:
        print("  fallo:", f)
    if prueba:
        for x in items[:10]:
            print(f"  [{x['prioridad']:>3}] {x['empresa'] or '—':<24} {x['estado']:<20} {x['tipo']:<14} {x['senal']} | {x['titulo'][:80]}")
        return 0
    DATA_JSON.write_text(json.dumps(salida, ensure_ascii=False, indent=1), encoding="utf-8")
    print("data.json actualizado.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
