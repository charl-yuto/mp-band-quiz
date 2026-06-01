from __future__ import annotations

import json
import os
import random
import re
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from scipy.spatial import Voronoi

from mp_api.client import MPRester
from emmet.core.electronic_structure import BSPathType
from pymatgen.core import Element
from pymatgen.electronic_structure.core import OrbitalType, Spin
from pymatgen.electronic_structure.plotter import BSPlotter


# ============================================================
# Paths
# ============================================================

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = Path(__file__).resolve().parent
FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"
CACHE_DIR = BACKEND_DIR / "cache"
QUIZ_CACHE_DIR = CACHE_DIR / "quizzes"
CANDIDATE_CACHE_DIR = CACHE_DIR / "candidate_pools"
CACHE_INDEX = CACHE_DIR / "index.json"
RECENT_HISTORY = CACHE_DIR / "recent_history.json"

CACHE_DIR.mkdir(parents=True, exist_ok=True)
QUIZ_CACHE_DIR.mkdir(parents=True, exist_ok=True)
CANDIDATE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# FastAPI setup
# ============================================================

app = FastAPI(title="MP Band Quiz", version="8.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def json_exception_handler(request: Request, exc: Exception):
    # Prevent the frontend from receiving plain-text "Internal Server Error",
    # which would cause "Unexpected token I ... is not valid JSON".
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc().splitlines()[-12:],
        },
    )


# ============================================================
# Request models
# ============================================================

class QuizSettings(BaseModel):
    api_key: Optional[str] = None

    material_kind: str = Field("any", description="any, element, compound")
    metallicity: str = Field("any", description="any, metal, nonmetal")
    stability: str = Field("stable_only", description="stable_only, any")
    nelements_min: int = 1
    nelements_max: int = 3

    path_type: str = "hinuma"

    # Speed / randomization
    # v8 default is LIVE RANDOM: do not reuse full quiz cache and do not prefetch.
    # The only acceleration is a faster MP summary query and optional lightweight
    # candidate-list cache.
    prefer_cache: bool = False       # use full cached quiz if available; default OFF
    cache_only: bool = False         # forbid MP API and use full cached quizzes only; default OFF
    save_cache: bool = False         # save generated full quiz data for later; default OFF
    fast_mode: bool = True

    # Prefer MP materials that already advertise bandstructure/DOS.
    # This greatly reduces slow failed trials.  If the local mp-api does not
    # support has_props filtering, backend automatically falls back.
    require_band_dos_props: bool = True

    # Candidate-list cache stores only mp-ids, not band/DOS. Default OFF for
    # better randomness; turn ON only if the summary search itself is too slow.
    use_candidate_cache: bool = False
    refresh_candidate_pool: bool = True
    candidate_pool_size: int = 120
    candidate_num_chunks: int = 1
    max_trials: int = 6

    # Avoid repeating recently shown materials.
    avoid_recent: bool = True
    recent_limit: int = 1000

    # Data window sent to frontend. Frontend can interactively zoom further.
    energy_min: float = -8.0
    energy_max: float = 8.0
    data_energy_padding: float = 4.0


class CheckAnswerRequest(BaseModel):
    quiz_id: str
    answer: str


class RevealRequest(BaseModel):
    quiz_id: str


class PrefetchRequest(BaseModel):
    count: int = 5
    settings: QuizSettings


# ============================================================
# Utility functions
# ============================================================

_SUB_MAP = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")
_GREEK_MAP = {
    "GAMMA": "Γ", "Gamma": "Γ", "gamma": "Γ", "Γ": "Γ",
    "SIGMA": "Σ", "Sigma": "Σ", "sigma": "σ", "Σ": "Σ",
    "DELTA": "Δ", "Delta": "Δ", "delta": "δ", "Δ": "Δ",
    "LAMBDA": "Λ", "Lambda": "Λ", "lambda": "λ", "Λ": "Λ",
    "THETA": "Θ", "Theta": "Θ", "theta": "θ", "Θ": "Θ",
    "PHI": "Φ", "Phi": "Φ", "phi": "φ", "Φ": "Φ",
    "ETA": "η", "Eta": "η", "eta": "η", "η": "η",
    "MU": "μ", "Mu": "μ", "mu": "μ", "μ": "μ",
    "NU": "ν", "Nu": "ν", "nu": "ν", "ν": "ν",
    "XI": "Ξ", "Xi": "Ξ", "xi": "ξ", "Ξ": "Ξ", "ξ": "ξ",
    "PI": "Π", "Pi": "Π", "pi": "π", "Π": "Π", "π": "π",
    "RHO": "ρ", "Rho": "ρ", "rho": "ρ", "ρ": "ρ",
    "TAU": "τ", "Tau": "τ", "tau": "τ", "τ": "τ",
    "OMEGA": "Ω", "Omega": "Ω", "omega": "ω", "Ω": "Ω", "ω": "ω",
}
_LATEX_CMD_RE = re.compile(r"\\([A-Za-z]+)")


def _replace_latex_command(match):
    cmd = match.group(1)
    if cmd in {"mid", "vert"}:
        return "|"
    return _GREEK_MAP.get(cmd, cmd)


def _convert_token(token: str) -> str:
    token = token.strip()
    if token == "":
        return ""
    if "_" in token:
        base, sub = token.split("_", 1)
    else:
        base, sub = token, ""
    base = _GREEK_MAP.get(base, base)
    if sub:
        return base + sub.translate(_SUB_MAP)
    return base


def pretty_klabel(label: Any) -> str:
    if label is None:
        return ""
    s = str(label).strip()
    if not s:
        return ""
    s = s.replace("$", "").replace(" ", "")
    s = _LATEX_CMD_RE.sub(_replace_latex_command, s)
    s = re.sub(r"(?i)mid", "|", s)
    s = s.replace("\\", "").replace("{", "").replace("}", "")
    s = s.replace("/", "|")
    s = re.sub(r"\|+", "|", s).strip("|")
    parts = [_convert_token(p) for p in s.split("|")]
    parts = [p for p in parts if p]
    return "|".join(parts)


def spin_name(spin_key: Any) -> str:
    if spin_key == Spin.up or str(spin_key) in {"1", "Spin.up"}:
        return "up"
    if spin_key == Spin.down or str(spin_key) in {"-1", "Spin.down"}:
        return "down"
    return str(spin_key)


def sum_spin_densities(densities: Dict[Any, Any]) -> np.ndarray:
    out = None
    for dens in densities.values():
        dens = np.asarray(dens, dtype=float)
        if out is None:
            out = np.zeros_like(dens)
        out += dens
    if out is None:
        return np.array([], dtype=float)
    return out


def orbital_type_to_label(orb: Any) -> str:
    if orb == OrbitalType.s or str(orb).lower().endswith("s"):
        return "s"
    if orb == OrbitalType.p or str(orb).lower().endswith("p"):
        return "p"
    if orb == OrbitalType.d or str(orb).lower().endswith("d"):
        return "d"
    if orb == OrbitalType.f or str(orb).lower().endswith("f"):
        return "f"
    return str(getattr(orb, "name", str(orb))).split(".")[-1]


def safe_attr(obj: Any, name: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def get_api_key(key_from_request: Optional[str]) -> str:
    key = (key_from_request or "").strip() or os.environ.get("MP_API_KEY", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="MP API key がありません。画面で入力するか、サーバー側で MP_API_KEY を設定してください。")
    return key


def get_path_type(name: str):
    table = {
        "hinuma": BSPathType.hinuma,
        "setyawan_curtarolo": BSPathType.setyawan_curtarolo,
        "sc": BSPathType.setyawan_curtarolo,
        "latimer_munro": BSPathType.latimer_munro,
        "lm": BSPathType.latimer_munro,
    }
    return table.get(name, BSPathType.hinuma)


def normalize_answer(s: str) -> str:
    return re.sub(r"\s+", "", str(s).strip().lower())


def load_index() -> List[Dict[str, Any]]:
    if not CACHE_INDEX.exists():
        return []
    try:
        return json.loads(CACHE_INDEX.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_index(index: List[Dict[str, Any]]):
    CACHE_INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")




def load_recent_history() -> List[str]:
    if not RECENT_HISTORY.exists():
        return []
    try:
        data = json.loads(RECENT_HISTORY.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception:
        pass
    return []


def add_recent_mpid(mpid: str, limit: int = 200):
    hist = [x for x in load_recent_history() if x != mpid]
    hist.insert(0, str(mpid))
    hist = hist[: max(1, int(limit))]
    RECENT_HISTORY.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")


def settings_candidate_key(settings: QuizSettings) -> str:
    # Key only by summary-search filters, not energy range or display settings.
    key = {
        "material_kind": settings.material_kind,
        "metallicity": settings.metallicity,
        "stability": settings.stability,
        "nelements_min": settings.nelements_min,
        "nelements_max": settings.nelements_max,
        "pool_size": settings.candidate_pool_size,
        "num_chunks": settings.candidate_num_chunks,
        "require_band_dos_props": settings.require_band_dos_props,
    }
    raw = json.dumps(key, sort_keys=True, ensure_ascii=False)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)[:160]
    return safe


def candidate_cache_path(settings: QuizSettings) -> Path:
    return CANDIDATE_CACHE_DIR / f"{settings_candidate_key(settings)}.json"


def load_candidate_pool(settings: QuizSettings) -> Optional[List[str]]:
    p = candidate_cache_path(settings)
    if not p.exists() or settings.refresh_candidate_pool:
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        ids = data.get("material_ids", [])
        if isinstance(ids, list) and ids:
            return [str(x) for x in ids]
    except Exception:
        pass
    return None


def save_candidate_pool(settings: QuizSettings, material_ids: List[str]):
    p = candidate_cache_path(settings)
    payload = {
        "created_at": time.time(),
        "settings_key": settings_candidate_key(settings),
        "material_ids": list(dict.fromkeys([str(x) for x in material_ids])),
        "note": "Lightweight candidate cache: only material IDs, no band/DOS/answer payload.",
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def choose_candidate_order(material_ids: List[str], settings: QuizSettings) -> List[str]:
    ids = list(dict.fromkeys([str(x) for x in material_ids]))
    if settings.avoid_recent:
        recent = set(load_recent_history()[: max(1, int(settings.recent_limit))])
        fresh = [x for x in ids if x not in recent]
        if fresh:
            ids = fresh
    random.shuffle(ids)
    return ids


def cache_path(quiz_id: str) -> Path:
    return QUIZ_CACHE_DIR / f"{quiz_id}.json"


def save_quiz_cache(payload: Dict[str, Any], add_to_index: bool = True):
    quiz_id = payload["quiz_id"]
    cache_path(quiz_id).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if not add_to_index:
        return
    index = load_index()
    index = [x for x in index if x.get("quiz_id") != quiz_id]
    index.append({
        "quiz_id": quiz_id,
        "mpid": payload["secret"]["mpid"],
        "answer": payload["secret"]["answer"],
        "anonymous_formula": payload["hint"].get("anonymous_formula"),
        "nelements": payload["hint"].get("nelements"),
        "is_metal": payload["hint"].get("is_metal"),
        "is_stable": payload["hint"].get("is_stable"),
        "kind": "element" if int(payload["hint"].get("nelements") or 99) == 1 else "compound",
        "created_at": time.time(),
    })
    save_index(index)


def load_quiz_cache(quiz_id: str) -> Dict[str, Any]:
    p = cache_path(quiz_id)
    if not p.exists():
        raise HTTPException(status_code=404, detail="この quiz_id のキャッシュが見つかりません。")
    return json.loads(p.read_text(encoding="utf-8"))


def public_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    # Do not send the secret answer to the frontend until reveal/check.
    out = {k: v for k, v in payload.items() if k != "secret"}
    out["ok"] = True
    return out


def matches_settings(meta: Dict[str, Any], settings: QuizSettings) -> bool:
    ne = meta.get("nelements")
    try:
        ne = int(ne)
    except Exception:
        return False
    if not (settings.nelements_min <= ne <= settings.nelements_max):
        return False
    if settings.material_kind == "element" and ne != 1:
        return False
    if settings.material_kind == "compound" and ne <= 1:
        return False
    if settings.metallicity == "metal" and meta.get("is_metal") is not True:
        return False
    if settings.metallicity == "nonmetal" and meta.get("is_metal") is not False:
        return False
    if settings.stability == "stable_only" and meta.get("is_stable") is not True:
        return False
    return True


def choose_cached(settings: QuizSettings) -> Optional[Dict[str, Any]]:
    candidates = [m for m in load_index() if matches_settings(m, settings)]
    if not candidates:
        return None
    meta = random.choice(candidates)
    return load_quiz_cache(meta["quiz_id"])


def composition_alias_map(composition) -> Dict[str, str]:
    # Make A/B/C match anonymous stoichiometric order approximately:
    # smaller reduced amount -> A, next -> B, ...
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    red = composition.reduced_composition
    items = [(str(el), float(amt)) for el, amt in red.items()]
    items_sorted = sorted(items, key=lambda x: (x[1], x[0]))
    return {elem: (letters[i] if i < len(letters) else f"X{i+1}") for i, (elem, _) in enumerate(items_sorted)}


def display_label(true_label: str, alias_map: Dict[str, str]) -> str:
    parts = true_label.split()
    if len(parts) >= 2:
        elem = parts[0]
        rest = " ".join(parts[1:])
        return f"{alias_map.get(elem, elem)} {rest}"
    return true_label


def element_order_from_structure(structure) -> List[str]:
    try:
        return [str(e) for e in structure.composition.elements]
    except Exception:
        return []


def get_element_orbital_dos(dos, alias_map: Dict[str, str], energy_min: float, energy_max: float, padding: float) -> Tuple[List[float], List[Dict[str, Any]], List[Dict[str, str]]]:
    energies = np.asarray(dos.energies, dtype=float) - float(dos.efermi)
    lo = energy_min - padding
    hi = energy_max + padding
    mask = (energies >= lo) & (energies <= hi)
    if not np.any(mask):
        mask = np.ones_like(energies, dtype=bool)

    order = element_order_from_structure(dos.structure)
    out = []
    label_map = []

    for elem_symbol in order:
        try:
            elem = Element(elem_symbol)
            elem_spd = dos.get_element_spd_dos(elem)
        except Exception:
            try:
                elem_spd = dos.get_element_spd_dos(elem_symbol)
            except Exception:
                continue

        tmp = {}
        for orb, pdos in elem_spd.items():
            orb_label = orbital_type_to_label(orb)
            dens = sum_spin_densities(pdos.densities)
            if dens.size and np.nanmax(np.abs(dens)) > 1e-12:
                tmp[orb_label] = dens[mask].tolist()

        for orb_label in ["s", "p", "d", "f"]:
            if orb_label in tmp:
                true_label = f"{elem_symbol} {orb_label}"
                shown = display_label(true_label, alias_map)
                out.append({"true_label": true_label, "label": shown, "orbital": orb_label, "element": elem_symbol, "y": tmp[orb_label]})
                label_map.append({"true_label": true_label, "display_label": shown})

    if not out:
        # fallback to total spd DOS
        try:
            for orb, pdos in dos.get_spd_dos().items():
                orb_label = orbital_type_to_label(orb)
                dens = sum_spin_densities(pdos.densities)
                if dens.size and np.nanmax(np.abs(dens)) > 1e-12:
                    out.append({"true_label": orb_label, "label": orb_label, "orbital": orb_label, "element": "", "y": dens[mask].tolist()})
        except Exception:
            pass

    return energies[mask].tolist(), out, label_map


def band_data_from_bs(bs, energy_min: float, energy_max: float, padding: float) -> Dict[str, Any]:
    data = BSPlotter(bs).bs_plot_data()
    distances = data["distances"]
    energies = data["energy"]
    ticks = data["ticks"]

    segments = []
    lo = energy_min - padding
    hi = energy_max + padding

    for spin_key, spin_branches in energies.items():
        sp = spin_name(spin_key)
        for ib, (branch_dist, branch_energies) in enumerate(zip(distances, spin_branches)):
            x = [float(v) for v in branch_dist]
            bands = []
            for band in branch_energies:
                vals = np.asarray(band, dtype=float)
                # Send all bands that intersect a padded window; this reduces JSON size.
                if np.nanmax(vals) >= lo and np.nanmin(vals) <= hi:
                    bands.append([float(v) for v in vals])
            if bands:
                segments.append({"branch_index": ib, "spin": sp, "x": x, "bands": bands})

    tick_dist = []
    tick_labels = []
    for x, lab in zip(ticks.get("distance", []), ticks.get("label", [])):
        lab = pretty_klabel(lab)
        if not lab:
            continue
        x = float(x)
        if not tick_dist or abs(x - tick_dist[-1]) > 1e-5:
            tick_dist.append(x)
            tick_labels.append(lab)
        else:
            parts = tick_labels[-1].split("|")
            for p in lab.split("|"):
                if p and p not in parts:
                    parts.append(p)
            tick_labels[-1] = "|".join(parts)

    return {
        "segments": segments,
        "ticks": {"distance": tick_dist, "label": tick_labels},
        "efermi": float(bs.efermi),
        "is_spin_polarized": bool(bs.is_spin_polarized),
    }


def structure_data(structure) -> Dict[str, Any]:
    lattice = np.asarray(structure.lattice.matrix, dtype=float)
    sites = []
    for site in structure.sites:
        sites.append({
            "species": str(site.specie),
            "frac": [float(x) for x in site.frac_coords],
            "cart": [float(x) for x in site.coords],
        })
    return {"lattice": lattice.tolist(), "sites": sites}


def cart_from_frac(frac, matrix):
    return np.asarray(frac, dtype=float) @ np.asarray(matrix, dtype=float)


def bz_kpath_data(bs) -> Dict[str, Any]:
    rec_matrix = np.asarray(bs.lattice_rec.matrix, dtype=float)
    millers = []
    points = []
    for h in range(-2, 3):
        for k in range(-2, 3):
            for l in range(-2, 3):
                frac = np.array([h, k, l], dtype=float)
                cart = cart_from_frac(frac, rec_matrix)
                millers.append((h, k, l))
                points.append(cart)
    points = np.asarray(points)
    origin_index = millers.index((0, 0, 0))
    vor = Voronoi(points)

    vertices = []
    edges = set()
    faces = []

    def add_vertex(v):
        key = tuple(np.round(v, 10))
        try:
            return vertices.index(list(key))
        except ValueError:
            vertices.append(list(key))
            return len(vertices) - 1

    for pidx, ridge_vertices in zip(vor.ridge_points, vor.ridge_vertices):
        if origin_index in pidx and -1 not in ridge_vertices:
            face_indices = []
            for rv in ridge_vertices:
                idx = add_vertex(vor.vertices[rv])
                face_indices.append(idx)
            faces.append(face_indices)
            n = len(face_indices)
            for i in range(n):
                a = face_indices[i]
                b = face_indices[(i + 1) % n]
                edges.add(tuple(sorted((a, b))))

    branches = []
    for ib, branch in enumerate(bs.branches):
        i0 = branch["start_index"]
        i1 = branch["end_index"]
        pts = []
        labels = []
        for kp in bs.kpoints[i0:i1 + 1]:
            pts.append(cart_from_frac(kp.frac_coords, rec_matrix).tolist())
            labels.append(pretty_klabel(kp.label) if kp.label else "")
        branches.append({"branch_index": ib, "points": pts, "labels": labels})

    high_labels = []
    seen = set()
    for kp in bs.kpoints:
        if not kp.label:
            continue
        lab = pretty_klabel(kp.label)
        if not lab:
            continue
        cart = cart_from_frac(kp.frac_coords, rec_matrix)
        key = tuple(np.round(cart, 6)) + (lab,)
        if key in seen:
            continue
        seen.add(key)
        high_labels.append({"label": lab, "point": cart.tolist()})

    return {
        "reciprocal_lattice": rec_matrix.tolist(),
        "vertices": vertices,
        "edges": [list(e) for e in sorted(edges)],
        "faces": faces,
        "branches": branches,
        "labels": high_labels,
    }


def summary_to_hint(doc) -> Dict[str, Any]:
    sym = safe_attr(doc, "symmetry", None)
    ne = safe_attr(doc, "nelements", None)
    info = {
        "formula_pretty": safe_attr(doc, "formula_pretty", None),
        "formula_anonymous": safe_attr(doc, "formula_anonymous", None),
        "anonymous_formula": safe_attr(doc, "formula_anonymous", None),
        "nelements": int(ne) if ne is not None else None,
        "nsites": safe_attr(doc, "nsites", None),
        "is_metal": safe_attr(doc, "is_metal", None),
        "band_gap": safe_attr(doc, "band_gap", None),
        "is_stable": safe_attr(doc, "is_stable", None),
        "energy_above_hull": safe_attr(doc, "energy_above_hull", None),
        "crystal_system": safe_attr(sym, "crystal_system", None),
        "spacegroup_symbol": safe_attr(sym, "symbol", None),
        "spacegroup_number": safe_attr(sym, "number", None),
        "point_group": safe_attr(sym, "point_group", None),
    }
    for k, v in list(info.items()):
        if v is not None and not isinstance(v, (str, int, float, bool)):
            info[k] = str(v)
    return info


def get_summary(mpr: MPRester, mpid: str):
    docs = mpr.materials.summary.search(
        material_ids=[mpid],
        fields=[
            "material_id", "formula_pretty", "formula_anonymous", "symmetry",
            "nsites", "is_metal", "band_gap", "nelements", "is_stable",
            "energy_above_hull", "structure",
        ],
    )
    if not docs:
        raise RuntimeError(f"Summary not found: {mpid}")
    return docs[0]


def search_candidates(mpr: MPRester, settings: QuizSettings) -> List[str]:
    """Return randomized candidate mp-ids using a lightweight summary query.

    v8 intentionally avoids full problem prefetch/cache by default.  Speed is
    improved by asking MP summary for materials that have bandstructure and DOS
    when the local mp-api supports the has_props filter.
    """
    if settings.use_candidate_cache:
        cached = load_candidate_pool(settings)
        if cached:
            return choose_candidate_order(cached, settings)

    fields = [
        "material_id", "formula_pretty", "formula_anonymous", "symmetry",
        "nsites", "is_metal", "band_gap", "nelements", "is_stable",
        "energy_above_hull", "has_props",
    ]
    chunk_size = max(20, min(int(settings.candidate_pool_size), 500))
    num_chunks = max(1, min(int(settings.candidate_num_chunks), 20))

    base_kwargs: Dict[str, Any] = {
        "fields": fields,
        "chunk_size": chunk_size,
        "num_chunks": num_chunks,
    }
    if settings.stability == "stable_only":
        base_kwargs["is_stable"] = True
    if settings.metallicity == "metal":
        base_kwargs["is_metal"] = True
    elif settings.metallicity == "nonmetal":
        base_kwargs["is_metal"] = False

    attempts: List[Dict[str, Any]] = []
    if settings.require_band_dos_props:
        # Several mp-api versions accept strings here.  Some may require enum
        # values, and older versions may not accept the kwarg at all.  Therefore
        # we try and fall back cleanly.
        attempts.append({**base_kwargs, "has_props": ["bandstructure", "dos"]})
        attempts.append({**base_kwargs, "has_props": ["electronic_structure"]})
    attempts.append(base_kwargs)

    last_exc: Optional[Exception] = None
    docs = []
    for kwargs in attempts:
        try:
            docs = mpr.materials.summary.search(**kwargs)
            break
        except Exception as exc:
            last_exc = exc
            docs = []
            continue
    if not docs and last_exc is not None:
        # one last minimal fallback without newer fields
        minimal_fields = ["material_id", "formula_pretty", "formula_anonymous", "symmetry", "nsites", "is_metal", "band_gap", "nelements", "is_stable", "energy_above_hull"]
        try:
            docs = mpr.materials.summary.search(fields=minimal_fields, chunk_size=chunk_size, num_chunks=num_chunks)
        except Exception:
            raise last_exc

    candidates = []
    for d in docs:
        ne = safe_attr(d, "nelements", None)
        try:
            ne = int(ne)
        except Exception:
            continue
        if not (settings.nelements_min <= ne <= settings.nelements_max):
            continue
        if settings.material_kind == "element" and ne != 1:
            continue
        if settings.material_kind == "compound" and ne <= 1:
            continue
        if settings.metallicity == "metal" and safe_attr(d, "is_metal", None) is not True:
            continue
        if settings.metallicity == "nonmetal" and safe_attr(d, "is_metal", None) is not False:
            continue
        if settings.stability == "stable_only" and safe_attr(d, "is_stable", None) is not True:
            continue
        candidates.append(str(safe_attr(d, "material_id")))

    candidates = list(dict.fromkeys(candidates))
    if settings.use_candidate_cache and candidates:
        save_candidate_pool(settings, candidates)
    return choose_candidate_order(candidates, settings)

def generate_quiz_for_mpid(mpr: MPRester, mpid: str, settings: QuizSettings) -> Dict[str, Any]:
    start = time.time()
    path_type = get_path_type(settings.path_type)

    summary_doc = get_summary(mpr, mpid)
    hint = summary_to_hint(summary_doc)

    bs = mpr.get_bandstructure_by_material_id(mpid, line_mode=True, path_type=path_type)
    dos = mpr.get_dos_by_material_id(mpid)

    structure = getattr(dos, "structure", None) or safe_attr(summary_doc, "structure", None)
    if structure is None:
        raise RuntimeError("Structure not available from DOS/summary.")

    alias_map = composition_alias_map(structure.composition)
    dos_energy, dos_curves, label_map = get_element_orbital_dos(
        dos,
        alias_map=alias_map,
        energy_min=settings.energy_min,
        energy_max=settings.energy_max,
        padding=settings.data_energy_padding,
    )

    band = band_data_from_bs(bs, settings.energy_min, settings.energy_max, settings.data_energy_padding)
    st = structure_data(structure)
    try:
        bz = bz_kpath_data(bs)
        bz_error = None
    except Exception as exc:
        bz = {"vertices": [], "edges": [], "branches": [], "labels": []}
        bz_error = str(exc)

    quiz_id = f"{mpid}_{settings.path_type}_{uuid.uuid4().hex[:8]}"
    formula = hint.get("formula_pretty") or str(structure.composition.reduced_formula)

    payload = {
        "quiz_id": quiz_id,
        "created_at": time.time(),
        "timing_sec": round(time.time() - start, 3),
        "settings_used": settings.model_dump(),
        "hint": {
            **hint,
            "anonymous_formula": hint.get("formula_anonymous") or structure.composition.anonymized_formula,
            "path_type": settings.path_type,
        },
        "band": band,
        "dos": {"energy": dos_energy, "curves": dos_curves, "label_map": label_map},
        "structure": st,
        "bz": bz,
        "errors": {"bz": bz_error},
        "secret": {
            "mpid": mpid,
            "answer": formula,
            "formula_pretty": formula,
            "aliases": [formula.replace(" ", ""), str(structure.composition.reduced_formula)],
            "alias_map": alias_map,
        },
    }
    return payload


# ============================================================
# API endpoints
# ============================================================

@app.get("/api/health")
def health():
    return {"ok": True, "cache_count": len(load_index()), "version": "8.0", "recent_count": len(load_recent_history())}


@app.post("/api/quiz/new")
def new_quiz(settings: QuizSettings):
    if settings.prefer_cache or settings.cache_only:
        cached = choose_cached(settings)
        if cached is not None:
            return public_payload(cached)
        if settings.cache_only:
            raise HTTPException(status_code=404, detail="条件に合うキャッシュがありません。cache only をOFFにしてください。")

    key = get_api_key(settings.api_key)
    errors = []
    with MPRester(key) as mpr:
        candidates = search_candidates(mpr, settings)
        if not candidates:
            raise HTTPException(status_code=404, detail="条件に合う MP 候補が見つかりませんでした。条件を緩めてください。")
        for i, mpid in enumerate(candidates[: max(1, settings.max_trials)]):
            try:
                payload = generate_quiz_for_mpid(mpr, mpid, settings)
                # Always write a per-session quiz file so /check and /reveal work.
                # Only add it to the reusable quiz cache index when save_cache=True.
                save_quiz_cache(payload, add_to_index=settings.save_cache)
                add_recent_mpid(mpid, settings.recent_limit)
                return public_payload(payload)
            except Exception as exc:
                errors.append(f"{mpid}: {exc}")
                continue
    raise HTTPException(status_code=500, detail="band/DOS を取得できる候補が見つかりませんでした: " + " | ".join(errors[-5:]))


@app.post("/api/cache/prefetch")
def prefetch(req: PrefetchRequest):
    settings = req.settings
    settings.cache_only = False
    settings.prefer_cache = False
    settings.save_cache = True
    key = get_api_key(settings.api_key)

    made = []
    errors = []
    with MPRester(key) as mpr:
        candidates = search_candidates(mpr, settings)
        for mpid in candidates:
            if len(made) >= req.count:
                break
            try:
                payload = generate_quiz_for_mpid(mpr, mpid, settings)
                save_quiz_cache(payload, add_to_index=True)
                add_recent_mpid(mpid, settings.recent_limit)
                made.append({"quiz_id": payload["quiz_id"], "mpid": mpid, "answer": payload["secret"]["answer"], "timing_sec": payload["timing_sec"]})
            except Exception as exc:
                errors.append(f"{mpid}: {exc}")
                continue
    return {"ok": True, "created": made, "errors": errors[-10:], "cache_count": len(load_index())}


@app.post("/api/quiz/check")
def check_answer(req: CheckAnswerRequest):
    payload = load_quiz_cache(req.quiz_id)
    sec = payload["secret"]
    answers = [sec.get("answer", ""), sec.get("formula_pretty", ""), *(sec.get("aliases") or [])]
    ok = normalize_answer(req.answer) in {normalize_answer(a) for a in answers if a}
    return {"ok": True, "correct": ok}


@app.post("/api/quiz/reveal")
def reveal(req: RevealRequest):
    payload = load_quiz_cache(req.quiz_id)
    sec = payload["secret"]
    return {
        "ok": True,
        "mpid": sec.get("mpid"),
        "answer": sec.get("answer"),
        "formula_pretty": sec.get("formula_pretty"),
        "alias_map": sec.get("alias_map"),
    }


@app.get("/api/cache/list")
def list_cache():
    return {"ok": True, "items": load_index()}


@app.post("/api/cache/clear_quiz_cache")
def clear_quiz_cache():
    # Remove reusable full quiz cache/index.  Per-session files are also removed.
    for path in QUIZ_CACHE_DIR.glob("*.json"):
        try:
            path.unlink()
        except Exception:
            pass
    if CACHE_INDEX.exists():
        CACHE_INDEX.unlink()
    return {"ok": True, "message": "full quiz cache cleared"}


@app.post("/api/cache/clear_recent")
def clear_recent():
    if RECENT_HISTORY.exists():
        RECENT_HISTORY.unlink()
    return {"ok": True, "message": "recent history cleared"}


@app.post("/api/cache/clear_candidate_cache")
def clear_candidate_cache():
    for path in CANDIDATE_CACHE_DIR.glob("*.json"):
        try:
            path.unlink()
        except Exception:
            pass
    return {"ok": True, "message": "candidate cache cleared"}


# ============================================================
# Static frontend serving
# ============================================================

if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        # API paths are handled above. Everything else returns the SPA index.
        index = FRONTEND_DIST / "index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse({"ok": False, "error": "frontend/dist/index.html not found. Run npm run build."}, status_code=404)
else:
    @app.get("/")
    def no_frontend():
        return {"ok": True, "message": "Backend is running. For one-URL mode, build the frontend with npm run build."}
