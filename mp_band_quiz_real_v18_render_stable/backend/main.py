from __future__ import annotations

import json
import os
import random
import secrets
import re
import time
import traceback
import uuid
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
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
from pymatgen.core import Element, Composition
from pymatgen.electronic_structure.core import OrbitalType, Spin
from pymatgen.electronic_structure.plotter import BSPlotter

warnings.filterwarnings("ignore", message="No Pauling electronegativity.*")


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

# In-memory candidate ID pools. These are not full quiz caches; they only store
# material IDs that matched inexpensive summary-level filters.  Render keeps
# this memory while the service is alive, so subsequent random questions avoid
# repeating slow MP summary searches while still fetching fresh band/DOS data.
MEMORY_CANDIDATE_POOLS: Dict[str, List[str]] = {}
# In-memory quiz queue.  This is not persisted to disk and is shuffled/refilled
# while the Render instance is alive.  It makes the next question fast without
# repeatedly showing the same material.
MEMORY_QUIZ_QUEUES: Dict[str, List[Dict[str, Any]]] = {}
QUIZ_QUEUE_LOCK = threading.Lock()
QUIZ_REFILLING: set[str] = set()
QUIZ_REFILL_EXECUTOR = ThreadPoolExecutor(max_workers=1)

CACHE_DIR.mkdir(parents=True, exist_ok=True)
QUIZ_CACHE_DIR.mkdir(parents=True, exist_ok=True)
CANDIDATE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# FastAPI setup
# ============================================================

app = FastAPI(title="MP Band Quiz", version="18.0-render-stable")
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

    # Electronic/orbital filters
    # electron_system: any, sp, d, f, exclude_f_block
    electron_system: str = "any"
    selected_orbitals: List[str] = Field(default_factory=lambda: ["s", "p", "d", "f"])
    exclude_f_block: bool = False
    exclude_missing_f_dos: bool = True

    # Search randomization.  Multiple random element seeds reduce the bias of
    # repeated MP summary.search calls while still avoiding full quiz cache.
    # Random search method:
    #   mpid_batch: generate random mp-ids and query them in batches.
    #               This avoids element bias such as As/V/Cr appearing too often.
    #   element_seed: query by random element seeds. Faster sometimes, but biased.
    #   hybrid: try mpid_batch first, then element_seed fallback.
    random_strategy: str = "fast_pool"
    random_element_search: bool = True
    random_element_seed_count: int = 3
    random_mpid_min: int = 1
    random_mpid_max: int = 2000000
    random_mpid_batch_size: int = 160
    random_mpid_rounds: int = 1

    # Fast random mode.  It caches only candidate mp-ids in memory, not full
    # band/DOS payloads, so the output remains much less repetitive than full
    # quiz cache while avoiding slow repeated summary searches.
    fast_pool_target: int = 80
    fast_pool_refill_rounds: int = 1
    parallel_fetch: bool = True

    # Speed / randomization
    # v8 default is LIVE RANDOM: do not reuse full quiz cache and do not prefetch.
    # The only acceleration is a faster MP summary query and optional lightweight
    # candidate-list cache.
    prefer_cache: bool = False       # use full cached quiz if available; default OFF
    cache_only: bool = False         # forbid MP API and use full cached quizzes only; default OFF
    save_cache: bool = False         # save generated full quiz data for later; default OFF
    fast_mode: bool = True
    # Render free instances become unstable when full next-quiz prefetch runs
    # in the background while the user is interacting. Keep it off by default.
    background_prefetch: bool = False
    # If strict filters fail, return a slightly relaxed quiz with a visible warning
    # instead of making the UI hit a dead end.
    auto_relax_on_failure: bool = True

    # Prefer MP materials that already advertise bandstructure/DOS.
    # This greatly reduces slow failed trials.  If the local mp-api does not
    # support has_props filtering, backend automatically falls back.
    require_band_dos_props: bool = True

    # Candidate-list cache stores only mp-ids, not band/DOS. Default OFF for
    # better randomness; turn ON only if the summary search itself is too slow.
    use_candidate_cache: bool = False
    refresh_candidate_pool: bool = True
    candidate_pool_size: int = 50
    candidate_num_chunks: int = 1
    max_trials: int = 6

    # Avoid repeating recently shown materials.
    avoid_recent: bool = True
    recent_limit: int = 500

    # Data window sent to frontend. Frontend can interactively zoom further.
    energy_min: float = -12.0
    energy_max: float = 12.0
    data_energy_padding: float = 8.0


class CheckAnswerRequest(BaseModel):
    quiz_id: str
    answer: str


class RevealRequest(BaseModel):
    quiz_id: str


class PrefetchRequest(BaseModel):
    count: int = 5
    settings: QuizSettings


class SearchQuizRequest(BaseModel):
    query: str
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
        "electron_system": settings.electron_system,
        "exclude_f_block": settings.exclude_f_block,
        "exclude_missing_f_dos": settings.exclude_missing_f_dos,
        "random_strategy": settings.random_strategy,
        "selected_orbitals": sorted([str(x) for x in (settings.selected_orbitals or [])]),
        "random_element_seed_count": settings.random_element_seed_count,
        "random_mpid_min": settings.random_mpid_min,
        "random_mpid_max": settings.random_mpid_max,
        "random_mpid_batch_size": settings.random_mpid_batch_size,
        "random_mpid_rounds": settings.random_mpid_rounds,
        "fast_pool_target": settings.fast_pool_target,
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
    rng = secrets.SystemRandom()
    ids = list(dict.fromkeys([str(x) for x in material_ids]))
    if settings.avoid_recent:
        recent = set(load_recent_history()[: max(1, int(settings.recent_limit))])
        fresh = [x for x in ids if x not in recent]
        if fresh:
            ids = fresh
    rng.shuffle(ids)
    return ids


def quiz_queue_key(settings: QuizSettings) -> str:
    data = settings.model_dump()
    # api_key should never affect matching, and full cache toggles should not
    # split queues.  Display energy range is kept because it changes payload size.
    data.pop("api_key", None)
    for k in ["prefer_cache", "cache_only", "save_cache"]:
        data.pop(k, None)
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)[:180]


def pop_queued_quiz(settings: QuizSettings) -> Optional[Dict[str, Any]]:
    key = quiz_queue_key(settings)
    with QUIZ_QUEUE_LOCK:
        q = MEMORY_QUIZ_QUEUES.get(key) or []
        while q:
            payload = q.pop(0)
            mpid = payload.get("secret", {}).get("mpid")
            if settings.avoid_recent and mpid in set(load_recent_history()[: max(1, int(settings.recent_limit))]):
                continue
            MEMORY_QUIZ_QUEUES[key] = q
            return payload
        MEMORY_QUIZ_QUEUES[key] = []
    return None


def push_queued_quiz(settings: QuizSettings, payload: Dict[str, Any], max_len: int = 4) -> None:
    key = quiz_queue_key(settings)
    with QUIZ_QUEUE_LOCK:
        q = MEMORY_QUIZ_QUEUES.get(key, [])
        existing = {x.get("secret", {}).get("mpid") for x in q}
        mpid = payload.get("secret", {}).get("mpid")
        if mpid and mpid not in existing:
            q.append(payload)
        MEMORY_QUIZ_QUEUES[key] = q[-max_len:]


def maybe_refill_quiz_queue(settings: QuizSettings, count: int = 2) -> None:
    # v18: background full-quiz generation is optional. On Render Free it often
    # competes with the active request and appears as repeated SummaryDoc/
    # ElectronicStructureDoc calls in the logs, causing long waits and 502-like
    # user experience. Candidate-id pools are still used; full next-quiz
    # prefetch is disabled unless explicitly requested.
    if not getattr(settings, "background_prefetch", False):
        return
    key = quiz_queue_key(settings)
    with QUIZ_QUEUE_LOCK:
        if key in QUIZ_REFILLING:
            return
        if len(MEMORY_QUIZ_QUEUES.get(key, [])) >= count:
            return
        QUIZ_REFILLING.add(key)

    def _worker():
        try:
            st = settings.model_copy(deep=True)
            st.prefer_cache = False
            st.cache_only = False
            st.save_cache = False
            # Use a small search to avoid background jobs monopolizing the free instance.
            st.max_trials = max(4, min(int(st.max_trials), 10))
            st.fast_pool_target = max(60, min(int(st.fast_pool_target), 160))
            st.fast_pool_refill_rounds = 1
            key_api = get_api_key(st.api_key)
            made = 0
            with MPRester(key_api) as local_mpr:
                candidates = search_candidates(local_mpr, st)
                for mpid in candidates:
                    if made >= count:
                        break
                    try:
                        payload = generate_quiz_for_mpid(local_mpr, mpid, st)
                        # Store by quiz_id so answer/reveal work, but do not add to reusable cache index.
                        save_quiz_cache(payload, add_to_index=False)
                        push_queued_quiz(settings, payload)
                        made += 1
                    except Exception:
                        continue
        finally:
            with QUIZ_QUEUE_LOCK:
                QUIZ_REFILLING.discard(key)

    QUIZ_REFILL_EXECUTOR.submit(_worker)


F_BLOCK_SYMBOLS = {
    "La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb", "Lu",
    "Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf", "Es", "Fm", "Md", "No", "Lr",
}
D_BLOCK_SYMBOLS = {
    "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
    "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd",
    "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
}
SP_BLOCK_SYMBOLS = {
    "H", "Li", "Be", "B", "C", "N", "O", "F", "Ne",
    "Na", "Mg", "Al", "Si", "P", "S", "Cl", "Ar",
    "K", "Ca", "Ga", "Ge", "As", "Se", "Br", "Kr",
    "Rb", "Sr", "In", "Sn", "Sb", "Te", "I", "Xe",
    "Cs", "Ba", "Tl", "Pb", "Bi", "Po", "At", "Rn",
}
COMMON_RANDOM_SYMBOLS = sorted(SP_BLOCK_SYMBOLS | D_BLOCK_SYMBOLS | F_BLOCK_SYMBOLS)


def valid_element_symbols_from_doc(doc: Any) -> List[str]:
    elems = safe_attr(doc, "elements", None)
    out: List[str] = []
    if elems:
        for e in elems:
            try:
                out.append(str(getattr(e, "symbol", e)))
            except Exception:
                pass
    if out:
        return out
    formula = safe_attr(doc, "formula_pretty", None)
    if formula:
        try:
            return [str(el) for el in Composition(str(formula)).elements]
        except Exception:
            return []
    return []


def is_f_block_symbol(sym: str) -> bool:
    return str(sym) in F_BLOCK_SYMBOLS


def allowed_seed_symbols(settings: QuizSettings) -> List[str]:
    mode = str(settings.electron_system or "any")
    if mode == "d":
        syms = sorted(D_BLOCK_SYMBOLS)
    elif mode == "f":
        syms = sorted(F_BLOCK_SYMBOLS)
    elif mode == "sp":
        syms = sorted(SP_BLOCK_SYMBOLS)
    elif mode == "exclude_f_block" or settings.exclude_f_block:
        syms = sorted((SP_BLOCK_SYMBOLS | D_BLOCK_SYMBOLS) - F_BLOCK_SYMBOLS)
    else:
        syms = COMMON_RANDOM_SYMBOLS
    return syms


def summary_doc_matches_filters(doc: Any, settings: QuizSettings) -> bool:
    ne = safe_attr(doc, "nelements", None)
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
    if settings.metallicity == "metal" and safe_attr(doc, "is_metal", None) is not True:
        return False
    if settings.metallicity == "nonmetal" and safe_attr(doc, "is_metal", None) is not False:
        return False
    if settings.stability == "stable_only" and safe_attr(doc, "is_stable", None) is not True:
        return False
    elems = valid_element_symbols_from_doc(doc)
    has_f_block = any(is_f_block_symbol(e) for e in elems)
    if settings.exclude_f_block or settings.electron_system == "exclude_f_block":
        if has_f_block:
            return False
    # Coarse prefilters.  Final decision for d/f/sp is made after DOS is read.
    if settings.electron_system == "d" and not any(e in D_BLOCK_SYMBOLS for e in elems):
        return False
    if settings.electron_system == "f" and not has_f_block:
        return False
    if settings.electron_system == "sp" and any((e in D_BLOCK_SYMBOLS or e in F_BLOCK_SYMBOLS) for e in elems):
        return False
    return True


def orbital_weights_from_curves(curves: List[Dict[str, Any]]) -> Dict[str, float]:
    weights = {"s": 0.0, "p": 0.0, "d": 0.0, "f": 0.0}
    for c in curves or []:
        orb = str(c.get("orbital", ""))
        y = c.get("y") or []
        if orb in weights:
            try:
                weights[orb] += float(np.nansum(np.abs(np.asarray(y, dtype=float))))
            except Exception:
                pass
    return weights


def payload_matches_orbital_filters(payload: Dict[str, Any], settings: QuizSettings) -> Tuple[bool, str]:
    curves = payload.get("dos", {}).get("curves", [])
    if not curves:
        return False, "DOS curve が空です"
    allowed = {str(x).lower() for x in (settings.selected_orbitals or ["s", "p", "d", "f"])}
    if allowed:
        curves = [c for c in curves if str(c.get("orbital", "")).lower() in allowed]
        payload["dos"]["curves"] = curves
    if not curves:
        return False, "選択軌道に対応するDOSがありません"
    weights = orbital_weights_from_curves(curves)
    total = sum(weights.values())
    if total <= 0:
        return False, "DOS weight がゼロです"
    mode = str(settings.electron_system or "any")
    if mode == "d" and weights.get("d", 0.0) <= 0:
        return False, "d-DOS がありません"
    if mode == "f" and weights.get("f", 0.0) <= 0:
        return False, "f-DOS がありません"
    if mode == "sp" and (weights.get("d", 0.0) + weights.get("f", 0.0)) > 0.35 * total:
        return False, "s/p系としては d/f 成分が大きすぎます"
    alias_map = payload.get("secret", {}).get("alias_map", {})
    true_elements = set(alias_map.keys())
    contains_f_block = any(is_f_block_symbol(e) for e in true_elements)
    if settings.exclude_missing_f_dos and contains_f_block and weights.get("f", 0.0) <= 0:
        return False, "fブロック元素を含むのに f-DOS がありません"
    return True, ""


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


def get_element_orbital_dos(dos, alias_map: Dict[str, str], energy_min: float, energy_max: float, padding: float, selected_orbitals: Optional[List[str]] = None) -> Tuple[List[float], List[Dict[str, Any]], List[Dict[str, str]]]:
    energies = np.asarray(dos.energies, dtype=float) - float(dos.efermi)
    lo = energy_min - padding
    hi = energy_max + padding
    mask = (energies >= lo) & (energies <= hi)
    if not np.any(mask):
        mask = np.ones_like(energies, dtype=bool)

    order = element_order_from_structure(dos.structure)
    allowed_orbs = {str(x).lower() for x in (selected_orbitals or ["s", "p", "d", "f"])}
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
            if orb_label in allowed_orbs and orb_label in tmp:
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
                if orb_label in allowed_orbs and dens.size and np.nanmax(np.abs(dens)) > 1e-12:
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
        "elements": [str(getattr(e, "symbol", e)) for e in (safe_attr(doc, "elements", None) or [])],
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
            "energy_above_hull", "elements", "structure",
        ],
    )
    if not docs:
        raise RuntimeError(f"Summary not found: {mpid}")
    return docs[0]


def _summary_search_once(mpr: MPRester, kwargs: Dict[str, Any]) -> List[Any]:
    try:
        return list(mpr.materials.summary.search(**kwargs))
    except Exception as exc:
        # Fall back if has_props is not accepted by the installed mp-api/server.
        if "has_props" in kwargs:
            kw2 = dict(kwargs)
            kw2.pop("has_props", None)
            return list(mpr.materials.summary.search(**kw2))
        raise exc


def _doc_has_required_band_dos(doc: Any, settings: QuizSettings) -> bool:
    """Check summary.has_props in Python when possible."""
    if not settings.require_band_dos_props:
        return True
    props = safe_attr(doc, "has_props", None)
    if props is None:
        return True
    try:
        prop_set = {str(x).lower() for x in props}
    except Exception:
        prop_set = {str(props).lower()}
    return "bandstructure" in prop_set and "dos" in prop_set


def _random_material_id_batch(rng: secrets.SystemRandom, settings: QuizSettings, batch_size: int) -> List[str]:
    lo = max(1, int(settings.random_mpid_min))
    hi = max(lo + 1, int(settings.random_mpid_max))
    ids = set()
    while len(ids) < batch_size:
        ids.add(f"mp-{rng.randint(lo, hi)}")
    return list(ids)




def _memory_pool_key(settings: QuizSettings) -> str:
    """Key for in-memory candidate ID pools.

    This intentionally keys only by filters that affect candidate validity.
    It does not key by display-only settings such as energy range.
    """
    return settings_candidate_key(settings)


def _take_from_memory_pool(settings: QuizSettings, count: int) -> List[str]:
    key = _memory_pool_key(settings)
    pool = MEMORY_CANDIDATE_POOLS.get(key, [])
    if not pool:
        return []
    if settings.avoid_recent:
        recent = set(load_recent_history()[: max(1, int(settings.recent_limit))])
        pool = [x for x in pool if x not in recent]
    rng = secrets.SystemRandom()
    rng.shuffle(pool)
    chosen = pool[: max(1, int(count))]
    remaining = [x for x in pool if x not in set(chosen)]
    MEMORY_CANDIDATE_POOLS[key] = remaining
    return chosen


def _add_to_memory_pool(settings: QuizSettings, ids: List[str], limit: Optional[int] = None) -> None:
    key = _memory_pool_key(settings)
    current = MEMORY_CANDIDATE_POOLS.get(key, [])
    merged = list(dict.fromkeys([str(x) for x in current + list(ids) if str(x)]))
    secrets.SystemRandom().shuffle(merged)
    if limit is None:
        limit = max(200, int(settings.fast_pool_target) * 3)
    MEMORY_CANDIDATE_POOLS[key] = merged[: int(limit)]

def search_candidates_by_random_mpid(mpr: MPRester, settings: QuizSettings) -> List[str]:
    """Candidate search with much less element bias.

    Random material IDs are queried in batches.  Invalid IDs simply return no
    documents, while valid ones are filtered by the current quiz conditions.
    This is not perfectly uniform over all MP materials, but it avoids the
    strong element-seed bias that made As/V/Cr appear too frequently.
    """
    rng = secrets.SystemRandom()
    fields = [
        "material_id", "formula_pretty", "formula_anonymous", "symmetry",
        "nsites", "is_metal", "band_gap", "nelements", "is_stable",
        "energy_above_hull", "elements", "has_props",
    ]
    minimal_fields = [
        "material_id", "formula_pretty", "formula_anonymous", "symmetry",
        "nsites", "is_metal", "band_gap", "nelements", "is_stable",
        "energy_above_hull", "elements",
    ]

    batch_size = max(40, min(int(settings.random_mpid_batch_size), 2000))
    rounds = max(1, min(int(settings.random_mpid_rounds), 20))

    docs: List[Any] = []
    for _ in range(rounds):
        mids = _random_material_id_batch(rng, settings, batch_size)
        try:
            docs.extend(list(mpr.materials.summary.search(material_ids=mids, fields=fields)))
        except Exception:
            try:
                docs.extend(list(mpr.materials.summary.search(material_ids=mids, fields=minimal_fields)))
            except Exception:
                continue

    rng.shuffle(docs)
    candidates: List[str] = []
    seen = set()
    for d in docs:
        if not _doc_has_required_band_dos(d, settings):
            continue
        if not summary_doc_matches_filters(d, settings):
            continue
        mid = str(safe_attr(d, "material_id", ""))
        if mid and mid not in seen:
            seen.add(mid)
            candidates.append(mid)
    return choose_candidate_order(candidates, settings)


def search_candidates_by_element_seed(mpr: MPRester, settings: QuizSettings) -> List[str]:
    """Element-seeded candidate search.

    This is retained as an optional/fallback strategy, but it can bias output
    toward certain elements depending on MP summary ordering and filters.

    v13 keeps the v8 UI/flow but fixes the weak randomness by doing several
    independent element-seeded summary searches, merging the results, removing
    duplicates, and shuffling with secrets.SystemRandom.
    """
    if settings.use_candidate_cache:
        cached = load_candidate_pool(settings)
        if cached:
            return choose_candidate_order(cached, settings)

    rng = secrets.SystemRandom()
    fields = [
        "material_id", "formula_pretty", "formula_anonymous", "symmetry",
        "nsites", "is_metal", "band_gap", "nelements", "is_stable",
        "energy_above_hull", "elements", "has_props",
    ]
    minimal_fields = [
        "material_id", "formula_pretty", "formula_anonymous", "symmetry",
        "nsites", "is_metal", "band_gap", "nelements", "is_stable",
        "energy_above_hull", "elements",
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
    if settings.require_band_dos_props:
        base_kwargs["has_props"] = ["bandstructure", "dos"]

    query_kwargs: List[Dict[str, Any]] = []
    if settings.random_element_search:
        seeds = allowed_seed_symbols(settings)
        rng.shuffle(seeds)
        nseed = max(1, min(int(settings.random_element_seed_count), 16))
        for sym in seeds[:nseed]:
            query_kwargs.append({**base_kwargs, "elements": [sym]})
    # Add one broad query as a safety net, but shuffle/merge means it no longer dominates.
    query_kwargs.append(dict(base_kwargs))

    docs: List[Any] = []
    last_exc: Optional[Exception] = None
    for kwargs in query_kwargs:
        try:
            docs.extend(_summary_search_once(mpr, kwargs))
        except Exception as exc:
            last_exc = exc
            try:
                kw2 = dict(kwargs)
                kw2["fields"] = minimal_fields
                kw2.pop("has_props", None)
                docs.extend(list(mpr.materials.summary.search(**kw2)))
            except Exception:
                continue

    if not docs and last_exc is not None:
        raise last_exc

    rng.shuffle(docs)
    candidates: List[str] = []
    seen = set()
    for d in docs:
        if not summary_doc_matches_filters(d, settings):
            continue
        mid = str(safe_attr(d, "material_id", ""))
        if mid and mid not in seen:
            seen.add(mid)
            candidates.append(mid)

    if settings.use_candidate_cache and candidates:
        save_candidate_pool(settings, candidates)
    return choose_candidate_order(candidates, settings)





def search_candidates_fast_pool(mpr: MPRester, settings: QuizSettings) -> List[str]:
    """Fast random candidate search.

    The previous fully-live random search repeated expensive MP summary queries
    on every question.  This function keeps a large in-memory pool of candidate
    mp-ids for the current filters.  It does NOT cache complete quizzes, band
    structures, DOS, answers, or structures.  Each selected mp-id still fetches
    fresh band/DOS data, but candidate discovery becomes much faster after the
    first refill.
    """
    requested = max(20, int(settings.max_trials) * 4)
    from_pool = _take_from_memory_pool(settings, requested)
    if len(from_pool) >= max(8, int(settings.max_trials)):
        return choose_candidate_order(from_pool, settings)

    ids: List[str] = list(from_pool)
    target = max(80, int(settings.fast_pool_target))
    refill_rounds = max(1, min(int(settings.fast_pool_refill_rounds), 8))

    # Refill with mp-id random first to avoid element bias.  If strict filters
    # produce too few valid docs, enrich with element-seeded search.
    for _ in range(refill_rounds):
        try:
            st = settings.model_copy(deep=True)
            st.use_candidate_cache = False
            st.refresh_candidate_pool = True
            st.random_mpid_rounds = max(1, min(int(settings.random_mpid_rounds), 4))
            st.random_mpid_batch_size = max(120, min(int(settings.random_mpid_batch_size), 1200))
            ids.extend(search_candidates_by_random_mpid(mpr, st))
        except Exception:
            pass
        if len(set(ids)) >= target:
            break
        try:
            st = settings.model_copy(deep=True)
            st.use_candidate_cache = False
            st.refresh_candidate_pool = True
            st.random_element_seed_count = max(3, min(int(settings.random_element_seed_count), 10))
            st.candidate_pool_size = max(60, min(int(settings.candidate_pool_size), 250))
            ids.extend(search_candidates_by_element_seed(mpr, st))
        except Exception:
            pass
        if len(set(ids)) >= target:
            break

    ids = list(dict.fromkeys([str(x) for x in ids if str(x)]))
    _add_to_memory_pool(settings, ids, limit=max(target * 3, 300))
    return choose_candidate_order(ids, settings)

def search_candidates_balanced_live(mpr: MPRester, settings: QuizSettings) -> List[str]:
    """Balanced live random search.

    The old pure element-seed search could overproduce familiar elements, while
    pure mp-id random search may return too few documents under strict filters.
    This hybrid ALWAYS mixes both sources, deduplicates, filters, and shuffles.
    It does not reuse full quiz cache, so each question is still live/random.
    """
    rng = secrets.SystemRandom()
    all_ids: List[str] = []

    # 1. Nearly element-neutral random mp-id sampling.
    try:
        all_ids.extend(search_candidates_by_random_mpid(mpr, settings))
    except Exception:
        pass

    # 2. Element-seeded sampling as a fallback/enrichment, but with candidate
    # cache forcibly disabled in this local copy to avoid stale/repeated pools.
    try:
        st = settings.model_copy(deep=True)
        st.use_candidate_cache = False
        st.refresh_candidate_pool = True
        # Use many small independent seeds rather than one big ordered MP page.
        st.random_element_seed_count = max(4, min(int(settings.random_element_seed_count or 6), 12))
        st.candidate_pool_size = max(40, min(int(settings.candidate_pool_size or 180), 220))
        all_ids.extend(search_candidates_by_element_seed(mpr, st))
    except Exception:
        pass

    # 3. Remove duplicates and recent materials, then shuffle with SystemRandom.
    return choose_candidate_order(all_ids, settings)

def search_candidates(mpr: MPRester, settings: QuizSettings) -> List[str]:
    """Dispatch candidate search.

    v15 default is balanced_live: combine random mp-id batches and randomized
    element-seed searches every time.  This keeps the v8-like live feel, reduces
    As/V/Cr-style element bias, and avoids relying on full quiz cache.
    """
    strategy = str(settings.random_strategy or "fast_pool")

    if strategy == "fast_pool":
        return search_candidates_fast_pool(mpr, settings)

    if strategy == "balanced_live":
        return search_candidates_balanced_live(mpr, settings)

    if settings.use_candidate_cache:
        cached = load_candidate_pool(settings)
        if cached:
            return choose_candidate_order(cached, settings)

    candidates: List[str] = []
    if strategy in {"mpid_batch", "hybrid"}:
        candidates = search_candidates_by_random_mpid(mpr, settings)

    if (not candidates) and strategy in {"element_seed", "hybrid"}:
        candidates = search_candidates_by_element_seed(mpr, settings)

    if settings.use_candidate_cache and candidates:
        save_candidate_pool(settings, candidates)
    return choose_candidate_order(candidates, settings)


def search_material_ids(mpr: MPRester, query: str, settings: QuizSettings, limit: int = 25) -> List[str]:
    q = (query or "").strip()
    if not q:
        return []
    if re.fullmatch(r"mp-\d+", q):
        return [q]

    fields = ["material_id", "formula_pretty", "formula_anonymous", "symmetry", "nsites", "is_metal", "band_gap", "nelements", "is_stable", "energy_above_hull", "elements"]
    docs: List[Any] = []

    def add_docs(**kwargs):
        nonlocal docs
        try:
            docs.extend(list(mpr.materials.summary.search(fields=fields, chunk_size=min(max(limit, 20), 100), num_chunks=1, **kwargs)))
        except Exception:
            pass

    # Formula search, e.g. Si, Fe2O3, SrTiO3.
    add_docs(formula=q)
    try:
        comp = Composition(q)
        add_docs(formula=str(comp.reduced_formula))
        elems = [str(el) for el in comp.elements]
        if elems:
            add_docs(elements=elems)
            add_docs(chemsys="-".join(sorted(elems)))
    except Exception:
        pass

    # Element or chemical system search, e.g. O, Fe-O.
    if re.fullmatch(r"[A-Z][a-z]?", q):
        add_docs(elements=[q])
    if "-" in q:
        add_docs(chemsys=q)

    out: List[str] = []
    seen = set()
    for d in docs:
        if not summary_doc_matches_filters(d, settings):
            continue
        mid = str(safe_attr(d, "material_id", ""))
        if mid and mid not in seen:
            seen.add(mid)
            out.append(mid)
        if len(out) >= limit:
            break
    return out




def _fetch_bandstructure(api_key: str, mpid: str, path_type: Any):
    with MPRester(api_key) as client:
        return client.get_bandstructure_by_material_id(mpid, line_mode=True, path_type=path_type)


def _fetch_dos(api_key: str, mpid: str):
    with MPRester(api_key) as client:
        return client.get_dos_by_material_id(mpid)

def generate_quiz_for_mpid(mpr: MPRester, mpid: str, settings: QuizSettings) -> Dict[str, Any]:
    start = time.time()
    path_type = get_path_type(settings.path_type)

    summary_doc = get_summary(mpr, mpid)
    hint = summary_to_hint(summary_doc)

    # Fetch band structure and DOS in parallel.  These two MP API calls are
    # independent and are the main bottleneck, so this noticeably improves
    # perceived random-question speed on Render.
    if getattr(settings, "parallel_fetch", True):
        api_key = get_api_key(settings.api_key)
        with ThreadPoolExecutor(max_workers=2) as ex:
            fut_bs = ex.submit(_fetch_bandstructure, api_key, mpid, path_type)
            fut_dos = ex.submit(_fetch_dos, api_key, mpid)
            bs = fut_bs.result()
            dos = fut_dos.result()
    else:
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
        selected_orbitals=settings.selected_orbitals,
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
    ok, reason = payload_matches_orbital_filters(payload, settings)
    if not ok:
        raise RuntimeError(reason)
    return payload




def relaxed_settings_for_fallback(settings: QuizSettings) -> QuizSettings:
    """Return a render-safe fallback setting.

    The goal is to avoid UI dead ends. We preserve the broad user choices
    (element/compound, metal/nonmetal, stability, nelements) but relax the most
    failure-prone filters: electron-system, selected orbitals, f-DOS missing,
    very large trials, and biased candidate constraints.
    """
    st = settings.model_copy(deep=True)
    st.electron_system = "any"
    st.selected_orbitals = ["s", "p", "d", "f"]
    st.exclude_missing_f_dos = False
    if getattr(st, "electron_system", "any") == "exclude_f_block":
        st.exclude_f_block = True
    # Keep f-block exclusion if user explicitly set it, but otherwise relax.
    # Do not use full quiz cache or background work in fallback.
    st.prefer_cache = False
    st.cache_only = False
    st.save_cache = False
    st.background_prefetch = False
    st.random_strategy = "fast_pool"
    st.fast_pool_target = min(max(int(st.fast_pool_target or 60), 60), 100)
    st.fast_pool_refill_rounds = 1
    st.random_mpid_batch_size = min(max(int(st.random_mpid_batch_size or 120), 80), 240)
    st.random_mpid_rounds = 1
    st.random_element_seed_count = min(max(int(st.random_element_seed_count or 3), 2), 4)
    st.candidate_pool_size = min(max(int(st.candidate_pool_size or 40), 30), 80)
    st.candidate_num_chunks = 1
    st.max_trials = min(max(int(st.max_trials or 5), 4), 8)
    # has_props sometimes narrows too aggressively on MP. Keep it for strict
    # pass, but fallback allows summary candidates and validates band/DOS by
    # actually fetching electronic structure.
    st.require_band_dos_props = False
    return st


def try_generate_from_settings(mpr: MPRester, settings: QuizSettings, max_search_rounds: int = 1) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    errors: List[str] = []
    total_trials = max(1, int(settings.max_trials))
    tried = set()
    for round_index in range(max(1, max_search_rounds)):
        try:
            candidates = search_candidates(mpr, settings)
        except Exception as exc:
            errors.append(f"candidate search round {round_index+1}: {exc}")
            continue
        if not candidates:
            errors.append(f"round {round_index+1}: 条件に合う候補がありません")
            continue
        for mpid in candidates:
            if mpid in tried:
                continue
            tried.add(mpid)
            if len(tried) > total_trials:
                break
            try:
                payload = generate_quiz_for_mpid(mpr, mpid, settings)
                return payload, errors
            except Exception as exc:
                errors.append(f"{mpid}: {exc}")
                continue
    return None, errors

# ============================================================
# API endpoints
# ============================================================

@app.get("/api/health")
def health():
    return {"ok": True, "cache_count": len(load_index()), "version": "18.0-render-stable", "recent_count": len(load_recent_history()), "memory_candidate_pools": {k: len(v) for k, v in MEMORY_CANDIDATE_POOLS.items()}, "memory_quiz_queues": {k: len(v) for k, v in MEMORY_QUIZ_QUEUES.items()}}


@app.post("/api/quiz/new")
def new_quiz(settings: QuizSettings):
    if settings.prefer_cache or settings.cache_only:
        cached = choose_cached(settings)
        if cached is not None:
            return public_payload(cached)
        if settings.cache_only:
            raise HTTPException(status_code=404, detail="条件に合うキャッシュがありません。cache only をOFFにしてください。")

    # Memory queue is only used when explicitly enabled. Default OFF on Render.
    queued = pop_queued_quiz(settings) if (settings.fast_mode and getattr(settings, "background_prefetch", False)) else None
    if queued is not None:
        add_recent_mpid(queued["secret"]["mpid"], settings.recent_limit)
        maybe_refill_quiz_queue(settings, count=1)
        out = public_payload(queued)
        out["from_memory_queue"] = True
        return out

    key = get_api_key(settings.api_key)
    all_errors: List[str] = []
    with MPRester(key) as mpr:
        payload, errors = try_generate_from_settings(mpr, settings, max_search_rounds=1)
        all_errors.extend(errors)
        if payload is not None:
            save_quiz_cache(payload, add_to_index=settings.save_cache)
            add_recent_mpid(payload["secret"]["mpid"], settings.recent_limit)
            maybe_refill_quiz_queue(settings, count=1)
            return public_payload(payload)

        # v18 fallback: avoid dead ends.  Strict orbital/electron filters are the
        # most common cause of failures on Render because many MP electronic
        # structure entries lack a projected channel or fail for one k-path.
        if settings.auto_relax_on_failure:
            relaxed = relaxed_settings_for_fallback(settings)
            payload, relaxed_errors = try_generate_from_settings(mpr, relaxed, max_search_rounds=1)
            all_errors.extend(["fallback: " + e for e in relaxed_errors])
            if payload is not None:
                save_quiz_cache(payload, add_to_index=False)
                add_recent_mpid(payload["secret"]["mpid"], settings.recent_limit)
                out = public_payload(payload)
                out["warning"] = "指定条件では出題候補が見つかりにくかったため、軌道/電子系条件を一部緩和して出題しました。"
                out["relaxed_fallback"] = True
                return out

    detail = "出題候補を取得できませんでした。元素数・電子系・軌道条件を緩めてください。最後の失敗: " + " | ".join(all_errors[-8:])
    # Return JSON with 200-ish shape? Keep HTTP 503 but JSON handler ensures the
    # frontend can show a readable message.
    raise HTTPException(status_code=503, detail=detail)


@app.post("/api/quiz/search")
def quiz_from_search(req: SearchQuizRequest):
    settings = req.settings
    key = get_api_key(settings.api_key)
    errors: List[str] = []
    with MPRester(key) as mpr:
        mpids = search_material_ids(mpr, req.query, settings, limit=max(10, int(settings.max_trials)))
        if not mpids:
            raise HTTPException(status_code=404, detail="検索条件に合う物質が見つかりませんでした。mp-id、化学式、元素記号、または Fe-O のような化学系で検索してください。")
        for mpid in mpids:
            try:
                payload = generate_quiz_for_mpid(mpr, mpid, settings)
                save_quiz_cache(payload, add_to_index=settings.save_cache)
                add_recent_mpid(mpid, settings.recent_limit)
                return public_payload(payload)
            except Exception as exc:
                errors.append(f"{mpid}: {exc}")
                continue
    raise HTTPException(status_code=502, detail="検索候補は見つかりましたが、band/DOS の取得または軌道条件で失敗しました: " + " | ".join(errors[-8:]))


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



@app.head("/")
def head_root():
    # Render health checks may use HEAD.  Returning 200 avoids misleading 405 logs.
    return JSONResponse(content=None, status_code=200)

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
