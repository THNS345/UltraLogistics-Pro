from __future__ import annotations

import hashlib
import hmac
import html as _html
import json
import math
import os
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from matplotlib import patches
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import (
    Image as RLImage,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

import ui_theme as ui  # Apple-style look (same theme as the EAS Estimating Tool)


# ==========================================================
# ULTRALOGISTICS PRO
# Single-file Streamlit logistics / crating planner.
#
# Revision 2026-10-02:
#   - Factory transport rules built in (standard pallet / low-floor
#     pallet without glass / slant rack / split or disassemble).
#   - Vehicle DOOR height is now the loading constraint.
#   - Slant racks lean the unit backwards: the lean uses rack DEPTH.
#   - Pallet load height, crate volume and vehicle floor fit are checked.
#   - Forced disassembly creates frame kits (25 %), manual split 15 %.
#   - First-fit crating, payload-aware container loading, bug fixes.
#
# Run with:  streamlit run ultralogistics_pro.py
# ==========================================================

APP_NAME = "UltraLogistics Pro"
APP_VERSION = "2026-11-03 guided"


# ==========================================================
# 1. CONSTANTS
# ==========================================================

MM_TO_INCH = 1 / 25.4


def mm_to_in(value_mm: float) -> float:
    return float(value_mm) * MM_TO_INCH


def in_to_mm(value_in: float) -> float:
    return float(value_in) * 25.4


# ----------------------------------------------------------
# Factory transport rules (vertical height of the unit AS SHIPPED)
#   <= 2350 mm         : standard certified pallet / crate, glass in unit
#   2351 - 2438 mm     : low-floor pallet, WITHOUT glass; suitable for
#                        transport but NOT for transshipment / handling
#   > 2438 mm          : slanted rack (max unit height 2718 mm) or ship in parts
#   > 2718 mm          : turn on side (if width allows), split or disassemble
# ----------------------------------------------------------
FACTORY_STD_PALLET_MAX_MM = 2350.0
FACTORY_LOW_FLOOR_MAX_MM = 2438.0
FACTORY_SLANT_MAX_MM = 2718.0

# Kept for backwards compatibility with older code/configs.
DEFAULT_FACTORY_SLANT_MAX_H_IN = mm_to_in(FACTORY_SLANT_MAX_MM)

CRATE_SIDE_CLEAR = 2.0       # clearance at each end of crate length
CRATE_BASE_DEPTH = 4.0       # fixed depth allowance (front + back boards)
UNIT_SPACER = 1.0            # spacer between stacked units
PALLET_H = 6.0               # pallet deck height
FRAME_KIT_PROFILE_H = 6.0    # assumed profile height of a frame-kit stick

# Packaging height overheads (base + top) added to the unit's vertical height.
# Chosen so a 40' HC (door 2585 mm, 2" clearance) reproduces the factory bands.
DEFAULT_STD_PACK_OVERHEAD = 6.0
DEFAULT_LOW_FLOOR_PACK_OVERHEAD = 3.5

DEFAULT_CRATE_MAX_LEN_EXT = 630.0
DEFAULT_CRATE_MAX_WIDTH_EXT = 48.0
DEFAULT_SLANT_RACK_MAX_DEPTH_EXT = 88.0
DEFAULT_CRATE_MAX_VOLUME_EXT = 1_000_000.0

DEFAULT_CONTAINER_ITEM_CLEARANCE = 1.0
DEFAULT_HEIGHT_CLEARANCE = 2.0

# Transport classes
CLASS_STD = "STANDARD"
CLASS_LOW = "LOW-FLOOR"
CLASS_SLANT = "SLANT RACK"
CLASS_DIS = "DISASSEMBLED"
CLASS_OVERSIZE = "OVERSIZE"

PACKAGE_TYPE_BY_CLASS = {
    CLASS_STD: "CRATE",
    CLASS_DIS: "CRATE",
    CLASS_LOW: "LOW-FLOOR PALLET",
    CLASS_SLANT: "SLANT RACK",
    CLASS_OVERSIZE: "CRATE",
}

HANDLING_NOTE_BY_CLASS = {
    CLASS_LOW: (
        "Low-floor pallet: unit ships WITHOUT glass (glazing shipped separately). "
        "Transport only, not suitable for transshipment/handling."
    ),
    CLASS_SLANT: "Slant rack: confirm with factory whether glass ships in the unit.",
    CLASS_OVERSIZE: "Exceeds all factory transport options: split or disassemble.",
}

VALID_MODES = {"WHOLE", "SLANT", "DISASSEMBLED"}
VALID_ORIENTS = {"AUTO", "UPRIGHT", "SIDE"}

MASTER_COLUMNS = [
    "Order",
    "ID",
    "Orig",
    "Mark",
    "W",
    "H",
    "Type",
    "Qty",
    "Depth",
    "Lbs",
    "Mode",
    "Orient",
    "SR",
    "SC",
    "Source",
    "Notes",
]

ORDER_COLOR_HEX = [
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
    "#bcbd22",
    "#17becf",
    "#003f5c",
    "#58508d",
    "#bc5090",
    "#ff6361",
    "#ffa600",
]

# H = interior height, door_H = door opening height (loading constraint).
# Verify door heights with the carrier; they vary by box and trailer.
DEFAULT_CONTAINERS: Dict[str, Dict[str, float]] = {
    "40' HC Container": {
        "L": 473.0,
        "W": 92.0,
        "H": 105.0,
        "door_H": round(mm_to_in(2585), 1),
        "max_lbs": 44000.0,
    },
    "40' Standard": {
        "L": 473.0,
        "W": 92.0,
        "H": 94.0,
        "door_H": round(mm_to_in(2280), 1),
        "max_lbs": 44000.0,
    },
    "53' Dry Van": {
        "L": 636.0,
        "W": 100.0,
        "H": 110.0,
        "door_H": 108.0,
        "max_lbs": 45000.0,
    },
    "20' Standard": {
        "L": 232.0,
        "W": 92.0,
        "H": 94.0,
        "door_H": round(mm_to_in(2280), 1),
        "max_lbs": 28000.0,
    },
}

DEFAULT_PALLETS: Dict[str, Dict[str, float]] = {
    "US GMA (48x40)": {
        "L": 48.0,
        "W": 40.0,
        "H": PALLET_H,
        "max_lbs": 2200.0,
    },
    "Euro 2 (1200x1000mm)": {
        "L": round(mm_to_in(1200), 2),
        "W": round(mm_to_in(1000), 2),
        "H": PALLET_H,
        "max_lbs": 2200.0,
    },
    "Factory (2200x1000mm)": {
        "L": round(mm_to_in(2200), 2),
        "W": round(mm_to_in(1000), 2),
        "H": PALLET_H,
        "max_lbs": 2200.0,
    },
    "Oversize (96x48)": {
        "L": 96.0,
        "W": 48.0,
        "H": PALLET_H,
        "max_lbs": 3000.0,
    },
}

SCENARIO_CURRENT = "Current Settings"
SCENARIO_SLANT = "Slant Instead of Low-Floor / Oversize"
SCENARIO_DISASSEMBLE = "Disassemble Oversized Units"
SCENARIO_SPLIT = "Split Oversized Units"

SCENARIO_OPTIONS = [
    SCENARIO_CURRENT,
    SCENARIO_SLANT,
    SCENARIO_DISASSEMBLE,
    SCENARIO_SPLIT,
]


# ==========================================================
# 2. SMALL UTILITY HELPERS
# ==========================================================

def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def today_file_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def is_blank(value: Any) -> bool:
    """True for None, NaN/NaT/pd.NA and empty strings."""
    if value is None:
        return True

    if isinstance(value, str):
        return value.strip() == ""

    if not pd.api.types.is_scalar(value):
        return False

    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def clean_str(value: Any) -> str:
    if is_blank(value):
        return ""
    return str(value).strip()


_UNIT_SUFFIX_RE = re.compile(r'(?i)\s*(inches|inch|in|")\s*$')


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if is_blank(value):
        return default

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "")
        text = _UNIT_SUFFIX_RE.sub("", text).strip()

        if text == "":
            return default

        try:
            number = float(text)
        except (TypeError, ValueError):
            return default

    if math.isnan(number) or math.isinf(number):
        return default

    return number


def safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    number = safe_float(value, None)
    if number is None:
        return default
    return int(round(number))


def slugify(value: str, fallback: str = "load-plan") -> str:
    value = clean_str(value)
    value = re.sub(r"[^\w\s\-]+", "", value)
    value = re.sub(r"\s+", "-", value)
    value = value.strip("-_")
    return value or fallback


def inches_text(value: float) -> str:
    return f'{value:.1f}"'


def inches_mm_text(value: float) -> str:
    return f'{value:.1f}" ({in_to_mm(value):,.0f} mm)'


def pounds_text(value: float) -> str:
    return f"{value:,.0f} lbs"


def dim_text(length: float, width: float, height: Optional[float] = None) -> str:
    if height is None:
        return f'{length:.0f}" x {width:.0f}"'
    return f'{length:.0f}" x {width:.0f}" x {height:.0f}"'


def hex_to_rgba(hex_color: str, alpha: float) -> str:
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def build_order_color_map(order_names: List[str]) -> Dict[str, str]:
    unique_names = sorted(set(x for x in order_names if clean_str(x)))
    return {
        name: ORDER_COLOR_HEX[i % len(ORDER_COLOR_HEX)]
        for i, name in enumerate(unique_names)
    }


def json_download_bytes(data: Dict[str, Any]) -> bytes:
    return json.dumps(data, indent=2, default=str).encode("utf-8")


# ==========================================================
# 3. DATA MODELS
# ==========================================================

@dataclass
class ProjectMeta:
    project_name: str = ""
    customer: str = ""
    project_location: str = ""
    destination: str = ""
    factory: str = ""
    system: str = ""
    estimator: str = ""
    estimator_email: str = ""
    quote_or_job_ref: str = ""
    revision: str = ""
    notes: str = ""

    def display_name(self) -> str:
        return self.project_name or "Untitled Project"


@dataclass
class LogisticsAssumptions:
    glass_kg_m2: float = 30.0
    std_weight_multiplier: float = 1.35
    lsd_weight_multiplier: float = 1.40

    # Share of unit weight that goes into the frame kit.
    frame_kit_pct_disassembly: float = 0.25   # DISASSEMBLED, no split
    frame_kit_pct_split: float = 0.15         # DISASSEMBLED with a row/column split

    max_crate_lbs: float = 2500.0
    max_pallet_lbs: float = 2200.0

    crate_max_len_ext: float = DEFAULT_CRATE_MAX_LEN_EXT
    crate_max_width_ext: float = DEFAULT_CRATE_MAX_WIDTH_EXT
    slant_rack_max_depth_ext: float = DEFAULT_SLANT_RACK_MAX_DEPTH_EXT
    crate_max_volume_ext: float = DEFAULT_CRATE_MAX_VOLUME_EXT

    vehicle_height_clearance: float = DEFAULT_HEIGHT_CLEARANCE
    container_item_clearance: float = DEFAULT_CONTAINER_ITEM_CLEARANCE

    # Factory transport rules (mm, vertical height as shipped)
    factory_std_max_mm: float = FACTORY_STD_PALLET_MAX_MM
    factory_low_floor_max_mm: float = FACTORY_LOW_FLOOR_MAX_MM
    factory_slant_max_mm: float = FACTORY_SLANT_MAX_MM
    allow_low_floor: bool = True

    std_pack_overhead: float = DEFAULT_STD_PACK_OVERHEAD
    low_floor_pack_overhead: float = DEFAULT_LOW_FLOOR_PACK_OVERHEAD

    no_mixing_orders: bool = True
    allow_pallets_for_disassembled: bool = True

    planning_warning: str = (
        "Planning layout only. Final loading, blocking, bracing, route limits, "
        "and carrier requirements must be verified by logistics/freight team."
    )


@dataclass
class TransportLimits:
    """
    All height/size limits for one vehicle + assumption set.

    vehicle_limit_h : min(interior height, door height)
    usable_ext_h    : max external package height (vehicle_limit_h - clearance)
    std_max_v       : max unit vertical on a standard pallet/crate
    low_floor_max_v : max unit vertical on a low-floor pallet (no glass)
    slant_max_v     : max unit vertical that may go on a slant rack
    slant_target_v  : rack height a slanted unit is leaned down to
    """
    vehicle_L: float
    vehicle_W: float
    vehicle_limit_h: float
    usable_ext_h: float
    std_max_v: float
    low_floor_max_v: float
    slant_max_v: float
    slant_target_v: float
    std_overhead: float
    low_overhead: float
    floor_clearance: float

    def max_for_class(self, transport_class: str) -> float:
        if transport_class == CLASS_STD:
            return self.std_max_v
        if transport_class == CLASS_LOW:
            return self.low_floor_max_v
        if transport_class == CLASS_SLANT:
            return self.slant_max_v
        if transport_class == CLASS_DIS:
            return self.std_max_v
        return 0.0

    def fits_floor(self, length: float, width: float) -> bool:
        c = self.floor_clearance
        return (
            (length + c <= self.vehicle_L and width + c <= self.vehicle_W)
            or (width + c <= self.vehicle_L and length + c <= self.vehicle_W)
        )


def vehicle_limit_height(vehicle_data: Dict[str, float]) -> float:
    interior = float(vehicle_data["H"])
    door = safe_float(vehicle_data.get("door_H"), None)
    if door is None or door <= 0:
        return interior
    return min(interior, door)


def get_transport_limits(
    vehicle_data: Dict[str, float],
    a: LogisticsAssumptions,
) -> TransportLimits:
    limit_h = vehicle_limit_height(vehicle_data)
    usable = max(1.0, limit_h - a.vehicle_height_clearance)

    std_cap = max(1.0, usable - a.std_pack_overhead)
    low_cap = max(1.0, usable - a.low_floor_pack_overhead)

    std_max = min(mm_to_in(a.factory_std_max_mm), std_cap)

    if a.allow_low_floor:
        low_max = max(std_max, min(mm_to_in(a.factory_low_floor_max_mm), low_cap))
    else:
        low_max = std_max

    slant_target = std_cap
    slant_max = max(mm_to_in(a.factory_slant_max_mm), 0.0)

    return TransportLimits(
        vehicle_L=float(vehicle_data["L"]),
        vehicle_W=float(vehicle_data["W"]),
        vehicle_limit_h=limit_h,
        usable_ext_h=usable,
        std_max_v=std_max,
        low_floor_max_v=low_max,
        slant_max_v=slant_max,
        slant_target_v=slant_target,
        std_overhead=a.std_pack_overhead,
        low_overhead=a.low_floor_pack_overhead,
        floor_clearance=a.container_item_clearance,
    )


@dataclass
class ValidationIssue:
    severity: str
    source: str
    row_no: Optional[int]
    order: str
    item_id: str
    problem: str
    suggestion: str
    raw_value: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Piece:
    orig_id: str
    piece_id: str
    w: float
    h: float
    utype: str
    d: float
    lbs: float
    mode: str = "WHOLE"
    orientation: str = "AUTO"
    source_order: str = ""

    # ------------------------------------------------------
    # Transport plan
    # ------------------------------------------------------
    def plan(self, lim: TransportLimits) -> Tuple[str, str]:
        """
        Returns (orientation, transport_class).

        orientation:
            UPRIGHT  vertical = height
            SIDE     vertical = width
            ON EDGE  disassembled part standing on its shorter edge
        """
        if self.mode == "DISASSEMBLED":
            vertical = min(self.w, self.h)
            cls = CLASS_DIS if vertical <= lim.std_max_v else CLASS_OVERSIZE
            return "ON EDGE", cls

        if self.orientation in ("UPRIGHT", "SIDE"):
            allowed = [self.orientation]
        else:
            allowed = ["UPRIGHT", "SIDE"]

        def vertical_of(orient: str) -> float:
            return self.w if orient == "SIDE" else self.h

        if self.mode == "SLANT":
            for orient in allowed:
                if vertical_of(orient) <= lim.slant_max_v:
                    return orient, CLASS_SLANT
            return allowed[0], CLASS_OVERSIZE

        # WHOLE: automatic choice, in factory preference order.
        preference = [
            ("UPRIGHT", CLASS_STD),
            ("SIDE", CLASS_STD),
            ("UPRIGHT", CLASS_LOW),
            ("UPRIGHT", CLASS_SLANT),
            ("SIDE", CLASS_LOW),
            ("SIDE", CLASS_SLANT),
        ]

        for orient, cls in preference:
            if orient not in allowed:
                continue
            if cls == CLASS_LOW and lim.low_floor_max_v <= lim.std_max_v:
                continue
            if vertical_of(orient) <= lim.max_for_class(cls):
                return orient, cls

        return allowed[0], CLASS_OVERSIZE

    def transport_class(self, lim: TransportLimits) -> str:
        return self.plan(lim)[1]

    def resolved_orientation(self, lim: TransportLimits) -> str:
        return self.plan(lim)[0]

    def vertical(self, lim: TransportLimits) -> float:
        """Unit dimension that stands vertical (before any slanting)."""
        orient = self.resolved_orientation(lim)
        if orient == "ON EDGE":
            return min(self.w, self.h)
        if orient == "SIDE":
            return self.w
        return self.h

    def base(self, lim: TransportLimits) -> float:
        """Unit dimension that runs along the crate length."""
        orient = self.resolved_orientation(lim)
        if orient == "ON EDGE":
            return max(self.w, self.h)
        if orient == "SIDE":
            return self.h
        return self.w

    def stack_thickness(self) -> float:
        return self.d + UNIT_SPACER


@dataclass
class Crate:
    """
    A crate, low-floor pallet or slant rack holding units stacked side by side.

    Length axis : unit base (width of the unit)
    Depth axis  : stack of unit thicknesses (+ slant lean for racks)
    Height      : unit vertical (+ packaging overhead)
    """
    order: str = ""
    pclass: str = CLASS_STD
    pieces: List[Piece] = field(default_factory=list)
    weight: float = 0.0
    problem: str = ""

    @property
    def package_type(self) -> str:
        return PACKAGE_TYPE_BY_CLASS.get(self.pclass, "CRATE")

    def slant_geometry(self, lim: TransportLimits, pieces: Optional[List[Piece]] = None) -> Tuple[float, float]:
        """
        Returns (cos_theta, max_horizontal_run) for a slant rack.

        All units on a rack share the lean angle set by the tallest unit,
        which is leaned down to the rack height (slant_target_v).
        """
        pieces = self.pieces if pieces is None else pieces
        if self.pclass != CLASS_SLANT or not pieces:
            return 1.0, 0.0

        tallest = max(p.vertical(lim) for p in pieces)
        target = lim.slant_target_v

        if tallest <= target:
            return 1.0, 0.0

        cos_t = target / tallest
        run = math.sqrt(max(0.0, tallest ** 2 - target ** 2))
        return cos_t, run

    def dims(self, lim: TransportLimits, pieces: Optional[List[Piece]] = None) -> Tuple[float, float, float]:
        """External (L, W, H) for the given pieces (defaults to current contents)."""
        pieces = self.pieces if pieces is None else pieces
        if not pieces:
            return 0.0, 0.0, 0.0

        length = max(p.base(lim) for p in pieces) + 2 * CRATE_SIDE_CLEAR
        stack = sum(p.stack_thickness() for p in pieces)

        if self.pclass == CLASS_SLANT:
            cos_t, run = self.slant_geometry(lim, pieces)
            depth = CRATE_BASE_DEPTH + stack / cos_t + run
            height_used = max(p.vertical(lim) for p in pieces) * cos_t
            overhead = lim.std_overhead
        else:
            depth = CRATE_BASE_DEPTH + stack
            height_used = max(p.vertical(lim) for p in pieces)
            overhead = lim.low_overhead if self.pclass == CLASS_LOW else lim.std_overhead

        return length, depth, height_used + overhead

    def limit_violations(
        self,
        lim: TransportLimits,
        a: LogisticsAssumptions,
        pieces: Optional[List[Piece]] = None,
        weight: Optional[float] = None,
    ) -> List[str]:
        pieces = self.pieces if pieces is None else pieces
        weight = self.weight if weight is None else weight
        length, depth, height = self.dims(lim, pieces)

        max_depth = (
            a.slant_rack_max_depth_ext
            if self.pclass == CLASS_SLANT
            else a.crate_max_width_ext
        )

        issues: List[str] = []

        if weight > a.max_crate_lbs:
            issues.append("OVER CRATE WEIGHT")
        if length > a.crate_max_len_ext:
            issues.append("OVER CRATE LENGTH")
        if depth > max_depth:
            issues.append("OVER RACK DEPTH" if self.pclass == CLASS_SLANT else "OVER CRATE WIDTH")
        if height > lim.usable_ext_h + 1e-6:
            issues.append("OVER VEHICLE/DOOR HEIGHT")
        if length * depth * height > a.crate_max_volume_ext:
            issues.append("OVER CRATE VOLUME")
        if not lim.fits_floor(length, depth):
            issues.append("DOES NOT FIT VEHICLE FLOOR")

        return issues

    def can_add(self, p: Piece, lim: TransportLimits, a: LogisticsAssumptions) -> Tuple[bool, str]:
        trial = self.pieces + [p]
        issues = self.limit_violations(lim, a, trial, self.weight + p.lbs)
        if issues:
            return False, "; ".join(issues).lower()
        return True, "fits"

    def add(self, p: Piece) -> None:
        self.pieces.append(p)
        self.weight += p.lbs


@dataclass
class PalletObject:
    """
    Pallet holding disassembled parts standing on edge in rows.
    Footprint per part = long edge x (thickness + spacer).
    """
    order: str
    name: str
    L: float
    W: float
    H: float
    max_wgt: float
    pieces: List[Piece] = field(default_factory=list)
    weight: float = 0.0
    load_h: float = 0.0
    uL: float = 0.0
    rW: float = 0.0
    tW: float = 0.0

    @property
    def H_ext(self) -> float:
        """Deck + tallest part + top clearance."""
        if not self.pieces:
            return self.H
        return self.H + self.load_h + CRATE_SIDE_CLEAR

    def footprint(self, p: Piece) -> Optional[Tuple[float, float]]:
        base = max(p.w, p.h)
        thick = p.stack_thickness()

        if base <= self.L and thick <= self.W:
            return base, thick
        if base <= self.W and thick <= self.L:
            return thick, base
        return None

    def place(self, p: Piece, lim: TransportLimits) -> bool:
        dims = self.footprint(p)
        if not dims:
            return False

        if self.weight + p.lbs > self.max_wgt:
            return False

        part_h = min(p.w, p.h)
        if self.H + max(self.load_h, part_h) + CRATE_SIDE_CLEAR > lim.usable_ext_h:
            return False

        pL, pW = dims

        if self.uL + pL <= self.L and self.tW + max(self.rW, pW) <= self.W:
            self.uL += pL
            self.rW = max(self.rW, pW)
        else:
            if self.tW + self.rW + pW > self.W:
                return False
            self.tW += self.rW
            self.uL = pL
            self.rW = pW

        self.pieces.append(p)
        self.weight += p.lbs
        self.load_h = max(self.load_h, part_h)
        return True


# ==========================================================
# 4. WEIGHT / DEPTH / SPLIT LOGIC
# ==========================================================

def is_lsd_type(unit_type: Any) -> bool:
    text = clean_str(unit_type).upper()
    keywords = ["LSD", "LIFT", "SLID", "SLIDE", "MULTISLIDE", "MULTI-SLIDE"]
    return any(keyword in text for keyword in keywords)


def calculate_specs(
    unit_type: str,
    w: float,
    h: float,
    glass_kg_m2: float,
    std_multiplier: float,
    lsd_multiplier: float,
) -> Tuple[float, float]:
    """
    Returns (depth_inches, estimated_weight_lbs).

    Depth: LSD / sliding 200 mm, other units 90 mm.
    Weight: glass area x kg/m2 x total-weight multiplier.
    """
    is_lsd = is_lsd_type(unit_type)
    depth = mm_to_in(200) if is_lsd else mm_to_in(90)

    area_m2 = (w * 0.0254) * (h * 0.0254)
    multiplier = lsd_multiplier if is_lsd else std_multiplier
    weight_lbs = area_m2 * glass_kg_m2 * multiplier * 2.20462

    return depth, weight_lbs


def expand_manual_split(
    p: Piece,
    rows: int,
    cols: int,
    a: LogisticsAssumptions,
) -> List[Piece]:
    """
    Splits one unit into rows x cols parts.

    WHOLE / SLANT : parts keep the mode; the transport class is chosen
                    per part from the factory rules.
    DISASSEMBLED  : panel parts carry the non-frame weight and four
                    frame-kit sticks are added.
                    No split  -> forced disassembly, frame kit 25 %
                    Split     -> manual split, frame kit 15 %
    """
    rows = max(1, int(rows or 1))
    cols = max(1, int(cols or 1))
    part_count = rows * cols

    p.mode = clean_str(p.mode).upper() or "WHOLE"
    p.orientation = clean_str(p.orientation).upper() or "AUTO"

    if p.mode != "DISASSEMBLED" and part_count == 1:
        return [p]

    if p.mode == "DISASSEMBLED":
        frame_pct = a.frame_kit_pct_disassembly if part_count == 1 else a.frame_kit_pct_split
    else:
        frame_pct = 0.0

    kit_weight = p.lbs * frame_pct
    panel_weight_each = max(0.0, p.lbs - kit_weight) / part_count

    part_w = p.w / cols
    part_h = p.h / rows

    parts: List[Piece] = []

    for i in range(part_count):
        parts.append(
            Piece(
                orig_id=p.orig_id,
                piece_id=f"{p.piece_id}:P{i + 1}",
                w=part_w,
                h=part_h,
                utype=f"{p.utype} (Part)",
                d=p.d,
                lbs=panel_weight_each,
                mode=p.mode,
                orientation=p.orientation,
                source_order=p.source_order,
            )
        )

    if p.mode == "DISASSEMBLED" and kit_weight > 0:
        stick_weight = kit_weight / 4.0

        for suffix in ["V1", "V2", "H1", "H2"]:
            stick_len = p.h if suffix.startswith("V") else p.w

            parts.append(
                Piece(
                    orig_id=p.orig_id,
                    piece_id=f"{p.piece_id}:KIT_{suffix}",
                    w=stick_len,
                    h=FRAME_KIT_PROFILE_H,
                    utype="FRAME KIT",
                    d=p.d,
                    lbs=stick_weight,
                    mode="DISASSEMBLED",
                    orientation="AUTO",
                    source_order=p.source_order,
                )
            )

    return parts


# ==========================================================
# 5. RAW PASTE PARSER
# ==========================================================

def make_master_row(
    order_name: str,
    item_id: str,
    index: int,
    width: float,
    height: float,
    unit_type: str,
    depth: float,
    lbs: float,
    source: str,
    notes: str = "",
) -> Dict[str, Any]:
    return {
        "Order": order_name,
        "ID": f"{order_name}|{item_id}#{index}",
        "Orig": f"{order_name}|{item_id}",
        "Mark": item_id,
        "W": round(width, 3),
        "H": round(height, 3),
        "Type": unit_type,
        "Qty": 1,
        "Depth": round(depth, 3),
        "Lbs": round(lbs, 1),
        "Mode": "WHOLE",
        "Orient": "AUTO",
        "SR": 1,
        "SC": 1,
        "Source": source,
        "Notes": notes,
    }


def parse_order_text_to_rows(
    order_name: str,
    raw_text: str,
    assumptions: LogisticsAssumptions,
) -> Tuple[List[Dict[str, Any]], List[ValidationIssue]]:
    """
    Expected paste format:  ID, W, H, Type, Qty
    Delimiters: comma, tab, or two or more spaces.
    """
    rows: List[Dict[str, Any]] = []
    issues: List[ValidationIssue] = []

    if not clean_str(raw_text):
        return rows, issues

    lines = [line.rstrip() for line in raw_text.splitlines() if clean_str(line)]

    def issue(line_no: int, item_id: str, problem: str, suggestion: str, line: str, severity: str = "ERROR") -> None:
        issues.append(
            ValidationIssue(
                severity=severity,
                source="Raw Paste",
                row_no=line_no,
                order=order_name,
                item_id=item_id,
                problem=problem,
                suggestion=suggestion,
                raw_value=line,
            )
        )

    for line_no, line in enumerate(lines, start=1):
        parts = re.split(r"\s*,\s*|\t+|\s{2,}", line.strip())

        if len(parts) < 5:
            issue(line_no, "", "Too few columns. Expected ID, W, H, Type, Qty.",
                  "Use format like: A1, 36, 72, FIXED, 2", line)
            continue

        if len(parts) > 5:
            item_id_raw, width_raw, height_raw = parts[0], parts[1], parts[2]
            qty_raw = parts[-1]
            type_raw = " ".join(parts[3:-1])
        else:
            item_id_raw, width_raw, height_raw, type_raw, qty_raw = parts[:5]

        item_id = clean_str(item_id_raw)
        width = safe_float(width_raw)
        height = safe_float(height_raw)
        qty = safe_int(qty_raw)
        unit_type = clean_str(type_raw)

        if not item_id:
            issue(line_no, "", "Missing item ID / mark.", "Add a unit ID or mark.", line)
            continue
        if width is None or width <= 0:
            issue(line_no, item_id, "Width is missing, zero, or not numeric.", "Enter width in inches.", line)
            continue
        if height is None or height <= 0:
            issue(line_no, item_id, "Height is missing, zero, or not numeric.", "Enter height in inches.", line)
            continue
        if qty is None or qty <= 0:
            issue(line_no, item_id, "Quantity is missing, zero, or not numeric.",
                  "Enter quantity as a positive whole number.", line)
            continue

        if not unit_type:
            unit_type = "STANDARD"
            issue(line_no, item_id, "Type is blank.", "Default STANDARD assumptions were used.", line, "WARNING")

        depth, lbs = calculate_specs(
            unit_type, width, height,
            assumptions.glass_kg_m2,
            assumptions.std_weight_multiplier,
            assumptions.lsd_weight_multiplier,
        )

        for i in range(qty):
            rows.append(
                make_master_row(order_name, item_id, i + 1, width, height, unit_type, depth, lbs, "Raw Paste")
            )

    return rows, issues


# ==========================================================
# 6. EXCEL / CSV UPLOAD NORMALIZATION
# ==========================================================

def read_uploaded_dataframe(uploaded_file) -> pd.DataFrame:
    file_name = uploaded_file.name.lower()

    if file_name.endswith(".csv"):
        return pd.read_csv(uploaded_file)

    if file_name.endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(uploaded_file)

    raise ValueError("Unsupported file type. Upload CSV, XLSX, XLSM, or XLS.")


def _norm_header(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def guess_column(
    columns: List[str],
    keywords: List[str],
    exclude: Optional[List[str]] = None,
) -> str:
    """
    Guesses a source column from keywords, in keyword priority order.

    Pass 1: exact header match.
    Pass 2: header contains keyword (keywords of 3+ characters only,
            so "w" or "id" cannot match "Window" or "Width").
    """
    exclude = exclude or []
    normalized = {col: _norm_header(col) for col in columns if col not in exclude}

    for keyword in keywords:
        key = _norm_header(keyword)
        for col, simple in normalized.items():
            if key and simple == key:
                return col

    for keyword in keywords:
        key = _norm_header(keyword)
        if len(key) < 3:
            continue
        for col, simple in normalized.items():
            if key in simple:
                return col

    return "<none>"


def build_default_column_mapping(columns: List[str]) -> Dict[str, str]:
    width = guess_column(columns, ["width", "w", "imperial width", "si width", "wid"])
    height = guess_column(columns, ["height", "h", "imperial height", "si height", "hgt"], exclude=[width])
    taken = [c for c in [width, height] if c != "<none>"]

    unit_id = guess_column(columns, ["mark", "type mark", "id", "unit id", "item", "position", "pos"], exclude=taken)
    taken.append(unit_id)

    qty = guess_column(columns, ["qty", "quantity", "count", "pcs"], exclude=taken)
    taken.append(qty)

    unit_type = guess_column(columns, ["type", "unit type", "system", "description"], exclude=taken)
    taken.append(unit_type)

    return {
        "id": unit_id,
        "width": width,
        "height": height,
        "type": unit_type,
        "qty": qty,
        "depth": guess_column(columns, ["depth", "frame depth"], exclude=taken),
        "weight": guess_column(columns, ["weight", "lbs", "pounds"], exclude=taken),
        "notes": guess_column(columns, ["notes", "remarks", "comments"], exclude=taken),
    }


def normalize_uploaded_table(
    df: pd.DataFrame,
    order_name: str,
    mapping: Dict[str, str],
    assumptions: LogisticsAssumptions,
    source_name: str,
) -> Tuple[List[Dict[str, Any]], List[ValidationIssue]]:
    """
    Converts uploaded CSV/Excel rows into master rows.
    Required: id, width, height. Optional: type, qty, depth, weight, notes.
    """
    rows: List[Dict[str, Any]] = []
    issues: List[ValidationIssue] = []

    def cell(row: pd.Series, logical_name: str) -> Any:
        col = mapping.get(logical_name, "<none>")
        if col == "<none>" or col not in df.columns:
            return None
        return row[col]

    def issue(row_no: int, item_id: str, problem: str, suggestion: str, raw: str) -> None:
        issues.append(
            ValidationIssue(
                severity="ERROR",
                source=source_name,
                row_no=row_no,
                order=order_name,
                item_id=item_id,
                problem=problem,
                suggestion=suggestion,
                raw_value=raw,
            )
        )

    for position, (_, row) in enumerate(df.iterrows()):
        row_no = position + 2  # header is spreadsheet row 1

        values = [clean_str(x) for x in row.tolist()]
        if not any(values):
            continue  # fully blank spreadsheet row

        raw_preview = " | ".join(values[:10])

        item_id = clean_str(cell(row, "id"))
        width = safe_float(cell(row, "width"))
        height = safe_float(cell(row, "height"))
        unit_type = clean_str(cell(row, "type")) or "STANDARD"
        notes = clean_str(cell(row, "notes"))

        qty_raw = cell(row, "qty")
        qty = 1 if is_blank(qty_raw) else safe_int(qty_raw)

        if not item_id:
            issue(row_no, "", "Missing ID / mark.", "Map the correct ID/mark column or fill missing marks.", raw_preview)
            continue
        if width is None or width <= 0:
            issue(row_no, item_id, "Width is missing, zero, or not numeric.",
                  "Map width column or enter width in inches.", raw_preview)
            continue
        if height is None or height <= 0:
            issue(row_no, item_id, "Height is missing, zero, or not numeric.",
                  "Map height column or enter height in inches.", raw_preview)
            continue
        if qty is None or qty <= 0:
            issue(row_no, item_id, "Quantity is zero, negative, or not numeric.",
                  "Use a positive whole-number quantity.", raw_preview)
            continue

        calc_depth, calc_lbs = calculate_specs(
            unit_type, width, height,
            assumptions.glass_kg_m2,
            assumptions.std_weight_multiplier,
            assumptions.lsd_weight_multiplier,
        )

        mapped_depth = safe_float(cell(row, "depth"))
        mapped_weight = safe_float(cell(row, "weight"))

        depth = mapped_depth if mapped_depth and mapped_depth > 0 else calc_depth
        lbs = mapped_weight if mapped_weight and mapped_weight > 0 else calc_lbs

        for i in range(qty):
            rows.append(
                make_master_row(order_name, item_id, i + 1, width, height, unit_type, depth, lbs, source_name, notes)
            )

    return rows, issues


# ==========================================================
# 7. MASTER TABLE NORMALIZATION & VALIDATION
# ==========================================================

def normalize_master_df(
    df: Optional[pd.DataFrame],
    assumptions: LogisticsAssumptions,
) -> pd.DataFrame:
    """
    Cleans an edited master table so it can be packed:

    - fills defaults for rows added in the editor (Order, Mark, Mode,
      Orient, SR, SC, Type, Source)
    - recalculates blank Depth / Lbs from W, H and Type
    - generates a unique ID / Orig for rows where they are blank
    """
    if df is None:
        return pd.DataFrame(columns=MASTER_COLUMNS)

    out = df.copy()
    for col in MASTER_COLUMNS:
        if col not in out.columns:
            out[col] = None
    out = out[MASTER_COLUMNS].reset_index(drop=True)
    out = out.astype(object)

    # Drop rows that are completely empty (e.g. an added row never filled in).
    meaningful = ["Mark", "W", "H", "ID"]
    keep = [
        any(not is_blank(out.at[i, c]) for c in meaningful)
        for i in range(len(out))
    ]
    out = out[keep].reset_index(drop=True)

    existing_ids = set(clean_str(x) for x in out["ID"] if clean_str(x))

    for i in range(len(out)):
        order = clean_str(out.at[i, "Order"]) or "Manual"
        out.at[i, "Order"] = order

        unit_id = clean_str(out.at[i, "ID"])
        mark = clean_str(out.at[i, "Mark"])

        if not mark and unit_id:
            mark = unit_id.split("|", 1)[-1].split("#", 1)[0]
        if not mark:
            mark = f"ROW{i + 1}"
        out.at[i, "Mark"] = mark

        unit_type = clean_str(out.at[i, "Type"]) or "STANDARD"
        out.at[i, "Type"] = unit_type

        mode = clean_str(out.at[i, "Mode"]).upper() or "WHOLE"
        orient = clean_str(out.at[i, "Orient"]).upper() or "AUTO"
        out.at[i, "Mode"] = mode
        out.at[i, "Orient"] = orient

        out.at[i, "SR"] = safe_int(out.at[i, "SR"], 1)
        out.at[i, "SC"] = safe_int(out.at[i, "SC"], 1)
        out.at[i, "Qty"] = 1
        out.at[i, "Source"] = clean_str(out.at[i, "Source"]) or "Manual Edit"
        out.at[i, "Notes"] = clean_str(out.at[i, "Notes"])

        width = safe_float(out.at[i, "W"])
        height = safe_float(out.at[i, "H"])
        out.at[i, "W"] = width
        out.at[i, "H"] = height

        depth = safe_float(out.at[i, "Depth"])
        lbs = safe_float(out.at[i, "Lbs"])

        if width and height and width > 0 and height > 0:
            calc_depth, calc_lbs = calculate_specs(
                unit_type, width, height,
                assumptions.glass_kg_m2,
                assumptions.std_weight_multiplier,
                assumptions.lsd_weight_multiplier,
            )
            if depth is None or depth <= 0:
                depth = round(calc_depth, 3)
            if lbs is None or lbs <= 0:
                lbs = round(calc_lbs, 1)

        out.at[i, "Depth"] = depth
        out.at[i, "Lbs"] = lbs

        if not clean_str(out.at[i, "Orig"]):
            out.at[i, "Orig"] = f"{order}|{mark}"

        if not unit_id:
            k = 1
            candidate = f"{order}|{mark}#{k}"
            while candidate in existing_ids:
                k += 1
                candidate = f"{order}|{mark}#{k}"
            out.at[i, "ID"] = candidate
            existing_ids.add(candidate)

    return out


def validation_issues_to_df(issues: List[ValidationIssue]) -> pd.DataFrame:
    if not issues:
        return pd.DataFrame()
    return pd.DataFrame([issue.to_dict() for issue in issues])


def validate_master_dataframe(df: pd.DataFrame) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []

    def add(row_no: Optional[int], order: str, item_id: str, problem: str, suggestion: str) -> None:
        issues.append(
            ValidationIssue(
                severity="ERROR",
                source="Master Table",
                row_no=row_no,
                order=order,
                item_id=item_id,
                problem=problem,
                suggestion=suggestion,
            )
        )

    if df is None or df.empty:
        add(None, "", "", "No unit data loaded.", "Process pasted data or upload a CSV/Excel file.")
        return issues

    required_columns = ["Order", "ID", "Orig", "W", "H", "Type", "Depth", "Lbs", "Mode", "Orient", "SR", "SC"]

    for col in required_columns:
        if col not in df.columns:
            add(None, "", "", f"Missing required column: {col}", "Reload/process the source data.")

    if issues:
        return issues

    seen_ids: set = set()

    for position, (_, row) in enumerate(df.iterrows()):
        row_no = position + 1
        order = clean_str(row.get("Order"))
        item_id = clean_str(row.get("ID"))

        if not item_id:
            add(row_no, order, "", "Generated unit ID is blank.", "Save the table to auto-generate IDs.")
        elif item_id in seen_ids:
            add(row_no, order, item_id, "Duplicate generated unit ID.", "Check order names and source marks.")
        else:
            seen_ids.add(item_id)

        for col in ["W", "H", "Depth", "Lbs"]:
            value = safe_float(row.get(col))
            if value is None or value <= 0:
                add(row_no, order, item_id, f"{col} must be positive numeric.", f"Correct {col} before optimizing.")

        if clean_str(row.get("Mode")).upper() not in VALID_MODES:
            add(row_no, order, item_id, "Invalid Mode.", "Use WHOLE, SLANT, or DISASSEMBLED.")

        if clean_str(row.get("Orient")).upper() not in VALID_ORIENTS:
            add(row_no, order, item_id, "Invalid Orient.", "Use AUTO, UPRIGHT, or SIDE.")

        sr = safe_int(row.get("SR"), 1)
        sc = safe_int(row.get("SC"), 1)
        if sr is None or sr < 1 or sc is None or sc < 1:
            add(row_no, order, item_id, "Invalid split rows/columns.", "SR and SC must be at least 1.")

    return issues


# ==========================================================
# 8. BUILD PIECES FROM MASTER TABLE
# ==========================================================

def row_to_piece(row: pd.Series) -> Optional[Piece]:
    """Builds the base Piece for one master row, or None if the row is invalid."""
    width = safe_float(row.get("W"))
    height = safe_float(row.get("H"))
    depth = safe_float(row.get("Depth"))
    lbs = safe_float(row.get("Lbs"))

    if not all(v is not None and v > 0 for v in [width, height, depth, lbs]):
        return None

    mode = clean_str(row.get("Mode")).upper() or "WHOLE"
    orient = clean_str(row.get("Orient")).upper() or "AUTO"

    if mode not in VALID_MODES or orient not in VALID_ORIENTS:
        return None

    return Piece(
        orig_id=clean_str(row.get("Orig")),
        piece_id=clean_str(row.get("ID")),
        w=float(width),
        h=float(height),
        utype=clean_str(row.get("Type")),
        d=float(depth),
        lbs=float(lbs),
        mode=mode,
        orientation=orient,
        source_order=clean_str(row.get("Order")),
    )


def glass_note(transport_class: str, utype: str) -> str:
    if utype == "FRAME KIT":
        return "N/A"
    if transport_class == CLASS_LOW:
        return "SHIP SEPARATELY"
    if transport_class == CLASS_SLANT:
        return "IN UNIT (confirm)"
    if transport_class == CLASS_OVERSIZE:
        return "-"
    return "IN UNIT"


def build_pieces_from_master(
    df: pd.DataFrame,
    vehicle_data: Dict[str, float],
    assumptions: LogisticsAssumptions,
) -> Tuple[List[Piece], pd.DataFrame]:
    """
    Converts the master table into final pieces.
    Returns (pieces, packing_decision_df). Invalid rows are skipped.
    """
    lim = get_transport_limits(vehicle_data, assumptions)

    pieces: List[Piece] = []
    decision_rows: List[Dict[str, Any]] = []

    for _, row in df.iterrows():
        base_piece = row_to_piece(row)
        if base_piece is None:
            continue

        split_rows = safe_int(row.get("SR"), 1) or 1
        split_cols = safe_int(row.get("SC"), 1) or 1

        expanded = expand_manual_split(base_piece, split_rows, split_cols, assumptions)
        pieces.extend(expanded)

        for p in expanded:
            orient, cls = p.plan(lim)
            single = Crate(order=p.source_order, pclass=cls if cls != CLASS_DIS else CLASS_STD)
            single.add(p)
            pkg_l, pkg_w, pkg_h = single.dims(lim)

            decision_rows.append(
                {
                    "Order": p.source_order,
                    "Original Unit": base_piece.piece_id,
                    "Final Piece": p.piece_id,
                    "Mode": p.mode,
                    "Orientation": orient,
                    "Transport Class": cls,
                    "Glass": glass_note(cls, p.utype),
                    "W": round(p.w, 1),
                    "H": round(p.h, 1),
                    "Vertical H": round(p.vertical(lim), 1),
                    "Vertical mm": round(in_to_mm(p.vertical(lim))),
                    "Depth": round(p.d, 2),
                    "Lbs": round(p.lbs, 1),
                    "Split Rows": split_rows,
                    "Split Cols": split_cols,
                    "Single-Unit Pkg L": round(pkg_l, 1),
                    "Single-Unit Pkg W": round(pkg_w, 1),
                    "Single-Unit Pkg H": round(pkg_h, 1),
                }
            )

    return pieces, pd.DataFrame(decision_rows)


# ==========================================================
# 9. PRACTICAL UNIT ISSUE REPORT
# ==========================================================

def build_unit_issue_report(
    df: pd.DataFrame,
    vehicle_data: Dict[str, float],
    assumptions: LogisticsAssumptions,
) -> pd.DataFrame:
    """
    Per-piece report using the SAME rules as the optimizer, so it
    reflects the current Mode / Orient / split settings.

    Severity:
      ERROR    cannot ship as set (oversize, crate/vehicle limits)
      WARNING  ships, but with factory restrictions (low-floor, no glass)
      INFO     ships, worth knowing (slant rack, turned on side)
    """
    if df is None or df.empty:
        return pd.DataFrame()

    lim = get_transport_limits(vehicle_data, assumptions)
    pieces, _ = build_pieces_from_master(df, vehicle_data, assumptions)

    rows: List[Dict[str, Any]] = []

    for p in pieces:
        orient, cls = p.plan(lim)
        vertical = p.vertical(lim)

        problems: List[str] = []
        recs: List[str] = []
        severity = ""

        if cls == CLASS_OVERSIZE:
            severity = "ERROR"
            if p.mode == "DISASSEMBLED":
                problems.append(
                    f'Disassembled part {min(p.w, p.h):.1f}" still taller than standard limit {lim.std_max_v:.1f}"'
                )
                recs.append("Increase split rows/columns (SR/SC)")
            else:
                problems.append(
                    f'Vertical {vertical:.1f}" ({in_to_mm(vertical):,.0f} mm) exceeds every factory option '
                    f'(standard {lim.std_max_v:.1f}", low-floor {lim.low_floor_max_v:.1f}", '
                    f'slant {lim.slant_max_v:.1f}")'
                )
                recs.append("Split (SR/SC) or set Mode = DISASSEMBLED")
        elif cls == CLASS_LOW:
            severity = "WARNING"
            problems.append("Low-floor pallet: ships WITHOUT glass; not suitable for transshipment/handling")
            recs.append("Glazing ships separately; confirm with factory, or split / slant instead")
        elif cls == CLASS_SLANT:
            severity = "INFO"
            problems.append(f'Ships on slant rack (vertical {vertical:.1f}" leaned to {lim.slant_target_v:.1f}")')
            recs.append("Confirm with factory whether glass ships in the unit")
        elif orient == "SIDE" and p.orientation == "AUTO":
            severity = "INFO"
            problems.append("Turned on its side to fit standard height")
            recs.append("Confirm the unit may ship on its side")

        if cls != CLASS_OVERSIZE:
            single = Crate(order=p.source_order, pclass=cls if cls != CLASS_DIS else CLASS_STD)
            single.add(p)
            violations = single.limit_violations(lim, assumptions)
            if violations:
                severity = "ERROR"
                problems.append("Single-unit package: " + ", ".join(violations).lower())
                recs.append("Split or disassemble, or review crate limits / vehicle")
            pkg = single.dims(lim)
        else:
            pkg = (0.0, 0.0, 0.0)

        if not problems:
            continue

        rows.append(
            {
                "Severity": severity,
                "Order": p.source_order,
                "Unit": p.orig_id,
                "Piece": p.piece_id,
                "W": round(p.w, 1),
                "H": round(p.h, 1),
                "Mode": p.mode,
                "Orientation": orient,
                "Transport Class": cls,
                "Vertical H": round(vertical, 1),
                "Vertical mm": round(in_to_mm(vertical)),
                "Lbs": round(p.lbs, 0),
                "Pkg L": round(pkg[0], 1),
                "Pkg W": round(pkg[1], 1),
                "Pkg H": round(pkg[2], 1),
                "Problem": "; ".join(dict.fromkeys(problems)),
                "Recommendation": "; ".join(dict.fromkeys(recs)),
            }
        )

    report = pd.DataFrame(rows)
    if not report.empty:
        order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
        report = report.sort_values(
            by="Severity", key=lambda s: s.map(order), kind="stable"
        ).reset_index(drop=True)
    return report


def show_issue_summary(issue_df: pd.DataFrame) -> None:
    if issue_df is None or issue_df.empty:
        st.success("No dimensional/weight issues detected for the selected vehicle.")
        return

    counts = issue_df["Severity"].value_counts()
    if counts.get("ERROR", 0):
        st.error(f"{counts['ERROR']} piece(s) cannot ship as set.")
    if counts.get("WARNING", 0):
        st.warning(f"{counts['WARNING']} piece(s) ship with factory restrictions (low-floor, no glass).")
    if counts.get("INFO", 0):
        st.info(f"{counts['INFO']} piece(s) ship on a slant rack or on their side.")

    st.dataframe(issue_df, **ui.STRETCH, hide_index=True)


# ==========================================================
# 10. PALLETIZE AND CRATE PIECES
# ==========================================================

def group_pieces_for_packing(pieces: List[Piece], no_mixing_orders: bool) -> Dict[str, List[Piece]]:
    grouped: Dict[str, List[Piece]] = {}
    for p in pieces:
        key = p.source_order if no_mixing_orders else "MIXED ORDERS"
        grouped.setdefault(key, []).append(p)
    return grouped


def palletize_disassembled_pieces(
    order_name: str,
    pieces: List[Piece],
    selected_pallet_names: List[str],
    pallet_catalog: Dict[str, Dict[str, float]],
    lim: TransportLimits,
    assumptions: LogisticsAssumptions,
) -> Tuple[List[PalletObject], List[Piece]]:
    """
    Places DISASSEMBLED parts (standing on edge) on pallets, first fit.
    Returns (pallets, leftovers that go to crates).
    """
    if not assumptions.allow_pallets_for_disassembled or not selected_pallet_names:
        return [], list(pieces)

    pallets: List[PalletObject] = []
    leftovers: List[Piece] = []

    for p in sorted(pieces, key=lambda x: x.w * x.h, reverse=True):
        if any(pallet.place(p, lim) for pallet in pallets):
            continue

        placed = False
        for pallet_name in selected_pallet_names:
            spec = pallet_catalog[pallet_name]
            new_pallet = PalletObject(
                order=order_name,
                name=pallet_name,
                L=float(spec["L"]),
                W=float(spec["W"]),
                H=float(spec.get("H", PALLET_H)),
                max_wgt=min(float(spec.get("max_lbs", assumptions.max_pallet_lbs)), assumptions.max_pallet_lbs),
            )
            if new_pallet.place(p, lim):
                pallets.append(new_pallet)
                placed = True
                break

        if not placed:
            leftovers.append(p)

    return pallets, leftovers


def crate_pieces(
    order_name: str,
    pieces: List[Piece],
    lim: TransportLimits,
    assumptions: LogisticsAssumptions,
) -> Tuple[List[Crate], List[Dict[str, Any]]]:
    """
    First-fit-decreasing crating.

    - Pieces only share a package with pieces of the same transport class
      (standard crate, low-floor pallet, slant rack).
    - Each piece goes into the first open package of its class that still
      meets every limit; otherwise a new package is opened.
    - Pieces that cannot meet the limits even alone get their own
      flagged problem package so they stay visible in the manifest.
    """
    crates: List[Crate] = []
    notes: List[Dict[str, Any]] = []

    sorted_pieces = sorted(
        pieces,
        key=lambda x: (x.vertical(lim), x.base(lim), x.lbs),
        reverse=True,
    )

    for p in sorted_pieces:
        cls = p.transport_class(lim)

        if cls == CLASS_OVERSIZE:
            problem = Crate(order=order_name, pclass=CLASS_OVERSIZE, problem="OVERSIZE: split or disassemble")
            problem.add(p)
            crates.append(problem)
            notes.append({"Piece": p.piece_id, "Class": cls, "Assigned": "Problem package",
                          "Reason": "exceeds all factory transport options"})
            continue

        package_class = CLASS_STD if cls == CLASS_DIS else cls
        target = None

        for c in crates:
            if c.problem or c.pclass != package_class:
                continue
            ok, _ = c.can_add(p, lim, assumptions)
            if ok:
                target = c
                break

        if target is not None:
            target.add(p)
            notes.append({"Piece": p.piece_id, "Class": cls, "Assigned": "Existing package", "Reason": "fits"})
            continue

        new_crate = Crate(order=order_name, pclass=package_class)
        ok, reason = new_crate.can_add(p, lim, assumptions)

        if not ok:
            new_crate.problem = reason.upper()
            notes.append({"Piece": p.piece_id, "Class": cls, "Assigned": "Problem package",
                          "Reason": f"does not meet limits alone: {reason}"})
        else:
            notes.append({"Piece": p.piece_id, "Class": cls, "Assigned": "New package",
                          "Reason": "no open package of this class had room"})

        new_crate.add(p)
        crates.append(new_crate)

    return crates, notes


# ==========================================================
# 11. 2D CONTAINER FLOOR PACKING
# ==========================================================

def pack_items_2d_maxrects(
    items: List[Dict[str, Any]],
    container_L: float,
    container_W: float,
    clearance: float,
    max_weight: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Max-rects floor packing (best short side fit, no stacking).
    Items that would push the load over max_weight are left for the next vehicle.
    Each placed item gets x, y, L, W (as placed) and rotated=True/False.
    """
    placed: List[Dict[str, Any]] = []
    overflow: List[Dict[str, Any]] = []
    load_weight = 0.0

    free_rects: List[Dict[str, float]] = [{"x": 0.0, "y": 0.0, "L": container_L, "W": container_W}]

    sorted_items = sorted(
        [dict(item) for item in items],
        key=lambda x: float(x["L"]) * float(x["W"]),
        reverse=True,
    )

    for item in sorted_items:
        item_weight = float(item.get("weight", 0) or 0)

        if max_weight and max_weight > 0 and load_weight + item_weight > max_weight:
            overflow.append(item)
            continue

        best_fit = None
        best_short_side = float("inf")

        req_L = float(item["L"]) + clearance
        req_W = float(item["W"]) + clearance

        orientations = [
            (req_L, req_W, float(item["L"]), float(item["W"]), False),
            (req_W, req_L, float(item["W"]), float(item["L"]), True),
        ]

        for fr in free_rects:
            for used_L, used_W, actual_L, actual_W, rotated in orientations:
                if used_L <= fr["L"] + 1e-9 and used_W <= fr["W"] + 1e-9:
                    short_side = min(fr["L"] - used_L, fr["W"] - used_W)
                    if short_side < best_short_side:
                        best_short_side = short_side
                        best_fit = {
                            "x": fr["x"], "y": fr["y"],
                            "used_L": used_L, "used_W": used_W,
                            "actual_L": actual_L, "actual_W": actual_W,
                            "rotated": rotated,
                        }

        if best_fit is None:
            overflow.append(item)
            continue

        placed_item = dict(item)
        placed_item.update(
            {
                "x": best_fit["x"],
                "y": best_fit["y"],
                "L": best_fit["actual_L"],
                "W": best_fit["actual_W"],
                "rotated": best_fit["rotated"],
            }
        )
        placed.append(placed_item)
        load_weight += item_weight

        ux, uy = best_fit["x"], best_fit["y"]
        uL, uW = best_fit["used_L"], best_fit["used_W"]

        new_free: List[Dict[str, float]] = []

        for fr in free_rects:
            no_overlap = (
                ux >= fr["x"] + fr["L"]
                or ux + uL <= fr["x"]
                or uy >= fr["y"] + fr["W"]
                or uy + uW <= fr["y"]
            )
            if no_overlap:
                new_free.append(fr)
                continue

            if ux > fr["x"]:
                new_free.append({"x": fr["x"], "y": fr["y"], "L": ux - fr["x"], "W": fr["W"]})
            if ux + uL < fr["x"] + fr["L"]:
                new_free.append({"x": ux + uL, "y": fr["y"], "L": fr["x"] + fr["L"] - (ux + uL), "W": fr["W"]})
            if uy > fr["y"]:
                new_free.append({"x": fr["x"], "y": fr["y"], "L": fr["L"], "W": uy - fr["y"]})
            if uy + uW < fr["y"] + fr["W"]:
                new_free.append({"x": fr["x"], "y": uy + uW, "L": fr["L"], "W": fr["y"] + fr["W"] - (uy + uW)})

        # Drop slivers and rectangles fully contained in another.
        new_free = [r for r in new_free if r["L"] > 1.0 and r["W"] > 1.0]
        pruned: List[Dict[str, float]] = []
        for i, r in enumerate(new_free):
            contained = False
            for j, o in enumerate(new_free):
                if i == j:
                    continue
                if (
                    r["x"] >= o["x"] and r["y"] >= o["y"]
                    and r["x"] + r["L"] <= o["x"] + o["L"]
                    and r["y"] + r["W"] <= o["y"] + o["W"]
                    and (r != o or j < i)
                ):
                    contained = True
                    break
            if not contained:
                pruned.append(r)
        free_rects = pruned

    return placed, overflow


def pack_across_multiple_containers(
    items: List[Dict[str, Any]],
    vehicle_name: str,
    vehicle_data: Dict[str, float],
    assumptions: LogisticsAssumptions,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Fills vehicles one after another.

    Returns (loads, unpacked). Packages that can never be loaded
    (too tall for the door, too big for the floor, heavier than the
    payload) go straight to unpacked, with a reason, and never create
    an empty vehicle.
    """
    lim = get_transport_limits(vehicle_data, assumptions)
    container_L = float(vehicle_data["L"])
    container_W = float(vehicle_data["W"])
    payload = float(vehicle_data.get("max_lbs", 0) or 0)

    loadable: List[Dict[str, Any]] = []
    unpacked: List[Dict[str, Any]] = []

    for item in items:
        reasons = []
        if float(item.get("H", 0)) > lim.usable_ext_h + 1e-6:
            reasons.append("taller than vehicle/door limit")
        if not lim.fits_floor(float(item["L"]), float(item["W"])):
            reasons.append("footprint larger than vehicle floor")
        if payload > 0 and float(item.get("weight", 0)) > payload:
            reasons.append("heavier than vehicle payload")

        if reasons:
            bad = dict(item)
            bad["unpacked_reason"] = "; ".join(reasons)
            unpacked.append(bad)
        else:
            loadable.append(dict(item))

    loads: List[Dict[str, Any]] = []
    remaining = loadable
    floor_area = container_L * container_W

    for container_no in range(1, 501):
        if not remaining:
            break

        placed, overflow = pack_items_2d_maxrects(
            remaining, container_L, container_W, assumptions.container_item_clearance, payload or None,
        )

        if not placed:
            for item in overflow:
                item["unpacked_reason"] = "could not be placed"
            unpacked.extend(overflow)
            break

        load_weight = sum(float(x.get("weight", 0)) for x in placed)
        used_area = sum(float(x["L"]) * float(x["W"]) for x in placed)

        loads.append(
            {
                "vehicle_name": vehicle_name,
                "container_no": container_no,
                "placed": placed,
                "overflow": overflow,
                "util": used_area / floor_area * 100.0 if floor_area > 0 else 0.0,
                "weight": load_weight,
                "payload_over": payload > 0 and load_weight > payload,
                "payload_limit": payload,
            }
        )

        remaining = overflow

    return loads, unpacked


# ==========================================================
# 12. MAIN OPTIMIZATION / PLAN BUILDER
# ==========================================================

def build_package_contents_map(pallets: List[PalletObject], crates: List[Crate]) -> Dict[str, str]:
    assignment: Dict[str, str] = {}
    for i, pallet in enumerate(pallets):
        for p in pallet.pieces:
            assignment[p.piece_id] = f"P{i + 1}"
    for i, crate in enumerate(crates):
        for p in crate.pieces:
            assignment[p.piece_id] = f"C{i + 1}"
    return assignment


def build_manifest_df(
    pallets: List[PalletObject],
    crates: List[Crate],
    lim: TransportLimits,
    assumptions: LogisticsAssumptions,
) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []

    for i, pallet in enumerate(pallets):
        status_items = []
        if pallet.weight > pallet.max_wgt:
            status_items.append("OVER PALLET WEIGHT")
        if pallet.H_ext > lim.usable_ext_h + 1e-6:
            status_items.append("OVER VEHICLE/DOOR HEIGHT")

        rows.append(
            {
                "Order": pallet.order,
                "ID": f"P{i + 1}",
                "Type": "PALLET",
                "Transport Class": CLASS_DIS,
                "Pieces": len(pallet.pieces),
                "Weight": round(pallet.weight, 0),
                "Dims": dim_text(pallet.L, pallet.W, pallet.H_ext),
                "L": round(pallet.L, 1),
                "W": round(pallet.W, 1),
                "H": round(pallet.H_ext, 1),
                "Status": "; ".join(status_items) or "OK",
                "Handling": f"{pallet.name}; parts on edge",
                "Contents": ", ".join(p.piece_id for p in pallet.pieces),
            }
        )

    for i, crate in enumerate(crates):
        length, depth, height = crate.dims(lim)

        status_items: List[str] = []
        if crate.problem:
            status_items.append(crate.problem)
        status_items.extend(
            v for v in crate.limit_violations(lim, assumptions) if v not in " ".join(status_items)
        )

        rows.append(
            {
                "Order": crate.order,
                "ID": f"C{i + 1}",
                "Type": crate.package_type,
                "Transport Class": crate.pclass,
                "Pieces": len(crate.pieces),
                "Weight": round(crate.weight, 0),
                "Dims": dim_text(length, depth, height),
                "L": round(length, 1),
                "W": round(depth, 1),
                "H": round(height, 1),
                "Status": "; ".join(dict.fromkeys(status_items)) or "OK",
                "Handling": HANDLING_NOTE_BY_CLASS.get(crate.pclass, ""),
                "Contents": ", ".join(p.piece_id for p in crate.pieces),
            }
        )

    return pd.DataFrame(rows)


def build_container_items_from_manifest(manifest_df: pd.DataFrame) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if manifest_df is None or manifest_df.empty:
        return items

    for _, row in manifest_df.iterrows():
        items.append(
            {
                "kind": clean_str(row["Type"]),
                "package_id": clean_str(row["ID"]),
                "transport_class": clean_str(row["Transport Class"]),
                "idx": len(items),
                "L": float(row["L"]),
                "W": float(row["W"]),
                "H": float(row["H"]),
                "weight": float(row["Weight"]),
                "order": clean_str(row["Order"]),
                "status": clean_str(row["Status"]),
            }
        )
    return items


def build_load_summary_df(loads: List[Dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for load in loads:
        placed = load.get("placed", [])
        rows.append(
            {
                "Container #": load["container_no"],
                "Vehicle": load["vehicle_name"],
                "Packages Loaded": len(placed),
                "Packages Remaining After This Container": len(load.get("overflow", [])),
                "Weight": round(float(load.get("weight", 0)), 0),
                "Payload Limit": round(float(load.get("payload_limit", 0)), 0),
                "Payload Status": "OVER PAYLOAD" if load.get("payload_over") else "OK",
                "Floor Utilization %": round(float(load.get("util", 0)), 1),
                "Packages": ", ".join(clean_str(x.get("package_id")) for x in placed),
            }
        )
    return pd.DataFrame(rows)


def assign_packages_to_decision_df(decision_df: pd.DataFrame, package_map: Dict[str, str]) -> pd.DataFrame:
    if decision_df.empty:
        return decision_df
    out = decision_df.copy()
    out["Assigned Package"] = out["Final Piece"].map(lambda pid: package_map.get(pid, "UNASSIGNED"))
    return out


def build_logistics_plan(
    master_df: pd.DataFrame,
    vehicle_name: str,
    vehicle_data: Dict[str, float],
    selected_pallet_names: List[str],
    pallet_catalog: Dict[str, Dict[str, float]],
    assumptions: LogisticsAssumptions,
) -> Dict[str, Any]:
    """
    End-to-end optimizer:
      1. normalize + validate the master table
      2. expand rows into final pieces (splits, frame kits)
      3. classify each piece by factory transport rules
      4. palletize disassembled parts, crate/rack everything else
      5. load packages into one or more vehicles
    """
    master_df = normalize_master_df(master_df, assumptions)
    validation_issues = validate_master_dataframe(master_df)

    if any(issue.severity == "ERROR" for issue in validation_issues):
        return {
            "ok": False,
            "errors": validation_issues_to_df(validation_issues),
            "message": "Fix validation errors before optimizing.",
        }

    lim = get_transport_limits(vehicle_data, assumptions)
    pieces, decision_df = build_pieces_from_master(master_df, vehicle_data, assumptions)

    all_pallets: List[PalletObject] = []
    all_crates: List[Crate] = []
    packing_notes: List[Dict[str, Any]] = []

    for group_name, group_pieces in group_pieces_for_packing(pieces, assumptions.no_mixing_orders).items():
        disassembled = [p for p in group_pieces if p.transport_class(lim) == CLASS_DIS]
        others = [p for p in group_pieces if p.transport_class(lim) != CLASS_DIS]

        pallets, leftovers = palletize_disassembled_pieces(
            group_name, disassembled, selected_pallet_names, pallet_catalog, lim, assumptions,
        )
        all_pallets.extend(pallets)

        for pallet in pallets:
            for p in pallet.pieces:
                packing_notes.append({"Piece": p.piece_id, "Class": CLASS_DIS, "Assigned": "Pallet",
                                      "Reason": pallet.name, "Group": group_name})

        crates, crate_notes = crate_pieces(group_name, others + leftovers, lim, assumptions)
        all_crates.extend(crates)

        for note in crate_notes:
            note["Group"] = group_name
            packing_notes.append(note)

    package_map = build_package_contents_map(all_pallets, all_crates)
    decision_df = assign_packages_to_decision_df(decision_df, package_map)
    manifest_df = build_manifest_df(all_pallets, all_crates, lim, assumptions)

    loads, unpacked = pack_across_multiple_containers(
        build_container_items_from_manifest(manifest_df), vehicle_name, vehicle_data, assumptions,
    )

    return {
        "ok": True,
        "message": "Optimization complete.",
        "vehicle_name": vehicle_name,
        "vehicle_data": vehicle_data,
        "assumptions": assumptions,
        "limits": lim,
        "pieces": pieces,
        "pallets": all_pallets,
        "crates": all_crates,
        "loads": loads,
        "manifest_df": manifest_df,
        "decision_df": decision_df,
        "packing_notes_df": pd.DataFrame(packing_notes),
        "load_summary_df": build_load_summary_df(loads),
        "order_color_map": build_order_color_map(list(master_df["Order"].astype(str).unique())),
        "final_overflow": unpacked,
        "validation_issues_df": validation_issues_to_df(validation_issues),
    }


def plan_class_counts(plan: Dict[str, Any]) -> Dict[str, int]:
    """Number of final pieces per transport class."""
    lim: TransportLimits = plan["limits"]
    counts: Dict[str, int] = {}
    for p in plan["pieces"]:
        cls = p.transport_class(lim)
        counts[cls] = counts.get(cls, 0) + 1
    return counts


# ==========================================================
# 13. SCENARIO COMPARISON
# ==========================================================

def create_scenario_master_df(
    base_df: pd.DataFrame,
    scenario_name: str,
    vehicle_data: Dict[str, float],
    assumptions: LogisticsAssumptions,
) -> pd.DataFrame:
    """
    Applies a handling strategy to rows that do not ship on a standard
    pallet as currently set.

      Current Settings                      : unchanged
      Slant Instead of Low-Floor / Oversize : SLANT for low-floor/oversize
                                              rows up to the slant limit
                                              (keeps glass in the unit)
      Disassemble Oversized Units           : DISASSEMBLED (+ split rows
                                              if the part is still too tall)
      Split Oversized Units                 : split rows/cols until parts
                                              fit standard height and crate
                                              weight
    """
    df = normalize_master_df(base_df, assumptions)
    if df.empty or scenario_name == SCENARIO_CURRENT:
        return df

    lim = get_transport_limits(vehicle_data, assumptions)

    for idx, row in df.iterrows():
        piece = row_to_piece(row)
        if piece is None:
            continue

        sr = safe_int(row.get("SR"), 1) or 1
        sc = safe_int(row.get("SC"), 1) or 1
        if sr * sc > 1:
            continue  # user already decided how to split this unit

        cls = piece.transport_class(lim)
        heavy = piece.lbs > assumptions.max_crate_lbs
        needs_split_rows = max(1, math.ceil(piece.h / lim.std_max_v))

        if scenario_name == SCENARIO_SLANT:
            if cls in (CLASS_LOW, CLASS_OVERSIZE) and min(piece.w, piece.h) <= lim.slant_max_v:
                df.at[idx, "Mode"] = "SLANT"

        elif scenario_name == SCENARIO_DISASSEMBLE:
            if cls in (CLASS_LOW, CLASS_OVERSIZE) or heavy:
                df.at[idx, "Mode"] = "DISASSEMBLED"
                if min(piece.w, piece.h) > lim.std_max_v:
                    df.at[idx, "SR"] = needs_split_rows

        elif scenario_name == SCENARIO_SPLIT:
            if cls in (CLASS_LOW, CLASS_OVERSIZE) or heavy:
                df.at[idx, "Mode"] = "WHOLE"
                df.at[idx, "Orient"] = "UPRIGHT"
                df.at[idx, "SR"] = needs_split_rows if cls in (CLASS_LOW, CLASS_OVERSIZE) else 1
                if heavy:
                    df.at[idx, "SC"] = max(1, math.ceil(piece.lbs / assumptions.max_crate_lbs))

    return df


def summarize_plan_for_scenario(scenario_name: str, vehicle_name: str, plan: Dict[str, Any]) -> Dict[str, Any]:
    if not plan.get("ok"):
        return {
            "Scenario": scenario_name,
            "Vehicle": vehicle_name,
            "Status": "ERROR",
            "Containers": None,
            "Crates/Racks": None,
            "Pallets": None,
            "Low-Floor Pieces (no glass)": None,
            "Slant Pieces": None,
            "Total Weight": None,
            "Avg Floor Util %": None,
            "Unpacked Items": None,
            "Warnings": plan.get("message", "Could not optimize"),
        }

    loads = plan["loads"]
    manifest_df = plan["manifest_df"]
    unpacked = plan["final_overflow"]
    counts = plan_class_counts(plan)

    total_weight = float(manifest_df["Weight"].astype(float).sum()) if not manifest_df.empty else 0.0
    avg_util = sum(float(ld["util"]) for ld in loads) / len(loads) if loads else 0.0
    bad_packages = int((manifest_df["Status"].astype(str) != "OK").sum()) if not manifest_df.empty else 0

    warnings = []
    if bad_packages:
        warnings.append(f"{bad_packages} package issue(s)")
    if unpacked:
        warnings.append(f"{len(unpacked)} unpacked package(s)")
    if counts.get(CLASS_LOW):
        warnings.append(f"{counts[CLASS_LOW]} low-floor piece(s) ship without glass")

    status = "OK"
    if bad_packages or unpacked:
        status = "REVIEW"
    elif counts.get(CLASS_LOW):
        status = "OK - RESTRICTIONS"

    return {
        "Scenario": scenario_name,
        "Vehicle": vehicle_name,
        "Status": status,
        "Containers": len(loads),
        "Crates/Racks": len(plan["crates"]),
        "Pallets": len(plan["pallets"]),
        "Low-Floor Pieces (no glass)": counts.get(CLASS_LOW, 0),
        "Slant Pieces": counts.get(CLASS_SLANT, 0),
        "Total Weight": round(total_weight, 0),
        "Avg Floor Util %": round(avg_util, 1),
        "Unpacked Items": len(unpacked),
        "Warnings": "; ".join(warnings) if warnings else "None",
    }


def run_scenario_comparison(
    master_df: pd.DataFrame,
    vehicle_catalog: Dict[str, Dict[str, float]],
    selected_vehicle_names: List[str],
    selected_pallet_names: List[str],
    pallet_catalog: Dict[str, Dict[str, float]],
    assumptions: LogisticsAssumptions,
    selected_strategy_names: List[str],
) -> pd.DataFrame:
    rows = []
    for vehicle_name in selected_vehicle_names:
        vehicle_data = vehicle_catalog[vehicle_name]
        for strategy_name in selected_strategy_names:
            scenario_df = create_scenario_master_df(master_df, strategy_name, vehicle_data, assumptions)
            plan = build_logistics_plan(
                scenario_df, vehicle_name, vehicle_data, selected_pallet_names, pallet_catalog, assumptions,
            )
            rows.append(summarize_plan_for_scenario(strategy_name, vehicle_name, plan))
    return pd.DataFrame(rows)


# ==========================================================
# 14. VISUALIZATION HELPERS
# ==========================================================

def package_fill_alpha(kind: str) -> float:
    return 0.55 if kind == "PALLET" else 0.35


def build_2d_plan(
    placed_items: List[Dict[str, Any]],
    container_L: float,
    container_W: float,
    order_color_map: Dict[str, str],
    title_text: str = "",
) -> go.Figure:
    fig = go.Figure()

    fig.add_shape(
        type="rect", x0=0, y0=0, x1=container_L, y1=container_W,
        line=dict(color="black", width=4), fillcolor="rgba(240,240,240,0.5)",
    )

    for item in placed_items:
        order = clean_str(item.get("order"))
        base_hex = order_color_map.get(order, "#888888")

        fig.add_shape(
            type="rect",
            x0=item["x"], y0=item["y"],
            x1=item["x"] + item["L"], y1=item["y"] + item["W"],
            fillcolor=hex_to_rgba(base_hex, package_fill_alpha(item["kind"])),
            line=dict(color="black", width=2),
        )

        label = clean_str(item.get("package_id"))
        hover_text = (
            f"Package: {label}<br>"
            f"Type: {clean_str(item.get('kind'))}<br>"
            f"Class: {clean_str(item.get('transport_class'))}<br>"
            f"Order: {order}<br>"
            f"Dims: {dim_text(float(item['L']), float(item['W']), float(item.get('H', 0)))}<br>"
            f"Weight: {pounds_text(float(item.get('weight', 0)))}<br>"
            f"Status: {clean_str(item.get('status'))}"
        )

        fig.add_trace(
            go.Scatter(
                x=[item["x"] + item["L"] / 2],
                y=[item["y"] + item["W"] / 2],
                mode="text",
                text=[f"<b>{label}</b>"],
                textfont=dict(color="black", size=11),
                hovertext=[hover_text],
                hoverinfo="text",
                showlegend=False,
            )
        )

    fig.update_layout(
        title=title_text,
        xaxis=dict(range=[-10, container_L + 10], title="Length (in)"),
        yaxis=dict(range=[-10, container_W + 10], title="Width (in)", scaleanchor="x"),
        height=520,
        margin=dict(l=20, r=20, t=50, b=20),
    )
    return ui.style_fig(fig)


def render_mpl_2d_plan(
    placed_items: List[Dict[str, Any]],
    container_L: float,
    container_W: float,
    order_color_map: Dict[str, str],
    title_text: str = "",
) -> BytesIO:
    """Matplotlib renderer for the PDF (no Kaleido/Chrome dependency)."""
    fig = Figure(figsize=(10, 4))
    ax = fig.add_subplot(111)

    ax.add_patch(
        patches.Rectangle((0, 0), container_L, container_W, linewidth=2, edgecolor="black", facecolor="#f9f9f9")
    )

    for item in placed_items:
        base_hex = order_color_map.get(clean_str(item.get("order")), "#888888")
        ax.add_patch(
            patches.Rectangle(
                (item["x"], item["y"]), item["L"], item["W"],
                linewidth=1, edgecolor="black", facecolor=base_hex,
                alpha=package_fill_alpha(item["kind"]),
            )
        )
        ax.text(
            item["x"] + item["L"] / 2, item["y"] + item["W"] / 2,
            clean_str(item.get("package_id")),
            ha="center", va="center", color="black", fontsize=7, fontweight="bold",
        )

    ax.set_xlim(-10, container_L + 10)
    ax.set_ylim(-10, container_W + 10)
    ax.set_aspect("equal")
    ax.set_title(title_text)
    ax.set_xlabel("Length (in)")
    ax.set_ylabel("Width (in)")

    buf = BytesIO()
    fig.tight_layout()
    FigureCanvasAgg(fig).print_png(buf)
    buf.seek(0)
    return buf


def add_3d_prism(
    fig: go.Figure,
    x: float,
    y: float,
    z: float,
    length: float,
    width: float,
    height: float,
    color: str,
    opacity: float = 0.5,
    name: str = "Box",
    top_dx: float = 0.0,
    top_dy: float = 0.0,
    hover_text: str = "",
) -> None:
    """
    Adds a box to a Plotly 3D figure. top_dx / top_dy shift the top face,
    which draws a leaning (slanted) unit as a sheared prism.
    """
    v = [
        (x, y, z),
        (x + length, y, z),
        (x + length, y + width, z),
        (x, y + width, z),
        (x + top_dx, y + top_dy, z + height),
        (x + length + top_dx, y + top_dy, z + height),
        (x + length + top_dx, y + width + top_dy, z + height),
        (x + top_dx, y + width + top_dy, z + height),
    ]

    fig.add_trace(
        go.Mesh3d(
            x=[p[0] for p in v],
            y=[p[1] for p in v],
            z=[p[2] for p in v],
            i=[7, 0, 0, 0, 4, 4, 6, 6, 4, 0, 3, 2],
            j=[3, 4, 1, 2, 5, 6, 5, 2, 0, 1, 6, 3],
            k=[0, 7, 2, 3, 6, 7, 1, 1, 5, 5, 7, 6],
            opacity=opacity,
            color=color,
            name=name,
            text=hover_text,
            hoverinfo="text",
            showlegend=False,
        )
    )


def build_3d_plan(
    load: Dict[str, Any],
    result: Dict[str, Any],
    vehicle_data: Dict[str, float],
    order_color_map: Dict[str, str],
) -> go.Figure:
    """
    3D view of one vehicle load.

    Inside each crate/rack, units run along the package length and are
    stacked along the package depth. If the floor packer rotated the
    package, the axes are swapped so the units are drawn correctly.
    Slanted units lean along the depth axis.
    """
    lim: TransportLimits = result["limits"]
    fig = go.Figure()

    add_3d_prism(
        fig, 0, 0, 0,
        float(vehicle_data["L"]), float(vehicle_data["W"]), float(vehicle_data["H"]),
        "gray", 0.05, "Vehicle Shell", hover_text="Vehicle Shell",
    )

    package_lookup: Dict[str, Any] = {}
    for i, pallet in enumerate(result["pallets"]):
        package_lookup[f"P{i + 1}"] = pallet
    for i, crate in enumerate(result["crates"]):
        package_lookup[f"C{i + 1}"] = crate

    for item in load.get("placed", []):
        order_name = clean_str(item.get("order"))
        package_id = clean_str(item.get("package_id"))
        base_hex = order_color_map.get(order_name, "#888888")
        obj = package_lookup.get(package_id)
        rotated = bool(item.get("rotated"))

        add_3d_prism(
            fig, item["x"], item["y"], 0, item["L"], item["W"], item["H"],
            base_hex, 0.35 if item["kind"] == "PALLET" else 0.18, package_id,
            hover_text=f"{package_id}: {item['kind']} ({order_name})",
        )

        # Duck-typed: stored results hold objects from an earlier script run,
        # so isinstance() against the re-defined class would be False.
        if obj is None or not hasattr(obj, "pclass"):
            continue

        cos_t, _ = obj.slant_geometry(lim)
        is_slant = obj.pclass == CLASS_SLANT
        z0 = lim.low_overhead / 2 if obj.pclass == CLASS_LOW else lim.std_overhead / 2

        # s = position along the stack (package depth axis)
        s = CRATE_BASE_DEPTH / 2

        for p in obj.pieces:
            base = p.base(lim)
            thick = p.d
            vertical = p.vertical(lim)
            shown_h = vertical * cos_t if is_slant else vertical
            lean = vertical * math.sqrt(max(0.0, 1 - cos_t ** 2)) if is_slant else 0.0
            foot = thick / cos_t if is_slant else thick

            hover = (
                f"{p.piece_id}<br>Mode: {p.mode}<br>"
                f"Class: {p.transport_class(lim)}<br>"
                f"Dims: {dim_text(p.w, p.h, p.d)}<br>"
                f"Weight: {pounds_text(p.lbs)}"
            )

            if not rotated:
                # length along x, stack along y
                add_3d_prism(
                    fig, item["x"] + CRATE_SIDE_CLEAR, item["y"] + s, z0,
                    base, foot, shown_h, "royalblue", 0.8, p.piece_id,
                    top_dx=0.0, top_dy=lean, hover_text=hover,
                )
            else:
                # length along y, stack along x
                add_3d_prism(
                    fig, item["x"] + s, item["y"] + CRATE_SIDE_CLEAR, z0,
                    foot, base, shown_h, "royalblue", 0.8, p.piece_id,
                    top_dx=lean, top_dy=0.0, hover_text=hover,
                )

            s += (thick + UNIT_SPACER) / cos_t if is_slant else thick + UNIT_SPACER

    fig.update_layout(scene=dict(aspectmode="data"), height=620, margin=dict(l=0, r=0, b=0, t=0))
    return ui.style_fig(fig)


# ==========================================================
# 15. PDF EXPORT
# ==========================================================

def paragraph_safe(value: Any) -> str:
    text = clean_str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def df_to_reportlab_table(
    df: pd.DataFrame,
    max_rows: int = 60,
    font_size: int = 7,
    wrap_cols: Optional[List[str]] = None,
) -> Table:
    """DataFrame -> ReportLab table. Long text columns wrap via Paragraphs."""
    wrap_cols = wrap_cols or []
    cell_style = ParagraphStyle("Cell", fontSize=font_size, leading=font_size + 2)

    if df is None or df.empty:
        data: List[List[Any]] = [["No data"]]
    else:
        shown = df.head(max_rows).copy()
        header = [str(c) for c in shown.columns]
        data = [header]

        for _, row in shown.iterrows():
            out_row: List[Any] = []
            for col in shown.columns:
                text = clean_str(row[col])
                if len(text) > 400:
                    text = text[:400] + "..."
                out_row.append(Paragraph(paragraph_safe(text), cell_style) if col in wrap_cols else text)
            data.append(out_row)

        if len(df) > max_rows:
            data.append([f"... {len(df) - max_rows} additional row(s) not shown in PDF"] + [""] * (len(header) - 1))

    table = Table(data, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.35, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("FONTSIZE", (0, 0), (-1, -1), font_size),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def build_pdf_meta_table(
    project: ProjectMeta,
    vehicle_name: str,
    vehicle_data: Dict[str, float],
    assumptions: LogisticsAssumptions,
    lim: TransportLimits,
) -> Table:
    door = safe_float(vehicle_data.get("door_H"), None)
    data = [
        ["Project", project.project_name],
        ["Customer", project.customer],
        ["Location", project.project_location],
        ["Destination", project.destination],
        ["Factory", project.factory],
        ["System", project.system],
        ["Estimator", project.estimator],
        ["Estimator Email", project.estimator_email],
        ["Quote / Job Ref", project.quote_or_job_ref],
        ["Revision", project.revision],
        ["Generated", now_stamp()],
        [
            "Vehicle",
            f'{vehicle_name} - {vehicle_data["L"]:.0f}"L x {vehicle_data["W"]:.0f}"W x {vehicle_data["H"]:.0f}"H'
            + (f', door {door:.1f}"' if door else ""),
        ],
        ["Vehicle Payload", pounds_text(float(vehicle_data.get("max_lbs", 0) or 0))],
        ["Max Package Height", inches_mm_text(lim.usable_ext_h)],
        [
            "Factory Transport Rules",
            f"Standard pallet up to {inches_mm_text(lim.std_max_v)}; "
            f"low-floor (no glass) up to {inches_mm_text(lim.low_floor_max_v)}; "
            f"slant rack up to {inches_mm_text(lim.slant_max_v)} leaned to {inches_text(lim.slant_target_v)}",
        ],
        ["Max Crate Weight", pounds_text(assumptions.max_crate_lbs)],
        ["Max Pallet Weight", pounds_text(assumptions.max_pallet_lbs)],
        ["Planning Note", assumptions.planning_warning],
    ]

    cell_style = ParagraphStyle("Meta", fontSize=8, leading=10)
    data = [[k, Paragraph(paragraph_safe(v), cell_style)] for k, v in data]

    table = Table(data, colWidths=[130, 600])
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.35, colors.grey),
                ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def generate_pdf_report(project: ProjectMeta, result: Dict[str, Any]) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(letter),
        rightMargin=24, leftMargin=24, topMargin=24, bottomMargin=24,
    )

    styles = getSampleStyleSheet()
    small_style = ParagraphStyle("Small", parent=styles["Normal"], fontSize=7, leading=9)

    story: List[Any] = []
    vehicle_name = result["vehicle_name"]
    vehicle_data = result["vehicle_data"]
    assumptions: LogisticsAssumptions = result["assumptions"]
    lim: TransportLimits = result["limits"]
    manifest_df = result["manifest_df"]
    decision_df = result["decision_df"]
    loads = result["loads"]

    story.append(Paragraph(f"Logistics Manifest: {paragraph_safe(project.display_name())}", styles["Title"]))
    story.append(Spacer(1, 10))
    story.append(Paragraph(paragraph_safe(assumptions.planning_warning), small_style))
    story.append(Spacer(1, 10))
    story.append(build_pdf_meta_table(project, vehicle_name, vehicle_data, assumptions, lim))
    story.append(Spacer(1, 14))

    story.append(Paragraph("Container Summary", styles["Heading2"]))
    story.append(Spacer(1, 6))
    story.append(df_to_reportlab_table(result["load_summary_df"], max_rows=80, font_size=7, wrap_cols=["Packages"]))
    story.append(Spacer(1, 14))

    story.append(Paragraph("Package Manifest", styles["Heading2"]))
    story.append(Spacer(1, 6))
    manifest_cols = [c for c in ["Order", "ID", "Type", "Weight", "Dims", "Status", "Handling", "Contents"]
                     if c in manifest_df.columns]
    story.append(
        df_to_reportlab_table(
            manifest_df[manifest_cols] if not manifest_df.empty else manifest_df,
            max_rows=80, font_size=6, wrap_cols=["Status", "Handling", "Contents"],
        )
    )
    story.append(PageBreak())

    story.append(Paragraph("Packing Decisions", styles["Heading2"]))
    story.append(Spacer(1, 6))
    decision_cols = [c for c in ["Order", "Final Piece", "Mode", "Orientation", "Transport Class", "Glass",
                                 "W", "H", "Vertical mm", "Lbs", "Assigned Package"] if c in decision_df.columns]
    story.append(
        df_to_reportlab_table(
            decision_df[decision_cols] if not decision_df.empty else decision_df,
            max_rows=150, font_size=6,
        )
    )

    container_L = float(vehicle_data["L"])
    container_W = float(vehicle_data["W"])

    for load in loads:
        story.append(PageBreak())
        title = (
            f"Container #{load['container_no']} - Floor Utilization: {load['util']:.1f}% - "
            f"Weight: {pounds_text(float(load.get('weight', 0)))}"
        )
        story.append(Paragraph(paragraph_safe(title), styles["Heading2"]))
        story.append(Spacer(1, 8))

        try:
            buf = render_mpl_2d_plan(
                load["placed"], container_L, container_W, result["order_color_map"],
                title_text=f"Container #{load['container_no']}",
            )
            story.append(RLImage(buf, width=680, height=290))
        except Exception as e:  # noqa: BLE001
            story.append(Paragraph(paragraph_safe(f"[Image generation failed: {e}]"), styles["Italic"]))

    final_overflow = result.get("final_overflow", [])
    if final_overflow:
        story.append(PageBreak())
        story.append(Paragraph("Unpacked Packages", styles["Heading2"]))
        story.append(Spacer(1, 8))
        overflow_cols = ["package_id", "kind", "order", "L", "W", "H", "weight", "unpacked_reason"]
        overflow_df = pd.DataFrame(final_overflow)
        overflow_df = overflow_df[[c for c in overflow_cols if c in overflow_df.columns]]
        story.append(df_to_reportlab_table(overflow_df, max_rows=100, font_size=7))

    doc.build(story)
    return buffer.getvalue()


# ==========================================================
# 16. EXCEL EXPORT
# ==========================================================

def autosize_excel_columns(writer: pd.ExcelWriter) -> None:
    try:
        for worksheet in writer.sheets.values():
            for column_cells in worksheet.columns:
                max_length = max(len("" if c.value is None else str(c.value)) for c in column_cells)
                worksheet.column_dimensions[column_cells[0].column_letter].width = min(max(max_length + 2, 10), 60)
            worksheet.freeze_panes = "A2"
    except Exception:  # noqa: BLE001  formatting must never block export
        pass


def export_plan_to_excel_bytes(
    project: ProjectMeta,
    master_df: pd.DataFrame,
    unit_issue_df: pd.DataFrame,
    result: Dict[str, Any],
    scenario_df: Optional[pd.DataFrame] = None,
) -> bytes:
    buffer = BytesIO()

    manifest_df = result.get("manifest_df", pd.DataFrame())
    loads = result.get("loads", [])
    final_overflow = result.get("final_overflow", [])
    assumptions: LogisticsAssumptions = result["assumptions"]
    lim: TransportLimits = result["limits"]
    vehicle_data = result["vehicle_data"]
    counts = plan_class_counts(result)

    total_weight = float(manifest_df["Weight"].astype(float).sum()) if not manifest_df.empty else 0.0
    avg_util = sum(float(ld.get("util", 0)) for ld in loads) / len(loads) if loads else 0.0

    summary_df = pd.DataFrame(
        [
            {"Metric": "Project", "Value": project.project_name},
            {"Metric": "Customer", "Value": project.customer},
            {"Metric": "Destination", "Value": project.destination},
            {"Metric": "Factory", "Value": project.factory},
            {"Metric": "System", "Value": project.system},
            {"Metric": "Estimator", "Value": project.estimator},
            {"Metric": "Generated", "Value": now_stamp()},
            {"Metric": "Vehicle", "Value": result.get("vehicle_name", "")},
            {"Metric": "Vehicle Dims", "Value": dim_text(float(vehicle_data["L"]), float(vehicle_data["W"]),
                                                         float(vehicle_data["H"]))},
            {"Metric": "Vehicle Door Height", "Value": safe_float(vehicle_data.get("door_H"), None)},
            {"Metric": "Max Package Height (in)", "Value": round(lim.usable_ext_h, 1)},
            {"Metric": "Standard Pallet Max Unit (in)", "Value": round(lim.std_max_v, 1)},
            {"Metric": "Low-Floor Max Unit (in)", "Value": round(lim.low_floor_max_v, 1)},
            {"Metric": "Slant Max Unit (in)", "Value": round(lim.slant_max_v, 1)},
            {"Metric": "Vehicle Payload", "Value": float(vehicle_data.get("max_lbs", 0) or 0)},
            {"Metric": "Total Weight", "Value": total_weight},
            {"Metric": "Containers Used", "Value": len(loads)},
            {"Metric": "Crates / Racks", "Value": len(result.get("crates", []))},
            {"Metric": "Pallets", "Value": len(result.get("pallets", []))},
            {"Metric": "Standard Pieces", "Value": counts.get(CLASS_STD, 0)},
            {"Metric": "Low-Floor Pieces (no glass)", "Value": counts.get(CLASS_LOW, 0)},
            {"Metric": "Slant Rack Pieces", "Value": counts.get(CLASS_SLANT, 0)},
            {"Metric": "Disassembled Pieces", "Value": counts.get(CLASS_DIS, 0)},
            {"Metric": "Oversize Pieces", "Value": counts.get(CLASS_OVERSIZE, 0)},
            {"Metric": "Average Floor Utilization %", "Value": round(avg_util, 1)},
            {"Metric": "Unpacked Packages", "Value": len(final_overflow)},
            {"Metric": "Planning Warning", "Value": assumptions.planning_warning},
        ]
    )

    assumptions_df = pd.DataFrame(
        [{"Assumption": k, "Value": v} for k, v in asdict(assumptions).items()]
    )

    sheets = [
        ("Summary", summary_df),
        ("Input Units", master_df),
        ("Unit Issues", unit_issue_df),
        ("Packages", manifest_df),
        ("Container Loads", result.get("load_summary_df")),
        ("Packing Decisions", result.get("decision_df")),
        ("Packing Notes", result.get("packing_notes_df")),
        ("Unpacked", pd.DataFrame(final_overflow)),
        ("Scenarios", scenario_df),
        ("Assumptions", assumptions_df),
    ]

    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for sheet_name, df in sheets:
            if df is not None and not df.empty:
                df.to_excel(writer, sheet_name=sheet_name[:31], index=False)
        autosize_excel_columns(writer)

    buffer.seek(0)
    return buffer.getvalue()


# ==========================================================
# 17. SAVE / LOAD JOB JSON HELPERS
# ==========================================================

def project_from_dict(data: Dict[str, Any]) -> ProjectMeta:
    return ProjectMeta(**{k: clean_str(data.get(k)) for k in ProjectMeta.__dataclass_fields__})


def assumptions_from_dict(data: Dict[str, Any]) -> LogisticsAssumptions:
    """Loads assumptions; unknown keys are ignored and missing keys use defaults."""
    defaults = LogisticsAssumptions()
    values: Dict[str, Any] = {}

    for name, default in asdict(defaults).items():
        raw = data.get(name, default)
        if isinstance(default, bool):
            values[name] = bool(raw)
        elif isinstance(default, (int, float)):
            values[name] = float(safe_float(raw, default))
        else:
            values[name] = clean_str(raw) or default

    return LogisticsAssumptions(**values)


def build_job_save_payload(
    project: ProjectMeta,
    assumptions: LogisticsAssumptions,
    orders: Dict[str, str],
    master_df: Optional[pd.DataFrame],
    selected_vehicle: str,
    selected_pallets: List[str],
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    records = master_df.to_dict(orient="records") if master_df is not None and not master_df.empty else []
    payload = {
        "app": APP_NAME,
        "version": APP_VERSION,
        "saved_at": now_stamp(),
        "project": asdict(project),
        "assumptions": asdict(assumptions),
        "orders": orders,
        "master_rows": records,
        "selected_vehicle": selected_vehicle,
        "selected_pallets": selected_pallets,
    }
    if extra:
        payload.update(extra)
    return payload


def load_job_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    project = project_from_dict(payload.get("project", {}) or {})
    assumptions = assumptions_from_dict(payload.get("assumptions", {}) or {})

    orders = payload.get("orders", {})
    if not isinstance(orders, dict):
        orders = {}

    master_df = pd.DataFrame(payload.get("master_rows", []) or [])
    if not master_df.empty:
        master_df = normalize_master_df(master_df, assumptions)

    selected_pallets = payload.get("selected_pallets", [])
    if not isinstance(selected_pallets, list):
        selected_pallets = []

    return {
        "project": project,
        "assumptions": assumptions,
        "orders": orders,
        "master_df": master_df,
        "selected_vehicle": clean_str(payload.get("selected_vehicle")),
        "selected_pallets": selected_pallets,
        "decided": payload.get("decided") if isinstance(payload.get("decided"), dict) else {},
        "step": safe_int(payload.get("step"), 0) or 0,
        "job_id": clean_str(payload.get("job_id")),
    }


def read_uploaded_json(uploaded_file) -> Dict[str, Any]:
    raw = uploaded_file.getvalue() if hasattr(uploaded_file, "getvalue") else uploaded_file.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


# ==========================================================
# 18. CUSTOM VEHICLE / PALLET CONFIG HELPERS
# ==========================================================

def sample_config_json() -> Dict[str, Any]:
    return {
        "vehicles": {
            "Custom Trailer Example": {"L": 600, "W": 96, "H": 106, "door_H": 104, "max_lbs": 44000}
        },
        "pallets": {
            "Factory Rack Example": {"L": 120, "W": 48, "H": 8, "max_lbs": 3000}
        },
    }


def merge_custom_config(
    uploaded_config: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, float]], Dict[str, Dict[str, float]], List[str]]:
    """
    Merges an optional uploaded config with the default vehicles/pallets.
    Vehicles: L, W, H, door_H (optional), max_lbs. Pallets: L, W, H, max_lbs.
    """
    vehicles = json.loads(json.dumps(DEFAULT_CONTAINERS))
    pallets = json.loads(json.dumps(DEFAULT_PALLETS))
    warnings: List[str] = []

    if not uploaded_config:
        return vehicles, pallets, warnings

    vehicle_rows = uploaded_config.get("vehicles", {})
    pallet_rows = uploaded_config.get("pallets", {})

    if not isinstance(vehicle_rows, dict):
        warnings.append("Config field 'vehicles' must be an object/dictionary.")
        vehicle_rows = {}
    if not isinstance(pallet_rows, dict):
        warnings.append("Config field 'pallets' must be an object/dictionary.")
        pallet_rows = {}

    for name, spec in vehicle_rows.items():
        try:
            entry = {
                "L": float(spec["L"]),
                "W": float(spec["W"]),
                "H": float(spec["H"]),
                "max_lbs": float(spec.get("max_lbs", 0) or 0),
            }
            door = safe_float(spec.get("door_H"), None)
            if door:
                entry["door_H"] = door
            else:
                warnings.append(f"Vehicle '{name}' has no door_H; interior height used as loading limit.")
            vehicles[clean_str(name)] = entry
        except Exception:  # noqa: BLE001
            warnings.append(f"Skipped invalid vehicle config: {name}")

    for name, spec in pallet_rows.items():
        try:
            pallets[clean_str(name)] = {
                "L": float(spec["L"]),
                "W": float(spec["W"]),
                "H": float(spec.get("H", PALLET_H)),
                "max_lbs": float(spec.get("max_lbs", 2200) or 2200),
            }
        except Exception:  # noqa: BLE001
            warnings.append(f"Skipped invalid pallet config: {name}")

    return vehicles, pallets, warnings


# ==========================================================
# 19. SESSION STATE, JOB STORE
#
# The app is a guided workflow: Jobs -> 1 Job -> 2 Units -> 3 Review -> 4 Plan.
# Every job is saved as a JSON file per user, so several jobs can be open over time.
# Decisions made in Review are written back into the master table
# (Mode / Orient / SR / SC), so the optimizer logic is unchanged.
# ==========================================================

STEPS = ["Job", "Units", "Review", "Plan"]
STEP_LABELS = [f"{i + 1}  {name}" for i, name in enumerate(STEPS)]

OPT_SLANT = "slant"
OPT_LOW = "low"
OPT_DIS = "disassemble"
OPT_SPLIT = "split"


def init_session_state() -> None:
    defaults = {
        "project": ProjectMeta(),
        "assumptions": LogisticsAssumptions(),
        "orders": {},
        "df_master": None,
        "results": None,
        "results_key": "",
        "unit_issue_df": pd.DataFrame(),
        "scenario_df": pd.DataFrame(),
        "vehicle_catalog": DEFAULT_CONTAINERS,
        "pallet_catalog": DEFAULT_PALLETS,
        "custom_config": None,
        "selected_vehicle": "40' HC Container",
        "selected_pallets": ["Euro 2 (1200x1000mm)", "Factory (2200x1000mm)"],
        "last_validation_df": pd.DataFrame(),
        "editor_version": 0,
        "job_id": None,
        "step": 0,
        "step_ctl": STEP_LABELS[0],
        "decided": {},
        "selected_order": "",
        "order_issues": {},
        "confirm_delete": "",
        "last_saved_hash": "",
        "export_cache": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def clear_results() -> None:
    st.session_state.results = None
    st.session_state.results_key = ""
    st.session_state.export_cache = None
    st.session_state.scenario_df = pd.DataFrame()


def set_master_df(df: Optional[pd.DataFrame]) -> None:
    """Stores a new master table and resets widget state that depends on it."""
    st.session_state.df_master = df
    st.session_state.editor_version += 1
    clear_results()


def has_master() -> bool:
    df = st.session_state.df_master
    return df is not None and not df.empty


def current_limits() -> TransportLimits:
    vehicle_data = st.session_state.vehicle_catalog[st.session_state.selected_vehicle]
    return get_transport_limits(vehicle_data, st.session_state.assumptions)


def current_vehicle_data() -> Dict[str, float]:
    return st.session_state.vehicle_catalog[st.session_state.selected_vehicle]


def go_step(step: int) -> None:
    st.session_state.step = max(0, min(len(STEPS) - 1, int(step)))
    st.session_state.step_ctl = STEP_LABELS[st.session_state.step]


def step_control_changed() -> None:
    value = st.session_state.get("step_ctl")
    if value in STEP_LABELS:
        st.session_state.step = STEP_LABELS.index(value)
    else:
        st.session_state.step_ctl = STEP_LABELS[st.session_state.step]


def apply_loaded_job_to_session(loaded: Dict[str, Any]) -> None:
    st.session_state.project = loaded["project"]
    st.session_state.assumptions = loaded["assumptions"]
    st.session_state.orders = loaded["orders"]

    if loaded["selected_vehicle"] in st.session_state.vehicle_catalog:
        st.session_state.selected_vehicle = loaded["selected_vehicle"]

    valid_pallets = [p for p in loaded["selected_pallets"] if p in st.session_state.pallet_catalog]
    if valid_pallets:
        st.session_state.selected_pallets = valid_pallets

    set_master_df(loaded["master_df"])


# ----------------------------------------------------------
# Job store (one JSON file per job, one folder per user)
# ----------------------------------------------------------

def jobs_dir() -> Optional[str]:
    base = os.environ.get("ULTRALOGISTICS_JOBS_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "jobs"
    )
    path = os.path.join(base, slugify(st.session_state.get("username", ""), "shared"))
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return None
    return path


def new_job_id(name: str = "") -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{slugify(name, 'job')[:30]}"


def reset_job_state() -> None:
    st.session_state.project = ProjectMeta()
    st.session_state.assumptions = LogisticsAssumptions()
    st.session_state.orders = {}
    st.session_state.df_master = None
    st.session_state.decided = {}
    st.session_state.selected_order = ""
    st.session_state.order_issues = {}
    st.session_state.last_validation_df = pd.DataFrame()
    st.session_state.unit_issue_df = pd.DataFrame()
    st.session_state.editor_version += 1
    st.session_state.last_saved_hash = ""
    clear_results()
    go_step(0)


def start_new_job() -> None:
    reset_job_state()
    st.session_state.job_id = new_job_id("new-job")


def open_saved_job(payload: Dict[str, Any], job_id: Optional[str] = None) -> None:
    loaded = load_job_payload(payload)
    reset_job_state()
    apply_loaded_job_to_session(loaded)
    st.session_state.decided = dict(loaded.get("decided") or {})
    st.session_state.job_id = job_id or loaded.get("job_id") or new_job_id(loaded["project"].project_name)
    go_step(int(loaded.get("step") or 0))


def open_job_by_id(job_id: str) -> None:
    folder = jobs_dir()
    if not folder:
        return
    try:
        with open(os.path.join(folder, f"{job_id}.json"), encoding="utf-8") as fh:
            open_saved_job(json.load(fh), job_id)
    except (OSError, ValueError):
        pass


def delete_job_file(job_id: str) -> None:
    folder = jobs_dir()
    if folder:
        try:
            os.remove(os.path.join(folder, f"{job_id}.json"))
        except OSError:
            pass
    st.session_state.confirm_delete = ""


def list_jobs() -> List[Dict[str, Any]]:
    folder = jobs_dir()
    if not folder:
        return []

    entries: List[Tuple[float, Dict[str, Any]]] = []
    for name in os.listdir(folder):
        if not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
            project = payload.get("project", {}) or {}
            entries.append(
                (
                    os.path.getmtime(path),
                    {
                        "id": name[:-5],
                        "name": clean_str(project.get("project_name")) or "Untitled job",
                        "reference": clean_str(project.get("quote_or_job_ref")),
                        "saved_at": clean_str(payload.get("saved_at")),
                        "status": payload.get("status") or {"label": "Add units", "tone": "neutral"},
                    },
                )
            )
        except (OSError, ValueError):
            continue

    return [job for _, job in sorted(entries, key=lambda x: x[0], reverse=True)]


def compute_status() -> Dict[str, Any]:
    df = st.session_state.df_master
    units = 0 if df is None else len(df)
    orders = 0 if units == 0 else int(df["Order"].nunique())
    status: Dict[str, Any] = {"label": "Add units", "tone": "neutral", "units": units, "orders": orders}

    if units == 0:
        return status

    try:
        review = build_review(df, current_vehicle_data(), st.session_state.assumptions, st.session_state.decided)
        left = review["decisions_left"]
        if left:
            status.update(label=f"{left} decision{'s' if left != 1 else ''} left", tone="warn")
        elif st.session_state.results and st.session_state.results.get("ok"):
            n = len(st.session_state.results["loads"])
            status.update(label=f"Plan ready, {n} container{'s' if n != 1 else ''}", tone="good")
        else:
            status.update(label="Ready to plan", tone="neutral")
    except Exception:  # noqa: BLE001  status is cosmetic
        pass

    return status


def save_current_job() -> None:
    job_id = st.session_state.get("job_id")
    folder = jobs_dir()
    if not job_id or not folder:
        return

    project: ProjectMeta = st.session_state.project
    if not project.project_name and not has_master() and not any(clean_str(v) for v in st.session_state.orders.values()):
        return  # empty draft, nothing worth keeping

    payload = build_job_save_payload(
        project,
        st.session_state.assumptions,
        st.session_state.orders,
        st.session_state.df_master,
        st.session_state.selected_vehicle,
        st.session_state.selected_pallets,
        extra={
            "job_id": job_id,
            "step": st.session_state.step,
            "decided": st.session_state.decided,
            "status": compute_status(),
        },
    )

    body = {k: v for k, v in payload.items() if k != "saved_at"}
    digest = hashlib.md5(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    if digest == st.session_state.last_saved_hash:
        return

    path = os.path.join(folder, f"{job_id}.json")
    try:
        with open(path + ".tmp", "wb") as fh:
            fh.write(json_download_bytes(payload))
        os.replace(path + ".tmp", path)
        st.session_state.last_saved_hash = digest
    except OSError:
        pass


def go_home() -> None:
    save_current_job()
    st.session_state.job_id = None


# ==========================================================
# 19b. REVIEW ENGINE (turns the transport check into per-unit decisions)
# ==========================================================

def base_transport_class(p: Piece, lim: TransportLimits) -> str:
    """Class a unit would get with no special handling (WHOLE, AUTO)."""
    probe = Piece(
        orig_id=p.orig_id, piece_id=p.piece_id, w=p.w, h=p.h, utype=p.utype, d=p.d, lbs=p.lbs,
        mode="WHOLE", orientation="AUTO", source_order=p.source_order,
    )
    return probe.transport_class(lim)


def split_layout(p: Piece, lim: TransportLimits, a: LogisticsAssumptions) -> Tuple[int, int]:
    rows = max(1, math.ceil(p.h / lim.std_max_v))
    cols = max(1, math.ceil(p.lbs / a.max_crate_lbs)) if p.lbs > a.max_crate_lbs else 1
    if rows * cols == 1:
        cols = 2
    return rows, cols


def current_unit_choice(row: pd.Series, default_cls: str) -> Optional[str]:
    mode = clean_str(row.get("Mode")).upper()
    sr = safe_int(row.get("SR"), 1) or 1
    sc = safe_int(row.get("SC"), 1) or 1
    if mode == "SLANT":
        return OPT_SLANT
    if mode == "DISASSEMBLED":
        return OPT_DIS
    if sr * sc > 1:
        return OPT_SPLIT
    return OPT_LOW if default_cls == CLASS_LOW else None


def build_review(
    master_df: pd.DataFrame,
    vehicle_data: Dict[str, float],
    a: LogisticsAssumptions,
    decided: Dict[str, str],
) -> Dict[str, Any]:
    lim = get_transport_limits(vehicle_data, a)
    issue_df = build_unit_issue_report(master_df, vehicle_data, a)

    issues_by_unit: Dict[str, Dict[str, str]] = {}
    if not issue_df.empty:
        for unit, sub in issue_df.groupby("Unit", sort=False):
            sevs = set(sub["Severity"])
            severity = "ERROR" if "ERROR" in sevs else ("WARNING" if "WARNING" in sevs else "INFO")
            issues_by_unit[str(unit)] = {
                "severity": severity,
                "problem": "; ".join(dict.fromkeys(sub["Problem"])),
            }

    groups: List[Dict[str, Any]] = []
    normal_units = 0

    for orig, sub in master_df.groupby("Orig", sort=False):
        row = sub.iloc[0]
        p = row_to_piece(row)
        if p is None:
            continue

        count = len(sub)
        info = issues_by_unit.get(str(orig), {})
        severity = info.get("severity", "")
        default_cls = base_transport_class(p, lim)
        current = current_unit_choice(row, default_cls)
        need = default_cls in (CLASS_LOW, CLASS_OVERSIZE) or severity == "ERROR"

        group = {
            "orig": str(orig),
            "order": clean_str(row.get("Order")),
            "mark": clean_str(row.get("Mark")),
            "type": clean_str(row.get("Type")),
            "count": count,
            "w": p.w,
            "h": p.h,
            "lbs": p.lbs,
            "default_cls": default_cls,
            "severity": severity,
            "problem": info.get("problem", ""),
            "current": current,
            "options": [],
            "kind": "normal",
        }

        if need:
            if default_cls == CLASS_LOW:
                options = [OPT_SLANT] if min(p.w, p.h) <= lim.slant_max_v else []
                options.append(OPT_LOW)
            else:
                options = [OPT_DIS, OPT_SPLIT]

            handled = (str(orig) in decided) or (current is not None and current != OPT_LOW)
            group["options"] = options
            group["kind"] = "resolved" if (handled and severity != "ERROR") else "decide"
        elif severity == "INFO":
            group["kind"] = "note"
        else:
            normal_units += count

        groups.append(group)

    return {
        "groups": groups,
        "normal_units": normal_units,
        "total_units": len(master_df),
        "decisions_left": sum(1 for g in groups if g["kind"] == "decide"),
        "issue_df": issue_df,
        "limits": lim,
    }


def apply_unit_choice(orig: str, choice: str, vehicle_data: Dict[str, float], a: LogisticsAssumptions) -> None:
    df = st.session_state.df_master.copy()
    mask = df["Orig"] == orig
    if not mask.any():
        return

    lim = get_transport_limits(vehicle_data, a)
    p = row_to_piece(df[mask].iloc[0])

    if choice == OPT_SLANT:
        mode, orient, sr, sc = "SLANT", "AUTO", 1, 1
    elif choice == OPT_LOW:
        mode, orient, sr, sc = "WHOLE", "AUTO", 1, 1
    elif choice == OPT_DIS:
        rows = max(1, math.ceil(p.h / lim.std_max_v)) if p else 1
        mode, orient, sr, sc = "DISASSEMBLED", "AUTO", (rows if p and min(p.w, p.h) > lim.std_max_v else 1), 1
    else:
        sr, sc = split_layout(p, lim, a) if p else (2, 1)
        mode, orient = "WHOLE", "UPRIGHT"

    df.loc[mask, ["Mode", "Orient", "SR", "SC"]] = [mode, orient, sr, sc]
    st.session_state.df_master = df
    st.session_state.decided[orig] = choice
    clear_results()


def option_text(choice: str, g: Dict[str, Any], lim: TransportLimits, a: LogisticsAssumptions) -> Tuple[str, str]:
    if choice == OPT_SLANT:
        return "Slant rack, glass stays in", "Avoids a separate glass shipment. Confirm with the factory."
    if choice == OPT_LOW:
        return "Low-floor pallet, glass ships separately", "Transport only. Not suited to transshipment or rough handling."
    if choice == OPT_DIS:
        return "Take apart", "Ships as a frame kit with loose glass on pallets."
    probe = Piece(orig_id=g["orig"], piece_id=g["orig"], w=g["w"], h=g["h"], utype=g["type"], d=1.0, lbs=g["lbs"])
    rows, cols = split_layout(probe, lim, a)
    return f"Split into {rows * cols} parts", "Each part ships whole. The parts are joined on site."


def vehicle_label(name: str) -> str:
    data = st.session_state.vehicle_catalog.get(name, {})
    door = safe_float(data.get("door_H"), None)
    payload = float(data.get("max_lbs", 0) or 0)
    parts = [name]
    if door:
        parts.append(f'door {door:.1f}"')
    if payload:
        parts.append(f"{payload:,.0f} lbs")
    return ", ".join(parts)


# ==========================================================
# 20. LOGIN / AUTHENTICATION
# ==========================================================

def load_users() -> Dict[str, str]:
    """
    Users come from Streamlit secrets, in either of these layouts. The first matches
    the EAS Estimating Tool, so the same secrets block works in both apps.

    [credentials.users]
    thanos = "choose-a-password"

    [passwords]
    thanos = "choose-a-password"
    """
    users: Dict[str, str] = {}
    try:
        users.update({str(k): str(v) for k, v in dict(st.secrets["credentials"]["users"]).items()})
    except Exception:  # noqa: BLE001
        pass
    try:
        for k, v in dict(st.secrets["passwords"]).items():
            users.setdefault(str(k), str(v))
    except Exception:  # noqa: BLE001
        pass
    return users


def check_password() -> bool:
    """Returns True once the user has signed in. Shows the sign-in screen otherwise."""
    if st.session_state.get("authenticated", False):
        return True

    users = load_users()

    def password_entered() -> None:
        name = clean_str(st.session_state.get("login_user", ""))
        password = str(st.session_state.get("login_pass", ""))
        stored = users.get(name)
        if stored is not None and hmac.compare_digest(password.encode("utf-8"), stored.encode("utf-8")):
            st.session_state.authenticated = True
            st.session_state.login_failed = False
            st.session_state.username = name
            st.session_state.pop("login_pass", None)
            st.session_state.pop("login_user", None)
        else:
            st.session_state.login_failed = True

    _, middle, _ = st.columns([1, 1.6, 1])
    with middle:
        ui.login_header(APP_NAME)
        if not users:
            st.error("Authentication secrets not configured correctly. Add a [credentials.users] section in Streamlit secrets.")
            st.stop()
        with st.form("login_form"):
            st.text_input("Username", key="login_user")
            st.text_input("Password", type="password", key="login_pass")
            st.form_submit_button("Sign in", type="primary", on_click=password_entered, **ui.STRETCH)
        if st.session_state.get("login_failed"):
            ui.note("bad", "Invalid username or password.")
    st.stop()
    return False


# ==========================================================
# 21. TOP BAR, STEPPER, JOBS HOME
# ==========================================================

def render_topbar() -> None:
    left, right = st.columns([5, 2], vertical_alignment="center")
    with left:
        ui.page_header(APP_NAME, "Crating and container load planning")
    with right:
        buttons = st.columns(2)
        if st.session_state.job_id:
            buttons[0].button("All jobs", on_click=go_home, key="topbar_home", **ui.STRETCH)
        if buttons[1].button("Log out", key="topbar_logout", **ui.STRETCH):
            st.session_state.authenticated = False
            st.session_state.username = ""
            st.session_state.job_id = None
            st.rerun()


def render_stepper() -> None:
    if hasattr(st, "segmented_control"):
        st.segmented_control(
            "Steps", STEP_LABELS, selection_mode="single", key="step_ctl",
            on_change=step_control_changed, label_visibility="collapsed",
        )
    else:
        st.radio(
            "Steps", STEP_LABELS, key="step_ctl", horizontal=True,
            on_change=step_control_changed, label_visibility="collapsed",
        )


def render_home() -> None:
    head = st.columns([5, 1.4], vertical_alignment="bottom")
    with head[0]:
        ui.headline("Jobs", "Pick up where you left off, or start a new job.")
    with head[1]:
        st.button("New job", type="primary", on_click=start_new_job, key="home_new", **ui.STRETCH)

    folder = jobs_dir()
    jobs = list_jobs()

    if folder is None:
        st.info("Jobs cannot be saved on this server. Download the job file from the Plan step to keep a job.")
    elif not jobs:
        st.info("No jobs yet. Start a new job, add the units, and it is saved here automatically.")

    for job in jobs:
        status = job["status"]
        with st.container(border=True):
            cols = st.columns([5, 3, 1.2, 1.2], vertical_alignment="center")
            meta = " | ".join(x for x in [job["reference"], status.get("units") and f"{status['units']} units", job["saved_at"]] if x)
            cols[0].markdown(
                f"**{_html.escape(job['name'])}**  \n<span style='color:#6e6e73;font-size:.86rem'>{_html.escape(meta)}</span>",
                unsafe_allow_html=True,
            )
            cols[1].markdown(ui.pill(status.get("label", ""), status.get("tone", "neutral")), unsafe_allow_html=True)
            cols[2].button("Open", key=f"open_{job['id']}", on_click=open_job_by_id, args=(job["id"],), **ui.STRETCH)
            cols[3].button(
                "Delete", key=f"del_{job['id']}",
                on_click=lambda jid=job["id"]: st.session_state.update(confirm_delete=jid), **ui.STRETCH,
            )
            if st.session_state.confirm_delete == job["id"]:
                st.warning(f"Delete {job['name']}? This cannot be undone.")
                confirm = st.columns([1, 1, 4])
                confirm[0].button("Delete job", key=f"delok_{job['id']}", type="primary",
                                  on_click=delete_job_file, args=(job["id"],), **ui.STRETCH)
                confirm[1].button("Keep it", key=f"delno_{job['id']}",
                                  on_click=lambda: st.session_state.update(confirm_delete=""), **ui.STRETCH)

    with st.expander("Open a job file", expanded=False):
        job_upload = st.file_uploader("Job file (.json)", type=["json"], key="job_upload")
        if st.button("Open this file", key="open_file_btn", disabled=job_upload is None):
            try:
                open_saved_job(read_uploaded_json(job_upload))
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"Could not open the file: {e}")


# ==========================================================
# 22. STEP 1: JOB
# ==========================================================

def render_assumption_widgets(a0: LogisticsAssumptions) -> LogisticsAssumptions:
    st.markdown("##### Factory transport rules")
    st.caption("Unit height as shipped. Defaults follow the factory 40' HC guidance.")
    c = st.columns(3)
    factory_std_max_mm = c[0].number_input(
        "Standard pallet max (mm)", min_value=500.0, max_value=4000.0, value=float(a0.factory_std_max_mm), step=1.0)
    allow_low_floor = c[1].checkbox("Allow low-floor pallet (no glass)", value=bool(a0.allow_low_floor))
    factory_low_floor_max_mm = c[2].number_input(
        "Low-floor pallet max (mm)", min_value=500.0, max_value=4000.0,
        value=float(a0.factory_low_floor_max_mm), step=1.0, disabled=not allow_low_floor)
    c = st.columns(3)
    factory_slant_max_mm = c[0].number_input(
        "Slant rack max unit height (mm)", min_value=500.0, max_value=5000.0,
        value=float(a0.factory_slant_max_mm), step=1.0)
    std_pack_overhead = c[1].number_input(
        "Standard package overhead (in)", min_value=0.0, max_value=24.0, value=float(a0.std_pack_overhead), step=0.5)
    low_floor_pack_overhead = c[2].number_input(
        "Low-floor package overhead (in)", min_value=0.0, max_value=24.0,
        value=float(a0.low_floor_pack_overhead), step=0.5)

    st.markdown("##### Weight and packing")
    c = st.columns(3)
    glass_kg_m2 = c[0].number_input("Glass kg/m²", min_value=10.0, max_value=80.0, value=float(a0.glass_kg_m2), step=1.0)
    std_mult = c[1].number_input("Weight multiplier, standard", min_value=1.0, max_value=3.0,
                                 value=float(a0.std_weight_multiplier), step=0.05)
    lsd_mult = c[2].number_input("Weight multiplier, lift and slide", min_value=1.0, max_value=3.0,
                                 value=float(a0.lsd_weight_multiplier), step=0.05)
    c = st.columns(2)
    frame_dis = c[0].slider("Frame kit share, take apart", 0.0, 0.5, float(a0.frame_kit_pct_disassembly), 0.01)
    frame_split = c[1].slider("Frame kit share, take apart with split", 0.0, 0.5,
                              float(a0.frame_kit_pct_split), 0.01)
    c = st.columns(4)
    max_crate_lbs = c[0].number_input("Max crate lbs", min_value=500.0, max_value=12000.0,
                                      value=float(a0.max_crate_lbs), step=100.0)
    max_pallet_lbs = c[1].number_input("Max pallet lbs", min_value=500.0, max_value=10000.0,
                                       value=float(a0.max_pallet_lbs), step=100.0)
    crate_len = c[2].number_input("Max crate length (in)", min_value=48.0, max_value=800.0,
                                  value=float(a0.crate_max_len_ext), step=6.0)
    crate_width = c[3].number_input("Max crate width (in)", min_value=12.0, max_value=120.0,
                                    value=float(a0.crate_max_width_ext), step=1.0)
    c = st.columns(4)
    slant_depth = c[0].number_input("Max slant rack depth (in)", min_value=12.0, max_value=120.0,
                                    value=float(a0.slant_rack_max_depth_ext), step=1.0)
    crate_volume = c[1].number_input("Max crate volume (in³)", min_value=10_000.0, max_value=5_000_000.0,
                                     value=float(a0.crate_max_volume_ext), step=50_000.0)
    height_clear = c[2].number_input("Door height clearance (in)", min_value=0.0, max_value=24.0,
                                     value=float(a0.vehicle_height_clearance), step=0.5)
    floor_clear = c[3].number_input("Floor item clearance (in)", min_value=0.0, max_value=12.0,
                                    value=float(a0.container_item_clearance), step=0.5)
    c = st.columns(2)
    no_mixing = c[0].checkbox("Do not mix orders inside crates and pallets", value=bool(a0.no_mixing_orders))
    pallets_for_dis = c[1].checkbox("Use pallets for taken-apart pieces", value=bool(a0.allow_pallets_for_disassembled))

    return LogisticsAssumptions(
        glass_kg_m2=glass_kg_m2,
        std_weight_multiplier=std_mult,
        lsd_weight_multiplier=lsd_mult,
        frame_kit_pct_disassembly=frame_dis,
        frame_kit_pct_split=frame_split,
        max_crate_lbs=max_crate_lbs,
        max_pallet_lbs=max_pallet_lbs,
        crate_max_len_ext=crate_len,
        crate_max_width_ext=crate_width,
        slant_rack_max_depth_ext=slant_depth,
        crate_max_volume_ext=crate_volume,
        vehicle_height_clearance=height_clear,
        container_item_clearance=floor_clear,
        factory_std_max_mm=factory_std_max_mm,
        factory_low_floor_max_mm=factory_low_floor_max_mm,
        factory_slant_max_mm=factory_slant_max_mm,
        allow_low_floor=allow_low_floor,
        std_pack_overhead=std_pack_overhead,
        low_floor_pack_overhead=low_floor_pack_overhead,
        no_mixing_orders=no_mixing,
        allow_pallets_for_disassembled=pallets_for_dis,
        planning_warning=a0.planning_warning,
    )


def render_advanced_settings(job: str) -> None:
    p0: ProjectMeta = st.session_state.project

    st.markdown("##### More job details")
    c = st.columns(2)
    destination = c[0].text_input("Destination", value=p0.destination, key=f"job_dest_{job}")
    location = c[1].text_input("Project location", value=p0.project_location, key=f"job_loc_{job}")
    system = c[0].text_input("System", value=p0.system, key=f"job_sys_{job}")
    revision = c[1].text_input("Revision", value=p0.revision, key=f"job_rev_{job}")
    estimator = c[0].text_input("Estimator", value=p0.estimator, key=f"job_est_{job}")
    estimator_email = c[1].text_input("Estimator email", value=p0.estimator_email, key=f"job_mail_{job}")
    notes = st.text_area("Notes", value=p0.notes, height=70, key=f"job_notes_{job}")
    st.session_state.project = replace(
        st.session_state.project, destination=destination, project_location=location, system=system,
        revision=revision, estimator=estimator, estimator_email=estimator_email, notes=notes,
    )

    a0: LogisticsAssumptions = st.session_state.assumptions
    assumptions = render_assumption_widgets(a0)
    # Compare by value: the stored object may come from an older run of the script.
    if asdict(assumptions) != asdict(a0):
        st.session_state.assumptions = assumptions
        clear_results()

    st.markdown("##### Pallets for taken-apart pieces")
    pallet_names = list(st.session_state.pallet_catalog.keys())
    valid_default = [p for p in st.session_state.selected_pallets if p in pallet_names] or pallet_names[:1]
    selected = st.multiselect("Pallets and racks allowed", pallet_names, default=valid_default, key=f"pallets_{job}")
    if selected != st.session_state.selected_pallets:
        st.session_state.selected_pallets = selected
        clear_results()

    st.markdown("##### Custom vehicles and pallets")
    st.caption("Optional JSON file. Vehicles use L, W, H, door_H and max_lbs (inches and lbs).")
    st.download_button(
        "Download sample config", data=json_download_bytes(sample_config_json()),
        file_name="ultralogistics_config_sample.json", mime="application/json", key="sample_cfg_dl",
    )
    config_upload = st.file_uploader("Upload config JSON", type=["json"], key="config_upload")
    if config_upload is not None:
        try:
            st.session_state.custom_config = read_uploaded_json(config_upload)
            st.success("Config loaded.")
        except Exception as e:  # noqa: BLE001
            st.error(f"Could not read the config: {e}")

    vehicles, pallets, warnings = merge_custom_config(st.session_state.custom_config)
    for warning in warnings:
        st.warning(warning)
    st.session_state.vehicle_catalog = vehicles
    st.session_state.pallet_catalog = pallets


def render_step_job() -> None:
    job = st.session_state.job_id
    ui.headline("Start a job", "Name it and pick the vehicle. The factory transport rules are applied for you.")

    p0: ProjectMeta = st.session_state.project

    with st.container(border=True):
        c = st.columns(2)
        name = c[0].text_input("Job name", value=p0.project_name, key=f"job_name_{job}")
        customer = c[1].text_input("Customer", value=p0.customer, key=f"job_customer_{job}")
        reference = c[0].text_input("Quote or job reference", value=p0.quote_or_job_ref, key=f"job_ref_{job}")
        factory = c[1].text_input("Factory", value=p0.factory, key=f"job_factory_{job}")

        vehicle_names = list(st.session_state.vehicle_catalog.keys())
        if st.session_state.selected_vehicle not in vehicle_names:
            st.session_state.selected_vehicle = vehicle_names[0]

        vehicle_labels = {vehicle_label(n): n for n in vehicle_names}
        current_label = vehicle_label(st.session_state.selected_vehicle)
        picked_label = st.radio(
            "Vehicle", list(vehicle_labels.keys()), index=list(vehicle_labels.keys()).index(current_label),
            horizontal=True, key=f"vehicle_{job}",
        )
        vehicle = vehicle_labels[picked_label]
        if vehicle != st.session_state.selected_vehicle:
            st.session_state.selected_vehicle = vehicle
            clear_results()

    st.session_state.project = replace(
        p0, project_name=name, customer=customer, quote_or_job_ref=reference, factory=factory,
    )

    lim = current_limits()
    with st.container(border=True):
        st.markdown("**How units ship in this vehicle**")
        a = st.session_state.assumptions
        rules = [
            f"Up to {a.factory_std_max_mm:,.0f} mm tall: standard pallet, glass in the unit.",
        ]
        if a.allow_low_floor:
            rules.append(
                f"{a.factory_std_max_mm + 1:,.0f} to {in_to_mm(lim.low_floor_max_v):,.0f} mm: low-floor pallet, glass ships separately."
            )
        rules.append(f"Up to {a.factory_slant_max_mm:,.0f} mm: slant rack.")
        for rule in rules:
            st.markdown(f"- {rule}")
        st.caption(f"Largest package this vehicle can load: {inches_mm_text(lim.usable_ext_h)} tall.")

    with st.expander("Advanced settings", expanded=False):
        render_advanced_settings(job)

    nav = st.columns([1, 4, 1.6])
    nav[0].button("All jobs", on_click=go_home, key="job_back", **ui.STRETCH)
    nav[2].button("Continue to units", type="primary", on_click=go_step, args=(1,), key="job_next", **ui.STRETCH)


# ==========================================================
# 23. STEP 2: UNITS
# ==========================================================

def order_names() -> List[str]:
    names = list(st.session_state.orders.keys())
    if has_master():
        for n in st.session_state.df_master["Order"].astype(str).unique():
            if n not in names:
                names.append(n)
    return names


def order_unit_count(name: str) -> int:
    if not has_master():
        return 0
    return int((st.session_state.df_master["Order"].astype(str) == name).sum())


def select_order(name: str) -> None:
    st.session_state.selected_order = name


def add_order() -> None:
    names = order_names()
    n = len(names) + 1
    while f"Order {n}" in names:
        n += 1
    name = f"Order {n}"
    st.session_state.orders[name] = ""
    st.session_state.selected_order = name


def replace_order_rows(name: str, rows: List[Dict[str, Any]]) -> None:
    new_df = pd.DataFrame(rows, columns=MASTER_COLUMNS)
    if has_master():
        keep = st.session_state.df_master[st.session_state.df_master["Order"].astype(str) != name]
        new_df = pd.concat([keep, new_df], ignore_index=True)
    st.session_state.decided = {
        k: v for k, v in st.session_state.decided.items() if not k.startswith(f"{name}|")
    }
    set_master_df(new_df)


def remove_order(name: str) -> None:
    st.session_state.orders.pop(name, None)
    st.session_state.order_issues.pop(name, None)
    if has_master():
        keep = st.session_state.df_master[st.session_state.df_master["Order"].astype(str) != name]
        st.session_state.decided = {
            k: v for k, v in st.session_state.decided.items() if not k.startswith(f"{name}|")
        }
        set_master_df(keep.reset_index(drop=True))
    st.session_state.selected_order = ""


def rename_order(old: str) -> None:
    key = f"ordname_{st.session_state.job_id}_{old}"
    new = clean_str(st.session_state.get(key))
    if not new or new == old or new in order_names():
        return

    st.session_state.orders = {(new if k == old else k): v for k, v in st.session_state.orders.items()}
    if old in st.session_state.order_issues:
        st.session_state.order_issues[new] = st.session_state.order_issues.pop(old)
    st.session_state.decided = {
        (new + k[len(old):] if k.startswith(old + "|") else k): v for k, v in st.session_state.decided.items()
    }

    if has_master():
        df = st.session_state.df_master.copy()
        df.loc[df["Order"].astype(str) == old, "Order"] = new
        for col in ("ID", "Orig"):
            df[col] = df[col].map(lambda s: new + str(s)[len(old):] if str(s).startswith(old + "|") else s)
        set_master_df(df)
    st.session_state.selected_order = new


def render_paste_editor(name: str) -> None:
    job = st.session_state.job_id
    text = st.text_area(
        "One unit per line: mark, width, height, type, quantity (inches)",
        value=st.session_state.orders.get(name, ""),
        height=170,
        key=f"paste_{job}_{name}",
        placeholder="A1, 36, 72, Fixed, 2\nD1, 96, 108, Lift and slide, 1",
    )
    if st.button("Read units", type="primary", key=f"read_{job}_{name}"):
        st.session_state.orders[name] = text
        rows, issues = parse_order_text_to_rows(name, text, st.session_state.assumptions)
        replace_order_rows(name, rows)
        st.session_state.order_issues[name] = validation_issues_to_df(issues)
        st.rerun()


def render_upload_editor(name: str) -> None:
    job = st.session_state.job_id
    uploaded_file = st.file_uploader(
        "Upload CSV or Excel", type=["csv", "xlsx", "xlsm", "xls"], key=f"upload_{job}_{name}",
    )
    if uploaded_file is None:
        return

    try:
        uploaded_df = read_uploaded_dataframe(uploaded_file)
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not read the file: {e}")
        return

    columns = [str(col) for col in uploaded_df.columns]
    uploaded_df.columns = columns
    options = ["<none>"] + columns
    default_mapping = build_default_column_mapping(columns)
    needs_check = any(default_mapping.get(k, "<none>") == "<none>" for k in ("id", "width", "height"))

    mapping: Dict[str, str] = {}
    with st.expander("Check the columns" + (" (some were not found)" if needs_check else ""), expanded=needs_check):
        def pick(label: str, logical: str, container) -> str:
            default_col = default_mapping.get(logical, "<none>")
            return container.selectbox(
                label, options, index=options.index(default_col) if default_col in options else 0,
                key=f"map_{logical}_{job}_{name}_{uploaded_file.name}",
            )

        m1, m2 = st.columns(2)
        mapping["id"] = pick("ID or mark", "id", m1)
        mapping["width"] = pick("Width (in)", "width", m1)
        mapping["height"] = pick("Height (in)", "height", m1)
        mapping["type"] = pick("Type or system", "type", m1)
        mapping["qty"] = pick("Quantity", "qty", m2)
        mapping["depth"] = pick("Depth (in)", "depth", m2)
        mapping["weight"] = pick("Weight (lbs)", "weight", m2)
        mapping["notes"] = pick("Notes", "notes", m2)
        st.dataframe(uploaded_df.head(8), **ui.STRETCH)

    missing = [k for k in ("id", "width", "height") if mapping.get(k, "<none>") == "<none>"]
    if st.button("Import file", type="primary", key=f"import_{job}_{name}", disabled=bool(missing)):
        rows, issues = normalize_uploaded_table(
            uploaded_df, name, mapping, st.session_state.assumptions, uploaded_file.name,
        )
        replace_order_rows(name, rows)
        st.session_state.order_issues[name] = validation_issues_to_df(issues)
        st.rerun()
    if missing:
        st.caption(f"Choose the column for: {', '.join(missing)}.")


def render_order_preview(name: str) -> None:
    if not has_master():
        return

    sub = st.session_state.df_master[st.session_state.df_master["Order"].astype(str) == name]
    issues = st.session_state.order_issues.get(name)

    if sub.empty:
        if issues is None:
            st.caption("No units read yet.")
        else:
            st.error("No valid units were found.")
    else:
        st.success(f"{len(sub)} unit{'s' if len(sub) != 1 else ''} read")

    if issues is not None and not issues.empty:
        st.warning(f"{len(issues)} line{'s' if len(issues) != 1 else ''} could not be read.")
        shown = [c for c in ["row_no", "item_id", "problem", "suggestion", "raw_value"] if c in issues.columns]
        st.dataframe(issues[shown], hide_index=True, **ui.STRETCH)

    if sub.empty:
        return

    table = sub.groupby("Orig", sort=False).agg(
        Mark=("Mark", "first"), Width=("W", "first"), Height=("H", "first"),
        Type=("Type", "first"), Qty=("ID", "count"), Weight=("Lbs", "first"),
    ).reset_index(drop=True)
    table = table.rename(columns={"Width": "Width (in)", "Height": "Height (in)", "Weight": "Weight each (lbs)"})
    st.dataframe(table.round(1).head(60), hide_index=True, **ui.STRETCH)
    if len(table) > 60:
        st.caption(f"{len(table) - 60} more rows not shown.")


def render_step_units() -> None:
    job = st.session_state.job_id
    project_name = st.session_state.project.project_name or "This job"
    ui.headline("Add the units", f"{project_name}. Add each order in the shipment, then paste or upload its units.")

    if not order_names():
        st.session_state.orders["Order 1"] = ""
    names = order_names()
    if st.session_state.selected_order not in names:
        st.session_state.selected_order = names[0]
    selected = st.session_state.selected_order

    left, right = st.columns([1, 3], gap="large")

    with left:
        st.markdown("**Orders in this job**")
        for n in names:
            count = order_unit_count(n)
            st.button(
                f"{n}  ({count} unit{'s' if count != 1 else ''})", key=f"sel_{job}_{n}",
                type="primary" if n == selected else "secondary",
                on_click=select_order, args=(n,), **ui.STRETCH,
            )
        st.button("Add order", key=f"add_order_{job}", on_click=add_order, **ui.STRETCH)
        st.caption("Orders stay in separate crates unless you change that in Advanced settings.")

    with right:
        with st.container(border=True):
            top = st.columns([3, 1], vertical_alignment="bottom")
            top[0].text_input(
                "Order name", value=selected, key=f"ordname_{job}_{selected}",
                on_change=rename_order, args=(selected,),
            )
            top[1].button(
                "Remove order", key=f"rm_{job}_{selected}", on_click=remove_order, args=(selected,),
                disabled=len(names) == 1 and order_unit_count(selected) == 0, **ui.STRETCH,
            )

            source = st.radio(
                "Source", ["Paste", "Upload file"], horizontal=True, key=f"src_{job}_{selected}",
                label_visibility="collapsed",
            )
            if source == "Paste":
                render_paste_editor(selected)
            else:
                render_upload_editor(selected)

            render_order_preview(selected)

    total = int(len(st.session_state.df_master)) if has_master() else 0
    lbs = float(st.session_state.df_master["Lbs"].astype(float).sum()) if has_master() else 0.0
    order_count = int(st.session_state.df_master["Order"].nunique()) if has_master() else 0

    with st.container(border=True):
        bar = st.columns([1, 1, 1.4, 1, 1.6], vertical_alignment="center")
        bar[0].metric("Units", total)
        bar[1].metric("Orders", order_count)
        bar[2].metric("Estimated weight", pounds_text(lbs))
        bar[3].button("Back", on_click=go_step, args=(0,), key="units_back", **ui.STRETCH)
        bar[4].button("Check the units", type="primary", on_click=go_step, args=(2,), key="units_next",
                      disabled=total == 0, **ui.STRETCH)


# ==========================================================
# 24. STEP 3: REVIEW
# ==========================================================

def unit_title(g: Dict[str, Any]) -> str:
    n = g["count"]
    return f"{g['mark']}, {g['type']}, {n} unit{'s' if n != 1 else ''}"


def unit_detail(g: Dict[str, Any], multi_order: bool) -> str:
    text = f'{g["w"]:.0f} in by {g["h"]:.0f} in, {g["lbs"]:,.0f} lbs each'
    return f"{g['order']}. {text}" if multi_order else text


def render_review_group(
    g: Dict[str, Any],
    vehicle_data: Dict[str, float],
    a: LogisticsAssumptions,
    lim: TransportLimits,
    multi_order: bool,
) -> None:
    resolved = g["kind"] == "resolved"
    job = st.session_state.job_id

    with st.container(border=True):
        head = st.columns([4, 1.6], vertical_alignment="center")
        head[0].markdown(f"**{unit_title(g)}**  \n<span style='color:#6e6e73;font-size:.88rem'>{_html.escape(unit_detail(g, multi_order))}</span>",
                         unsafe_allow_html=True)
        if resolved:
            head[1].markdown(ui.pill("Decided", "good"), unsafe_allow_html=True)
        elif g["severity"] == "ERROR" or g["default_cls"] == CLASS_OVERSIZE:
            head[1].markdown(ui.pill("Cannot ship as entered", "bad"), unsafe_allow_html=True)
        else:
            head[1].markdown(ui.pill("Needs your choice", "warn"), unsafe_allow_html=True)

        if g["default_cls"] == CLASS_OVERSIZE:
            st.caption(
                f'Even on its side it is {in_to_mm(min(g["w"], g["h"])):,.0f} mm tall, '
                f"over the {in_to_mm(lim.slant_max_v):,.0f} mm limit."
            )
        elif g["default_cls"] == CLASS_LOW and not resolved:
            st.caption("By default it goes on a low-floor pallet and the glass ships separately.")
        elif g["problem"] and not resolved:
            st.caption(g["problem"])

        opts = g["options"]
        texts = {o: option_text(o, g, lim, a) for o in opts}
        by_label = {texts[o][0]: o for o in opts}
        label_list = [texts[o][0] for o in opts]
        index = opts.index(g["current"]) if resolved and g["current"] in opts else None
        key = f"choice_{job}_{g['orig']}_{st.session_state.editor_version}"

        kwargs: Dict[str, Any] = dict(index=index, key=key, label_visibility="collapsed")
        try:
            picked = st.radio("How should it ship?", label_list, captions=[texts[o][1] for o in opts], **kwargs)
        except TypeError:
            picked = st.radio("How should it ship?", label_list, **kwargs)
        choice = by_label.get(picked) if picked else None

        if choice and choice != (g["current"] if resolved else None):
            apply_unit_choice(g["orig"], choice, vehicle_data, a)
            st.rerun()

        if resolved and g["severity"] == "ERROR":
            st.error(g["problem"] or "This choice still does not fit.")


def render_edit_all_rows() -> None:
    st.caption(
        "Edit Mode, Orientation, Split Rows/Cols, Depth and Weight directly. New rows: enter Order, Mark, W, H "
        "and Type, then save; ID, depth and weight are filled in automatically."
    )
    current_master = st.session_state.df_master.copy()
    for col in MASTER_COLUMNS:
        if col not in current_master.columns:
            current_master[col] = None
    current_master = current_master[MASTER_COLUMNS]

    edited_df = st.data_editor(
        current_master,
        key=f"master_data_editor_{st.session_state.editor_version}",
        hide_index=True,
        num_rows="dynamic",
        column_config={
            "Mode": st.column_config.SelectboxColumn("Mode", options=["WHOLE", "SLANT", "DISASSEMBLED"], default="WHOLE"),
            "Orient": st.column_config.SelectboxColumn("Orientation", options=["AUTO", "UPRIGHT", "SIDE"], default="AUTO"),
            "SR": st.column_config.NumberColumn("Split Rows", min_value=1, step=1, default=1),
            "SC": st.column_config.NumberColumn("Split Cols", min_value=1, step=1, default=1),
            "W": st.column_config.NumberColumn("Width", min_value=0.01, step=0.125, format="%.3f"),
            "H": st.column_config.NumberColumn("Height", min_value=0.01, step=0.125, format="%.3f"),
            "Depth": st.column_config.NumberColumn("Depth", min_value=0.01, step=0.01, format="%.3f"),
            "Lbs": st.column_config.NumberColumn("Weight Lbs", min_value=0.01, step=1.0, format="%.1f"),
            "Qty": st.column_config.NumberColumn("Qty", disabled=True),
        },
        disabled=["ID", "Orig", "Source", "Qty"],
        **ui.STRETCH,
    )

    if st.button("Save edited rows", type="primary", key="save_rows_btn"):
        set_master_df(normalize_master_df(edited_df, st.session_state.assumptions))
        st.rerun()


def render_step_review() -> None:
    a = st.session_state.assumptions
    vehicle_data = current_vehicle_data()

    master = normalize_master_df(st.session_state.df_master, a)
    problems = validate_master_dataframe(master)
    if any(issue.severity == "ERROR" for issue in problems):
        ui.headline("Fix the data first", "Some rows cannot be used as entered.")
        st.dataframe(validation_issues_to_df(problems), hide_index=True, **ui.STRETCH)
        with st.expander("Edit all rows", expanded=True):
            render_edit_all_rows()
        st.button("Back to units", on_click=go_step, args=(1,), key="review_back_err")
        return

    review = build_review(master, vehicle_data, a, st.session_state.decided)
    lim: TransportLimits = review["limits"]
    groups = review["groups"]
    left = review["decisions_left"]
    total = review["total_units"]
    multi_order = master["Order"].astype(str).nunique() > 1

    if left:
        ui.headline(f"{total} units checked, {left} decision{'s' if left != 1 else ''} left",
                    "Only units that need your call are listed. Everything else is already set.")
    else:
        ui.headline(f"{total} units checked, all set", "Every unit has a way to ship. You can build the plan.")

    to_decide = [g for g in groups if g["kind"] == "decide"]
    to_decide.sort(key=lambda g: 0 if (g["severity"] == "ERROR" or g["default_cls"] == CLASS_OVERSIZE) else 1)
    resolved = [g for g in groups if g["kind"] == "resolved"]
    notes = [g for g in groups if g["kind"] == "note"]

    if to_decide:
        st.markdown("### Needs a decision")
        for g in to_decide:
            render_review_group(g, vehicle_data, a, lim, multi_order)

    if resolved:
        st.markdown("### Your choices")
        for g in resolved:
            render_review_group(g, vehicle_data, a, lim, multi_order)

    if notes:
        st.markdown("### Ships with a note")
        with st.container(border=True):
            for g in notes:
                row = st.columns([3, 5], vertical_alignment="center")
                row[0].markdown(f"**{unit_title(g)}**")
                row[1].caption(g["problem"])

    with st.container(border=True):
        row = st.columns([2, 5], vertical_alignment="center")
        normal_pill = ui.pill(f"{review['normal_units']} units", "good")
        row[0].markdown(f"**Ships normally**  \n{normal_pill}", unsafe_allow_html=True)
        row[1].caption("Standard pallets or crates, glass in the unit.")

    with st.expander("Edit all rows", expanded=False):
        render_edit_all_rows()

    nav = st.columns([1.4, 4, 1.8])
    nav[0].button("Back to units", on_click=go_step, args=(1,), key="review_back", **ui.STRETCH)
    nav[2].button("Build the plan", type="primary", on_click=go_step, args=(3,), key="review_next",
                  disabled=left > 0, **ui.STRETCH)


# ==========================================================
# 25. STEP 4: PLAN
# ==========================================================

def plan_cache_key(master: pd.DataFrame) -> str:
    selected = st.session_state.selected_pallets
    blob = json.dumps(
        {
            "m": master.to_dict("records"),
            "a": asdict(st.session_state.assumptions),
            "v": st.session_state.selected_vehicle,
            "vd": current_vehicle_data(),
            "p": selected,
            "pc": {k: st.session_state.pallet_catalog.get(k) for k in selected},
        },
        sort_keys=True, default=str,
    )
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


def get_exports(result: Dict[str, Any]) -> Dict[str, Any]:
    key = (st.session_state.results_key, not st.session_state.scenario_df.empty)
    cache = st.session_state.export_cache
    if cache and cache.get("key") == key:
        return cache

    cache = {"key": key}
    try:
        cache["pdf"] = generate_pdf_report(st.session_state.project, result)
    except Exception as e:  # noqa: BLE001
        cache["pdf_error"] = str(e)
    try:
        cache["xlsx"] = export_plan_to_excel_bytes(
            st.session_state.project, st.session_state.df_master, st.session_state.unit_issue_df,
            result, st.session_state.scenario_df,
        )
    except Exception as e:  # noqa: BLE001
        cache["xlsx_error"] = str(e)
    st.session_state.export_cache = cache
    return cache


def apply_scenario(strategy: str) -> None:
    new_df = create_scenario_master_df(
        st.session_state.df_master, strategy, current_vehicle_data(), st.session_state.assumptions,
    )
    set_master_df(new_df)


def render_compare_options(result: Dict[str, Any]) -> None:
    if st.session_state.scenario_df is None or st.session_state.scenario_df.empty:
        with st.spinner("Comparing options"):
            st.session_state.scenario_df = run_scenario_comparison(
                st.session_state.df_master, st.session_state.vehicle_catalog, [st.session_state.selected_vehicle],
                st.session_state.selected_pallets, st.session_state.pallet_catalog,
                st.session_state.assumptions, SCENARIO_OPTIONS,
            )

    table = st.session_state.scenario_df
    shown = table[["Scenario", "Status", "Containers", "Crates/Racks", "Pallets",
                   "Low-Floor Pieces (no glass)", "Avg Floor Util %", "Unpacked Items"]].copy()
    shown["Scenario"] = shown["Scenario"].replace({SCENARIO_CURRENT: "Your choices (this plan)"})
    st.dataframe(shown, hide_index=True, **ui.STRETCH)

    options = [s for s in SCENARIO_OPTIONS if s != SCENARIO_CURRENT]
    row = st.columns([3, 1.4], vertical_alignment="bottom")
    picked = row[0].selectbox("Try a different way to ship the oversized units", options, key="compare_pick")
    if row[1].button("Use this option", key="compare_apply", **ui.STRETCH):
        apply_scenario(picked)
        st.rerun()
    st.caption("Using an option changes how oversized and low-floor units ship. Units you split yourself stay as they are.")


def render_plan_view(result: Dict[str, Any]) -> None:
    project: ProjectMeta = st.session_state.project
    manifest_df: pd.DataFrame = result["manifest_df"]
    loads = result["loads"]
    unpacked = result["final_overflow"]
    counts = plan_class_counts(result)

    bad = 0 if manifest_df.empty else int((manifest_df["Status"].astype(str) != "OK").sum())
    packages = len(result["crates"]) + len(result["pallets"])
    low = counts.get(CLASS_LOW, 0)

    if unpacked or bad:
        pill = ui.pill(f"{bad + len(unpacked)} item{'s' if bad + len(unpacked) != 1 else ''} need attention", "bad")
    elif low:
        pill = ui.pill("Fits, with restrictions", "warn")
    else:
        pill = ui.pill("Everything fits", "good")

    glass_note = f"{low} piece{'s' if low != 1 else ''} ship without glass." if low else "All glass ships in the units."
    ui.headline(
        f"{len(loads)} container{'s' if len(loads) != 1 else ''}, {packages} package{'s' if packages != 1 else ''}",
        f"{project.display_name()}, {result['vehicle_name']}. {glass_note}",
        pill,
    )

    exports = get_exports(result)
    base_name = f"{today_file_stamp()}-{slugify(project.display_name())}-logistics"
    dl = st.columns(3)
    if "pdf" in exports:
        dl[0].download_button("Download summary PDF", data=exports["pdf"], file_name=f"{base_name}-manifest.pdf",
                              mime="application/pdf", type="primary", key="plan_pdf", **ui.STRETCH)
    else:
        dl[0].error(f"Could not make the PDF: {exports.get('pdf_error')}")
    if "xlsx" in exports:
        dl[1].download_button(
            "Download Excel manifest", data=exports["xlsx"], file_name=f"{base_name}-manifest.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="plan_xlsx", **ui.STRETCH)
    else:
        dl[1].error(f"Could not make the workbook: {exports.get('xlsx_error')}")
    dl[2].download_button(
        "Download job file",
        data=json_download_bytes(build_job_save_payload(
            project, st.session_state.assumptions, st.session_state.orders, st.session_state.df_master,
            st.session_state.selected_vehicle, st.session_state.selected_pallets,
            extra={"job_id": st.session_state.job_id, "step": 3, "decided": st.session_state.decided},
        )),
        file_name=f"{base_name}.json", mime="application/json", key="plan_json", **ui.STRETCH)

    total_weight = float(manifest_df["Weight"].astype(float).sum()) if not manifest_df.empty else 0.0
    avg_util = sum(float(ld["util"]) for ld in loads) / len(loads) if loads else 0.0
    m = st.columns(4)
    m[0].metric("Total weight", pounds_text(total_weight))
    m[1].metric("Containers", len(loads))
    m[2].metric("Average floor use", f"{avg_util:.0f}%")
    m[3].metric("Not loaded", len(unpacked))

    if unpacked:
        st.error(f"{len(unpacked)} package(s) could not be loaded into this vehicle.")
        st.dataframe(pd.DataFrame(unpacked), hide_index=True, **ui.STRETCH)
    if bad:
        st.warning(f"{bad} package(s) have status warnings.")
        st.dataframe(manifest_df[manifest_df["Status"].astype(str) != "OK"], hide_index=True, **ui.STRETCH)

    st.markdown("### Compare ways to ship")
    render_compare_options(result)

    st.markdown("### Container floor plan")
    vehicle_data = result["vehicle_data"]
    if loads:
        numbers = [load["container_no"] for load in loads]
        selected_no = numbers[0]
        if len(numbers) > 1:
            cont_labels = {f"Container {n}": n for n in numbers}
            selected_no = cont_labels[st.radio("Container", list(cont_labels.keys()), horizontal=True,
                                               key="plan_container")]
        load = next(ld for ld in loads if ld["container_no"] == selected_no)
        st.caption(
            f"{result['vehicle_name']}, container {selected_no}: {len(load['placed'])} packages, "
            f"{pounds_text(float(load.get('weight', 0)))}, {load['util']:.0f}% of the floor. "
            "Colours show the order each package belongs to."
        )
        st.plotly_chart(
            build_2d_plan(load["placed"], float(vehicle_data["L"]), float(vehicle_data["W"]),
                          result["order_color_map"], title_text=f"Container {selected_no}"),
            key=f"plan2d_{selected_no}", **ui.STRETCH,
        )

        st.markdown("### 3D view")
        st.caption("Drag to rotate, scroll to zoom. Slanted units lean back inside their rack.")
        st.plotly_chart(
            build_3d_plan(load, result, vehicle_data, result["order_color_map"]),
            key=f"plan3d_{selected_no}", **ui.STRETCH,
        )

    with st.expander("Packages and packing decisions", expanded=False):
        st.markdown("**Package manifest**")
        st.dataframe(manifest_df, hide_index=True, **ui.STRETCH)
        st.markdown("**Container load summary**")
        st.dataframe(result["load_summary_df"], hide_index=True, **ui.STRETCH)
        st.markdown("**Packing decisions**")
        st.dataframe(result["decision_df"], hide_index=True, **ui.STRETCH)
        notes = result.get("packing_notes_df")
        if notes is not None and not notes.empty:
            st.markdown("**Packing notes**")
            st.dataframe(notes, hide_index=True, **ui.STRETCH)

    st.caption(result["assumptions"].planning_warning)


def render_step_plan() -> None:
    a = st.session_state.assumptions
    vehicle_data = current_vehicle_data()
    master = normalize_master_df(st.session_state.df_master, a)
    review = build_review(master, vehicle_data, a, st.session_state.decided)

    if review["decisions_left"]:
        n = review["decisions_left"]
        ui.headline("One more step", f"{n} unit{'s' if n != 1 else ''} still need a decision before the plan can be built.")
        st.button("Go to review", type="primary", on_click=go_step, args=(2,), key="plan_to_review")
        return

    key = plan_cache_key(master)
    result = st.session_state.results
    if not result or st.session_state.results_key != key:
        with st.spinner("Building the plan"):
            plan = build_logistics_plan(
                master, st.session_state.selected_vehicle, vehicle_data,
                st.session_state.selected_pallets, st.session_state.pallet_catalog, a,
            )
        if not plan.get("ok"):
            st.error(plan.get("message", "The plan could not be built."))
            if "errors" in plan:
                st.dataframe(plan["errors"], hide_index=True, **ui.STRETCH)
            st.button("Back to review", on_click=go_step, args=(2,), key="plan_back_err")
            return
        st.session_state.results = plan
        st.session_state.results_key = key
        st.session_state.export_cache = None
        st.session_state.unit_issue_df = review["issue_df"]
        result = plan

    render_plan_view(result)

    st.button("Back to review", on_click=go_step, args=(2,), key="plan_back")


# ==========================================================
# 26. MAIN
# ==========================================================

def main() -> None:
    st.set_page_config(page_title=APP_NAME, page_icon="📦", layout="wide", initial_sidebar_state="collapsed")
    ui.apply_theme()
    init_session_state()
    check_password()

    render_topbar()

    if not st.session_state.job_id:
        render_home()
    else:
        render_stepper()
        step = st.session_state.step

        if step in (2, 3) and not has_master():
            ui.headline("Add units first", "There is nothing to check yet.")
            st.button("Go to units", type="primary", on_click=go_step, args=(1,), key="need_units")
        elif step == 0:
            render_step_job()
        elif step == 1:
            render_step_units()
        elif step == 2:
            render_step_review()
        else:
            render_step_plan()

        save_current_job()

    st.divider()
    st.caption(
        f"{APP_NAME} {APP_VERSION}. Planning tool only. Final crate design, glass handling, blocking and bracing, "
        "freight loading, payload, route restrictions and carrier requirements must be verified before shipment."
    )


if __name__ == "__main__":
    main()
