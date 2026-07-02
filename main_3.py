from __future__ import annotations

import json
import math
import re
import hmac
from dataclasses import dataclass, field, asdict
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib import patches

from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    Image as RLImage,
    PageBreak,
)
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors


# ==========================================================
# ULTRALOGISTICS PRO
# Upgraded single-file Streamlit logistics/crating planner
# Paste all parts into one .py file in order.
# ==========================================================


# ==========================================================
# 1. CONSTANTS
# ==========================================================

MM_TO_INCH = 1 / 25.4

# 2718 mm = about 107.01"
DEFAULT_FACTORY_SLANT_MAX_H_IN = 2718.0 * MM_TO_INCH

CRATE_SIDE_CLEAR = 2.0
CRATE_BASE_DEPTH = 4.0
UNIT_SPACER = 1.0
PALLET_H = 6.0

DEFAULT_CRATE_MAX_LEN_EXT = 630.0
DEFAULT_CRATE_MAX_WIDTH_EXT = 48.0

DEFAULT_CONTAINER_ITEM_CLEARANCE = 1.0
DEFAULT_HEIGHT_CLEARANCE = 2.0

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

DEFAULT_CONTAINERS: Dict[str, Dict[str, float]] = {
    "40' HC Container": {
        "L": 473.0,
        "W": 92.0,
        "H": 105.0,
        "max_lbs": 44000.0,
    },
    "40' Standard": {
        "L": 473.0,
        "W": 92.0,
        "H": 95.0,
        "max_lbs": 44000.0,
    },
    "53' Dry Van": {
        "L": 636.0,
        "W": 100.0,
        "H": 110.0,
        "max_lbs": 45000.0,
    },
    "20' Standard": {
        "L": 232.0,
        "W": 92.0,
        "H": 95.0,
        "max_lbs": 28000.0,
    },
}

DEFAULT_PALLETS: Dict[str, Dict[str, float]] = {
    "US GMA (48x40)": {
        "L": 48.0,
        "W": 40.0,
        "H": 6.0,
        "max_lbs": 2200.0,
    },
    "Euro 2 (1200x1000mm)": {
        "L": 1200 * MM_TO_INCH,
        "W": 1000 * MM_TO_INCH,
        "H": 6.0,
        "max_lbs": 2200.0,
    },
    "Oversize (96x48)": {
        "L": 96.0,
        "W": 48.0,
        "H": 6.0,
        "max_lbs": 3000.0,
    },
}


# ==========================================================
# 2. SMALL UTILITY HELPERS
# ==========================================================

def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def today_file_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default

    text = str(value).strip()
    text = text.replace(",", "")
    text = text.replace('"', "")
    text = text.replace("in", "")
    text = text.strip()

    if text == "":
        return default

    try:
        return float(text)
    except Exception:
        return default


def safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    f = safe_float(value, None)
    if f is None:
        return default

    try:
        return int(round(f))
    except Exception:
        return default


def slugify(value: str, fallback: str = "load-plan") -> str:
    value = clean_str(value)
    value = re.sub(r"[^\w\s\-]+", "", value)
    value = re.sub(r"\s+", "-", value)
    value = value.strip("-_")
    return value or fallback


def inches_text(value: float) -> str:
    return f'{value:.1f}"'


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
    unique_names = sorted(set([x for x in order_names if clean_str(x)]))
    color_map: Dict[str, str] = {}

    for i, order_name in enumerate(unique_names):
        color_map[order_name] = ORDER_COLOR_HEX[i % len(ORDER_COLOR_HEX)]

    return color_map


def order_of_piece_id(piece_id: str) -> str:
    if "|" not in piece_id:
        return ""
    return piece_id.split("|", 1)[0]


def base_mark_of_piece_id(piece_id: str) -> str:
    if "|" in piece_id:
        piece_id = piece_id.split("|", 1)[1]
    return piece_id.split("#", 1)[0]


def get_vehicle_internal_crate_height_limit(
    vehicle_data: Dict[str, float],
    height_clearance: float,
) -> float:
    """
    Max INTERNAL usable crate height.

    Ensures:
    crate internal height + top/bottom crate clearances + vehicle clearance
    stays inside selected vehicle height.
    """
    vehicle_h = float(vehicle_data["H"])
    return max(
        1.0,
        vehicle_h - height_clearance - (2 * CRATE_SIDE_CLEAR),
    )


def get_vehicle_external_crate_height_limit(
    vehicle_data: Dict[str, float],
    height_clearance: float,
) -> float:
    """
    Max EXTERNAL crate height allowed inside selected vehicle.
    """
    vehicle_h = float(vehicle_data["H"])
    return max(1.0, vehicle_h - height_clearance)


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
    frame_kit_weight_pct: float = 0.20

    max_crate_lbs: float = 2500.0
    max_pallet_lbs: float = 2200.0

    crate_max_len_ext: float = DEFAULT_CRATE_MAX_LEN_EXT
    crate_max_width_ext: float = DEFAULT_CRATE_MAX_WIDTH_EXT

    vehicle_height_clearance: float = DEFAULT_HEIGHT_CLEARANCE
    container_item_clearance: float = DEFAULT_CONTAINER_ITEM_CLEARANCE

    no_mixing_orders: bool = True
    allow_pallets_for_disassembled: bool = True

    planning_warning: str = (
        "Planning layout only. Final loading, blocking, bracing, route limits, "
        "and carrier requirements must be verified by logistics/freight team."
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

    def resolved_orientation(self, max_h_int: float) -> str:
        """
        Resolves AUTO orientation.

        UPRIGHT means vertical dimension = height.
        SIDE means vertical dimension = width.
        """
        if self.mode == "DISASSEMBLED":
            return "UPRIGHT"

        if self.orientation in ("UPRIGHT", "SIDE"):
            return self.orientation

        if self.h <= max_h_int:
            return "UPRIGHT"

        if self.w <= max_h_int:
            return "SIDE"

        return "UPRIGHT"

    def oriented_vertical(self, max_h_int: float) -> float:
        if self.resolved_orientation(max_h_int) == "SIDE":
            return self.w
        return self.h

    def oriented_base(self, max_h_int: float) -> float:
        if self.resolved_orientation(max_h_int) == "SIDE":
            return self.h
        return self.w

    def crate_len_need_int(self, max_h_int: float) -> float:
        """
        Required INTERNAL crate length.

        For slanted items:
        length = base + horizontal run created by leaning the piece.
        """
        if self.mode == "DISASSEMBLED":
            return max(self.w, self.h)

        vert = self.oriented_vertical(max_h_int)
        base = self.oriented_base(max_h_int)

        if self.mode == "SLANT" and vert > max_h_int:
            slant_run = math.sqrt(max(0.0, vert ** 2 - max_h_int ** 2))
            return base + slant_run

        return base

    def crate_eff_vertical_h(self, max_h_int: float) -> float:
        """
        Effective internal crate height consumed by this piece.
        """
        if self.mode == "DISASSEMBLED":
            return min(self.w, self.h)

        if self.mode == "SLANT":
            return min(self.oriented_vertical(max_h_int), max_h_int)

        return self.oriented_vertical(max_h_int)

    def pallet_footprint_dims(
        self,
        pallet_L: float,
        pallet_W: float,
    ) -> Optional[Tuple[float, float]]:
        """
        Returns footprint dimensions on a pallet if this piece can fit.

        This simple palletizer treats disassembled pieces as long flat parts.
        """
        if self.mode == "DISASSEMBLED":
            base = max(self.w, self.h)
        else:
            base = self.oriented_base(DEFAULT_FACTORY_SLANT_MAX_H_IN)

        thick = self.d + UNIT_SPACER

        if base <= pallet_L and thick <= pallet_W:
            return base, thick

        if base <= pallet_W and thick <= pallet_L:
            return thick, base

        return None


@dataclass
class Crate:
    order: str = ""
    pieces: List[Piece] = field(default_factory=list)
    depth_used: float = CRATE_BASE_DEPTH
    weight: float = 0.0
    max_len_int: float = 0.0
    max_h_int: float = 0.0

    @property
    def L_ext(self) -> float:
        return self.max_len_int + 2 * CRATE_SIDE_CLEAR

    @property
    def W_ext(self) -> float:
        return self.depth_used

    @property
    def H_ext(self) -> float:
        return self.max_h_int + 2 * CRATE_SIDE_CLEAR

    def projected_add(
        self,
        p: Piece,
        max_h_int: float,
    ) -> Dict[str, float]:
        l_need = p.crate_len_need_int(max_h_int)
        h_need = p.crate_eff_vertical_h(max_h_int)

        return {
            "weight": self.weight + p.lbs,
            "L_ext": max(self.max_len_int, l_need) + 2 * CRATE_SIDE_CLEAR,
            "W_ext": self.depth_used + p.d + UNIT_SPACER,
            "H_ext": max(self.max_h_int, h_need) + 2 * CRATE_SIDE_CLEAR,
        }

    def can_add(
        self,
        p: Piece,
        max_h_int: float,
        max_crate_lbs: float,
        max_len_ext: float,
        max_width_ext: float,
        max_height_ext: float,
    ) -> Tuple[bool, str]:
        projected = self.projected_add(p, max_h_int)

        if projected["weight"] > max_crate_lbs:
            return False, "crate weight limit"

        if projected["L_ext"] > max_len_ext:
            return False, "crate length limit"

        if projected["W_ext"] > max_width_ext:
            return False, "crate width/depth limit"

        if projected["H_ext"] > max_height_ext:
            return False, "vehicle height limit"

        return True, "fits"

    def add(
        self,
        p: Piece,
        max_h_int: float,
    ) -> None:
        self.pieces.append(p)
        self.depth_used += p.d + UNIT_SPACER
        self.max_len_int = max(
            self.max_len_int,
            p.crate_len_need_int(max_h_int),
        )
        self.max_h_int = max(
            self.max_h_int,
            p.crate_eff_vertical_h(max_h_int),
        )
        self.weight += p.lbs


@dataclass
class PalletObject:
    order: str
    name: str
    L: float
    W: float
    H: float
    max_wgt: float
    pieces: List[Piece] = field(default_factory=list)
    weight: float = 0.0
    uL: float = 0.0
    rW: float = 0.0
    tW: float = 0.0

    def place(self, p: Piece) -> bool:
        dims = p.pallet_footprint_dims(self.L, self.W)

        if not dims:
            return False

        pL, pW = dims

        if self.weight + p.lbs > self.max_wgt:
            return False

        # Continue current row.
        if (
            self.uL + pL <= self.L
            and self.tW + max(self.rW, pW) <= self.W
        ):
            self.uL += pL
            self.rW = max(self.rW, pW)

        # Start new row.
        else:
            if self.tW + self.rW + pW > self.W:
                return False

            self.tW += self.rW
            self.uL = pL
            self.rW = pW

        self.pieces.append(p)
        self.weight += p.lbs

        return True

# ==========================================================
# 4. WEIGHT / DEPTH / SPLIT LOGIC
# ==========================================================

def is_lsd_type(unit_type: Any) -> bool:
    text = clean_str(unit_type).upper()

    keywords = [
        "LSD",
        "LIFT",
        "SLID",
        "SLIDE",
        "MULTISLIDE",
        "MULTI-SLIDE",
    ]

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
    Returns:
        depth_inches, estimated_weight_lbs

    Important upgrade:
    depth is now kept and used later in crate/pallet packing.
    """
    is_lsd = is_lsd_type(unit_type)

    depth = 7.87 if is_lsd else 3.54

    area_m2 = (w * 0.0254) * (h * 0.0254)
    multiplier = lsd_multiplier if is_lsd else std_multiplier
    weight_lbs = area_m2 * glass_kg_m2 * multiplier * 2.20462

    return depth, weight_lbs


def should_auto_slant(
    piece: Piece,
    max_h_int: float,
) -> bool:
    return (
        piece.mode == "WHOLE"
        and piece.oriented_vertical(max_h_int) > max_h_int
    )


def expand_manual_split(
    p: Piece,
    rows: int,
    cols: int,
    mode_override: str,
    orient_override: str,
    frame_pct: float,
    max_h_int: float,
) -> List[Piece]:
    """
    Splits one unit into rows x cols parts.

    If DISASSEMBLED:
      - panel pieces get the non-frame weight
      - four frame kit sticks are added
    """
    rows = max(1, int(rows or 1))
    cols = max(1, int(cols or 1))
    part_count = rows * cols

    mode_override = clean_str(mode_override).upper() or "WHOLE"
    orient_override = clean_str(orient_override).upper() or "AUTO"

    if part_count <= 1:
        p.mode = mode_override
        p.orientation = orient_override

        if should_auto_slant(p, max_h_int):
            p.mode = "SLANT"

        return [p]

    part_w = p.w / cols
    part_h = p.h / rows

    kit_weight = 0.0
    if mode_override == "DISASSEMBLED":
        kit_weight = p.lbs * frame_pct

    panel_weight_total = max(0.0, p.lbs - kit_weight)
    panel_weight_each = panel_weight_total / part_count

    parts: List[Piece] = []

    for i in range(part_count):
        sub_mode = mode_override

        if sub_mode != "DISASSEMBLED":
            test_piece = Piece(
                orig_id=p.orig_id,
                piece_id=p.piece_id,
                w=part_w,
                h=part_h,
                utype=p.utype,
                d=p.d,
                lbs=panel_weight_each,
                mode="WHOLE",
                orientation=orient_override,
                source_order=p.source_order,
            )

            if test_piece.oriented_vertical(max_h_int) > max_h_int:
                sub_mode = "SLANT"
            else:
                sub_mode = "WHOLE"

        parts.append(
            Piece(
                orig_id=p.orig_id,
                piece_id=f"{p.piece_id}:P{i + 1}",
                w=part_w,
                h=part_h,
                utype=f"{p.utype} (Part)",
                d=p.d,
                lbs=panel_weight_each,
                mode=sub_mode,
                orientation=orient_override,
                source_order=p.source_order,
            )
        )

    if mode_override == "DISASSEMBLED" and kit_weight > 0:
        stick_weight = kit_weight / 4.0
        suffixes = ["V1", "V2", "H1", "H2"]

        for suffix in suffixes:
            stick_len = p.h if suffix.startswith("V") else p.w

            parts.append(
                Piece(
                    orig_id=p.orig_id,
                    piece_id=f"{p.piece_id}:KIT_{suffix}",
                    w=stick_len,
                    h=6.0,
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

def parse_order_text_to_rows(
    order_name: str,
    raw_text: str,
    assumptions: LogisticsAssumptions,
) -> Tuple[List[Dict[str, Any]], List[ValidationIssue]]:
    """
    Expected paste format:
        ID, W, H, Type, Qty

    Delimiters supported:
        comma
        tab
        two or more spaces
    """
    rows: List[Dict[str, Any]] = []
    issues: List[ValidationIssue] = []

    if not clean_str(raw_text):
        return rows, issues

    lines = [
        line.rstrip()
        for line in raw_text.splitlines()
        if clean_str(line)
    ]

    for line_no, line in enumerate(lines, start=1):
        parts = re.split(r"\s*,\s*|\t+|\s{2,}", line.strip())

        if len(parts) < 5:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    source="Raw Paste",
                    row_no=line_no,
                    order=order_name,
                    item_id="",
                    problem="Too few columns. Expected ID, W, H, Type, Qty.",
                    suggestion="Use format like: A1, 36, 72, FIXED, 2",
                    raw_value=line,
                )
            )
            continue

        if len(parts) > 5:
            item_id_raw = parts[0]
            width_raw = parts[1]
            height_raw = parts[2]
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
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    source="Raw Paste",
                    row_no=line_no,
                    order=order_name,
                    item_id="",
                    problem="Missing item ID / mark.",
                    suggestion="Add a unit ID or mark.",
                    raw_value=line,
                )
            )
            continue

        if width is None or width <= 0:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    source="Raw Paste",
                    row_no=line_no,
                    order=order_name,
                    item_id=item_id,
                    problem="Width is missing, zero, or not numeric.",
                    suggestion="Enter width in inches.",
                    raw_value=line,
                )
            )
            continue

        if height is None or height <= 0:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    source="Raw Paste",
                    row_no=line_no,
                    order=order_name,
                    item_id=item_id,
                    problem="Height is missing, zero, or not numeric.",
                    suggestion="Enter height in inches.",
                    raw_value=line,
                )
            )
            continue

        if qty is None or qty <= 0:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    source="Raw Paste",
                    row_no=line_no,
                    order=order_name,
                    item_id=item_id,
                    problem="Quantity is missing, zero, or not numeric.",
                    suggestion="Enter quantity as a positive whole number.",
                    raw_value=line,
                )
            )
            continue

        if not unit_type:
            unit_type = "STANDARD"
            issues.append(
                ValidationIssue(
                    severity="WARNING",
                    source="Raw Paste",
                    row_no=line_no,
                    order=order_name,
                    item_id=item_id,
                    problem="Type is blank.",
                    suggestion="Default STANDARD assumptions were used.",
                    raw_value=line,
                )
            )

        depth, lbs = calculate_specs(
            unit_type,
            width,
            height,
            assumptions.glass_kg_m2,
            assumptions.std_weight_multiplier,
            assumptions.lsd_weight_multiplier,
        )

        for i in range(qty):
            generated_id = f"{order_name}|{item_id}#{i + 1}"

            rows.append(
                {
                    "Order": order_name,
                    "ID": generated_id,
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
                    "Source": "Raw Paste",
                    "Notes": "",
                }
            )

    return rows, issues


# ==========================================================
# 6. EXCEL / CSV UPLOAD NORMALIZATION
# ==========================================================

def read_uploaded_dataframe(uploaded_file) -> pd.DataFrame:
    """
    Reads CSV or Excel uploads into a DataFrame.
    """
    file_name = uploaded_file.name.lower()

    if file_name.endswith(".csv"):
        return pd.read_csv(uploaded_file)

    if (
        file_name.endswith(".xlsx")
        or file_name.endswith(".xlsm")
        or file_name.endswith(".xls")
    ):
        return pd.read_excel(uploaded_file)

    raise ValueError("Unsupported file type. Upload CSV, XLSX, XLSM, or XLS.")


def guess_column(
    columns: List[str],
    keywords: List[str],
) -> str:
    """
    Attempts to guess a source column based on keywords.
    Returns '<none>' if no likely match is found.
    """
    normalized = {
        col: re.sub(r"[^a-z0-9]+", "", str(col).lower())
        for col in columns
    }

    for col, simple in normalized.items():
        for keyword in keywords:
            key = re.sub(r"[^a-z0-9]+", "", keyword.lower())
            if key and key in simple:
                return col

    return "<none>"


def build_default_column_mapping(columns: List[str]) -> Dict[str, str]:
    """
    Best-effort mapping for common estimating spreadsheet headers.
    """
    return {
        "id": guess_column(columns, ["id", "mark", "type mark", "window", "door"]),
        "width": guess_column(columns, ["width", "w", "si width", "imperial width"]),
        "height": guess_column(columns, ["height", "h", "si height", "imperial height"]),
        "type": guess_column(columns, ["type", "system", "description", "unit type"]),
        "qty": guess_column(columns, ["qty", "quantity", "count"]),
        "depth": guess_column(columns, ["depth", "frame depth"]),
        "weight": guess_column(columns, ["weight", "lbs", "pounds"]),
        "notes": guess_column(columns, ["notes", "remarks", "comments"]),
    }


def normalize_uploaded_table(
    df: pd.DataFrame,
    order_name: str,
    mapping: Dict[str, str],
    assumptions: LogisticsAssumptions,
    source_name: str,
) -> Tuple[List[Dict[str, Any]], List[ValidationIssue]]:
    """
    Converts uploaded CSV/Excel rows into the app's master row format.

    Required logical fields:
        id
        width
        height

    Optional:
        type
        qty
        depth
        weight
        notes
    """
    rows: List[Dict[str, Any]] = []
    issues: List[ValidationIssue] = []

    def cell(row: pd.Series, logical_name: str) -> Any:
        col = mapping.get(logical_name, "<none>")
        if col == "<none>" or col not in df.columns:
            return None
        return row[col]

    for idx, row in df.iterrows():
        row_no = int(idx) + 2

        item_id = clean_str(cell(row, "id"))
        width = safe_float(cell(row, "width"))
        height = safe_float(cell(row, "height"))
        unit_type = clean_str(cell(row, "type")) or "STANDARD"
        qty = safe_int(cell(row, "qty"), 1) or 1
        notes = clean_str(cell(row, "notes"))

        raw_preview = " | ".join(
            clean_str(x)
            for x in row.tolist()[:10]
        )

        if not item_id:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    source=source_name,
                    row_no=row_no,
                    order=order_name,
                    item_id="",
                    problem="Missing ID / mark.",
                    suggestion="Map the correct ID/mark column or fill missing marks.",
                    raw_value=raw_preview,
                )
            )
            continue

        if width is None or width <= 0:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    source=source_name,
                    row_no=row_no,
                    order=order_name,
                    item_id=item_id,
                    problem="Width is missing, zero, or not numeric.",
                    suggestion="Map width column or enter width in inches.",
                    raw_value=raw_preview,
                )
            )
            continue

        if height is None or height <= 0:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    source=source_name,
                    row_no=row_no,
                    order=order_name,
                    item_id=item_id,
                    problem="Height is missing, zero, or not numeric.",
                    suggestion="Map height column or enter height in inches.",
                    raw_value=raw_preview,
                )
            )
            continue

        if qty <= 0:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    source=source_name,
                    row_no=row_no,
                    order=order_name,
                    item_id=item_id,
                    problem="Quantity is zero or negative.",
                    suggestion="Use a positive quantity.",
                    raw_value=raw_preview,
                )
            )
            continue

        calc_depth, calc_lbs = calculate_specs(
            unit_type,
            width,
            height,
            assumptions.glass_kg_m2,
            assumptions.std_weight_multiplier,
            assumptions.lsd_weight_multiplier,
        )

        mapped_depth = safe_float(cell(row, "depth"))
        mapped_weight = safe_float(cell(row, "weight"))

        depth = mapped_depth if mapped_depth and mapped_depth > 0 else calc_depth
        lbs = mapped_weight if mapped_weight and mapped_weight > 0 else calc_lbs

        for i in range(qty):
            generated_id = f"{order_name}|{item_id}#{i + 1}"

            rows.append(
                {
                    "Order": order_name,
                    "ID": generated_id,
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
                    "Source": source_name,
                    "Notes": notes,
                }
            )

    return rows, issues

# ==========================================================
# 7. MASTER TABLE VALIDATION
# ==========================================================

def validation_issues_to_df(
    issues: List[ValidationIssue],
) -> pd.DataFrame:
    if not issues:
        return pd.DataFrame()

    return pd.DataFrame([issue.to_dict() for issue in issues])


def validate_master_dataframe(
    df: pd.DataFrame,
) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []

    if df is None or df.empty:
        issues.append(
            ValidationIssue(
                severity="ERROR",
                source="Master Table",
                row_no=None,
                order="",
                item_id="",
                problem="No unit data loaded.",
                suggestion="Process pasted data or upload a CSV/Excel file.",
            )
        )
        return issues

    required_columns = [
        "Order",
        "ID",
        "W",
        "H",
        "Depth",
        "Lbs",
        "Mode",
        "Orient",
        "SR",
        "SC",
    ]

    for col in required_columns:
        if col not in df.columns:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    source="Master Table",
                    row_no=None,
                    order="",
                    item_id="",
                    problem=f"Missing required column: {col}",
                    suggestion="Reload/process the source data.",
                )
            )

    if issues:
        return issues

    seen_ids: set[str] = set()

    for idx, row in df.iterrows():
        row_no = int(idx) + 1
        order = clean_str(row.get("Order"))
        item_id = clean_str(row.get("ID"))

        if not item_id:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    source="Master Table",
                    row_no=row_no,
                    order=order,
                    item_id="",
                    problem="Generated unit ID is blank.",
                    suggestion="Check the source unit mark.",
                )
            )
        elif item_id in seen_ids:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    source="Master Table",
                    row_no=row_no,
                    order=order,
                    item_id=item_id,
                    problem="Duplicate generated unit ID.",
                    suggestion="Check order names and source marks.",
                )
            )
        else:
            seen_ids.add(item_id)

        for col in ["W", "H", "Depth", "Lbs"]:
            value = safe_float(row.get(col))
            if value is None or value <= 0:
                issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        source="Master Table",
                        row_no=row_no,
                        order=order,
                        item_id=item_id,
                        problem=f"{col} must be positive numeric.",
                        suggestion=f"Correct {col} before optimizing.",
                    )
                )

        mode = clean_str(row.get("Mode")).upper()
        if mode not in {"WHOLE", "SLANT", "DISASSEMBLED"}:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    source="Master Table",
                    row_no=row_no,
                    order=order,
                    item_id=item_id,
                    problem="Invalid Mode.",
                    suggestion="Use WHOLE, SLANT, or DISASSEMBLED.",
                )
            )

        orient = clean_str(row.get("Orient")).upper()
        if orient not in {"AUTO", "UPRIGHT", "SIDE"}:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    source="Master Table",
                    row_no=row_no,
                    order=order,
                    item_id=item_id,
                    problem="Invalid Orient.",
                    suggestion="Use AUTO, UPRIGHT, or SIDE.",
                )
            )

        sr = safe_int(row.get("SR"), 1)
        sc = safe_int(row.get("SC"), 1)

        if sr is None or sr < 1 or sc is None or sc < 1:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    source="Master Table",
                    row_no=row_no,
                    order=order,
                    item_id=item_id,
                    problem="Invalid split rows/columns.",
                    suggestion="SR and SC must be at least 1.",
                )
            )

    return issues



# ==========================================================
# 8. PRACTICAL UNIT ISSUE REPORT
# ==========================================================

def build_unit_issue_report(
    df: pd.DataFrame,
    vehicle_data: Dict[str, float],
    assumptions: LogisticsAssumptions,
) -> pd.DataFrame:
    """
    Builds a practical issue report for oversized/heavy units.

    This is more useful than only saying "too tall" because it shows:
      - whether the unit fits upright
      - whether it fits sideways
      - whether slanting could work
      - estimated slant length
      - projected crate height
      - recommended action
    """
    if df is None or df.empty:
        return pd.DataFrame()

    max_h_int = get_vehicle_internal_crate_height_limit(
        vehicle_data,
        assumptions.vehicle_height_clearance,
    )

    max_h_ext = get_vehicle_external_crate_height_limit(
        vehicle_data,
        assumptions.vehicle_height_clearance,
    )

    issue_rows: List[Dict[str, Any]] = []

    for _, row in df.iterrows():
        order = clean_str(row.get("Order"))
        unit_id = clean_str(row.get("ID"))

        width = float(row.get("W", 0))
        height = float(row.get("H", 0))
        depth = float(row.get("Depth", 0))
        lbs = float(row.get("Lbs", 0))

        mode = clean_str(row.get("Mode")).upper() or "WHOLE"
        orient = clean_str(row.get("Orient")).upper() or "AUTO"

        fits_upright = height <= max_h_int
        fits_sideways = width <= max_h_int

        slant_needed = height > max_h_int
        slant_run = 0.0
        slant_length_ext = ""

        if slant_needed:
            slant_run = math.sqrt(max(0.0, height ** 2 - max_h_int ** 2))
            slant_length_ext = round(width + slant_run + 2 * CRATE_SIDE_CLEAR, 1)

        projected_crate_h_ext = min(height, max_h_int) + 2 * CRATE_SIDE_CLEAR
        projected_crate_w_ext = CRATE_BASE_DEPTH + depth + UNIT_SPACER

        problems: List[str] = []
        recommendations: List[str] = []

        if not fits_upright and fits_sideways:
            problems.append("Too tall upright")
            recommendations.append("Try SIDE orientation if acceptable")

        if not fits_upright and not fits_sideways:
            problems.append("Too tall in both orientations")
            recommendations.append("Use SLANT, split, or DISASSEMBLED")

        if slant_needed and slant_length_ext != "":
            if float(slant_length_ext) > assumptions.crate_max_len_ext:
                problems.append("Slant length exceeds crate length")
                recommendations.append("Split or disassemble")

        if projected_crate_h_ext > max_h_ext:
            problems.append("Projected crate height exceeds selected vehicle")
            recommendations.append("Split, side-orient, or choose taller vehicle")

        if projected_crate_w_ext > assumptions.crate_max_width_ext:
            problems.append("Single-unit crate depth exceeds crate width")
            recommendations.append("Review depth, crate standard, or disassembly")

        if lbs > assumptions.max_crate_lbs:
            problems.append("Unit exceeds max crate weight")
            recommendations.append("Split or disassemble")

        if mode == "WHOLE" and not fits_upright:
            problems.append("WHOLE mode may not fit upright")
            recommendations.append("Change Mode or Orientation before optimizing")

        if not problems:
            continue

        severity = "ERROR" if any(
            word in " ".join(problems).lower()
            for word in ["exceeds", "too tall in both", "weight"]
        ) else "WARNING"

        issue_rows.append(
            {
                "Severity": severity,
                "Order": order,
                "ID": unit_id,
                "W": round(width, 1),
                "H": round(height, 1),
                "Depth": round(depth, 1),
                "Lbs": round(lbs, 0),
                "Mode": mode,
                "Orient": orient,
                "Vehicle Internal H Limit": round(max_h_int, 1),
                "Fits Upright": "YES" if fits_upright else "NO",
                "Fits Sideways": "YES" if fits_sideways else "NO",
                "Slant Length Ext": slant_length_ext,
                "Projected Crate H Ext": round(projected_crate_h_ext, 1),
                "Projected Crate W Ext": round(projected_crate_w_ext, 1),
                "Problem": "; ".join(dict.fromkeys(problems)),
                "Recommendation": "; ".join(dict.fromkeys(recommendations)),
            }
        )

    return pd.DataFrame(issue_rows)


def show_issue_summary(issue_df: pd.DataFrame) -> None:
    """
    Streamlit helper for displaying issue reports.
    """
    if issue_df is None or issue_df.empty:
        st.success("No major dimensional/weight issues detected for the selected vehicle.")
        return

    error_count = len(issue_df[issue_df["Severity"] == "ERROR"])
    warning_count = len(issue_df[issue_df["Severity"] == "WARNING"])

    if error_count:
        st.error(f"{error_count} critical issue(s) found.")

    if warning_count:
        st.warning(f"{warning_count} warning(s) found.")

    st.dataframe(issue_df, use_container_width=True, hide_index=True)

# ==========================================================
# 9. BUILD PIECES FROM MASTER TABLE
# ==========================================================

def build_pieces_from_master(
    df: pd.DataFrame,
    vehicle_data: Dict[str, float],
    assumptions: LogisticsAssumptions,
) -> Tuple[List[Piece], pd.DataFrame]:
    """
    Converts the editable master table into final pieces.

    Returns:
        pieces
        packing_decision_df
    """
    max_h_int = get_vehicle_internal_crate_height_limit(
        vehicle_data,
        assumptions.vehicle_height_clearance,
    )

    pieces: List[Piece] = []
    decision_rows: List[Dict[str, Any]] = []

    for _, row in df.iterrows():
        base_piece = Piece(
            orig_id=clean_str(row["Orig"]),
            piece_id=clean_str(row["ID"]),
            w=float(row["W"]),
            h=float(row["H"]),
            utype=clean_str(row["Type"]),
            d=float(row["Depth"]),
            lbs=float(row["Lbs"]),
            mode=clean_str(row["Mode"]).upper(),
            orientation=clean_str(row["Orient"]).upper(),
            source_order=clean_str(row["Order"]),
        )

        split_rows = safe_int(row.get("SR"), 1) or 1
        split_cols = safe_int(row.get("SC"), 1) or 1

        expanded = expand_manual_split(
            base_piece,
            split_rows,
            split_cols,
            base_piece.mode,
            base_piece.orientation,
            assumptions.frame_kit_weight_pct,
            max_h_int,
        )

        pieces.extend(expanded)

        for p in expanded:
            decision_rows.append(
                {
                    "Order": p.source_order,
                    "Original Unit": base_piece.piece_id,
                    "Final Piece": p.piece_id,
                    "Mode": p.mode,
                    "Orientation": p.resolved_orientation(max_h_int),
                    "W": round(p.w, 1),
                    "H": round(p.h, 1),
                    "Depth": round(p.d, 2),
                    "Lbs": round(p.lbs, 1),
                    "Split Rows": split_rows,
                    "Split Cols": split_cols,
                    "Internal Crate H Limit": round(max_h_int, 1),
                    "Required Crate Length Int": round(p.crate_len_need_int(max_h_int), 1),
                    "Effective Vertical H": round(p.crate_eff_vertical_h(max_h_int), 1),
                }
            )

    return pieces, pd.DataFrame(decision_rows)

# ==========================================================
# 10. PALLETIZE AND CRATE PIECES
# ==========================================================

def group_pieces_for_packing(
    pieces: List[Piece],
    no_mixing_orders: bool,
) -> Dict[str, List[Piece]]:
    """
    Groups pieces by order if no-mixing is enabled.
    Otherwise uses one mixed group.
    """
    grouped: Dict[str, List[Piece]] = {}

    for p in pieces:
        group_key = p.source_order if no_mixing_orders else "MIXED ORDERS"
        grouped.setdefault(group_key, []).append(p)

    return grouped


def palletize_disassembled_pieces(
    order_name: str,
    pieces: List[Piece],
    selected_pallet_names: List[str],
    pallet_catalog: Dict[str, Dict[str, float]],
    assumptions: LogisticsAssumptions,
) -> Tuple[List[PalletObject], List[Piece]]:
    """
    Attempts to place DISASSEMBLED pieces on pallets.

    Returns:
        pallets
        leftover pieces that could not be palletized
    """
    pallets: List[PalletObject] = []
    leftovers: List[Piece] = []

    if not assumptions.allow_pallets_for_disassembled:
        return pallets, pieces

    if not selected_pallet_names:
        return pallets, pieces

    sorted_pieces = sorted(
        pieces,
        key=lambda x: x.w * x.h,
        reverse=True,
    )

    for p in sorted_pieces:
        placed = False

        for pallet in pallets:
            if pallet.place(p):
                placed = True
                break

        if placed:
            continue

        for pallet_name in selected_pallet_names:
            spec = pallet_catalog[pallet_name]
            max_lbs = min(
                float(spec.get("max_lbs", assumptions.max_pallet_lbs)),
                assumptions.max_pallet_lbs,
            )

            new_pallet = PalletObject(
                order=order_name,
                name=pallet_name,
                L=float(spec["L"]),
                W=float(spec["W"]),
                H=float(spec.get("H", PALLET_H)),
                max_wgt=max_lbs,
            )

            if new_pallet.place(p):
                pallets.append(new_pallet)
                placed = True
                break

        if not placed:
            leftovers.append(p)

    return pallets, leftovers


def crate_pieces(
    order_name: str,
    pieces: List[Piece],
    vehicle_data: Dict[str, float],
    assumptions: LogisticsAssumptions,
) -> Tuple[List[Crate], List[Dict[str, Any]]]:
    """
    Creates crates from pieces.

    Uses selected vehicle height to determine allowed crate height.
    """
    max_h_int = get_vehicle_internal_crate_height_limit(
        vehicle_data,
        assumptions.vehicle_height_clearance,
    )

    max_h_ext = get_vehicle_external_crate_height_limit(
        vehicle_data,
        assumptions.vehicle_height_clearance,
    )

    crates: List[Crate] = []
    packing_notes: List[Dict[str, Any]] = []

    sorted_pieces = sorted(
        pieces,
        key=lambda x: (
            x.oriented_vertical(max_h_int),
            x.crate_len_need_int(max_h_int),
            x.lbs,
        ),
        reverse=True,
    )

    current = Crate(order=order_name)

    for p in sorted_pieces:
        can_add, reason = current.can_add(
            p=p,
            max_h_int=max_h_int,
            max_crate_lbs=assumptions.max_crate_lbs,
            max_len_ext=assumptions.crate_max_len_ext,
            max_width_ext=assumptions.crate_max_width_ext,
            max_height_ext=max_h_ext,
        )

        if can_add:
            current.add(p, max_h_int)
            packing_notes.append(
                {
                    "Piece": p.piece_id,
                    "Assigned": "Current crate",
                    "Reason": "fits",
                }
            )
            continue

        if current.pieces:
            crates.append(current)
            current = Crate(order=order_name)

        can_add_empty, reason_empty = current.can_add(
            p=p,
            max_h_int=max_h_int,
            max_crate_lbs=assumptions.max_crate_lbs,
            max_len_ext=assumptions.crate_max_len_ext,
            max_width_ext=assumptions.crate_max_width_ext,
            max_height_ext=max_h_ext,
        )

        if can_add_empty:
            current.add(p, max_h_int)
            packing_notes.append(
                {
                    "Piece": p.piece_id,
                    "Assigned": "New crate",
                    "Reason": reason,
                }
            )
        else:
            # Still put it in its own crate so it is visible in manifest,
            # but flag it as a problem.
            problem_crate = Crate(order=order_name)
            problem_crate.add(p, max_h_int)
            crates.append(problem_crate)

            packing_notes.append(
                {
                    "Piece": p.piece_id,
                    "Assigned": "Problem crate",
                    "Reason": f"Does not meet crate limits: {reason_empty}",
                }
            )

            current = Crate(order=order_name)

    if current.pieces:
        crates.append(current)

    return crates, packing_notes

# ==========================================================
# 11. 2D CONTAINER FLOOR PACKING
# ==========================================================

def pack_items_2d_maxrects(
    items: List[Dict[str, Any]],
    container_L: float,
    container_W: float,
    clearance: float,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Simple 2D max-rects style floor-packing heuristic.

    Returns:
        placed items
        overflow items
    """
    placed: List[Dict[str, Any]] = []
    overflow: List[Dict[str, Any]] = []

    free_rects: List[Dict[str, float]] = [
        {
            "x": 0.0,
            "y": 0.0,
            "L": container_L,
            "W": container_W,
        }
    ]

    sorted_items = sorted(
        [dict(item) for item in items],
        key=lambda x: x["L"] * x["W"],
        reverse=True,
    )

    for item in sorted_items:
        best_fit = None
        best_short_side = float("inf")

        req_L = float(item["L"]) + clearance
        req_W = float(item["W"]) + clearance

        orientations = [
            (req_L, req_W, float(item["L"]), float(item["W"])),
            (req_W, req_L, float(item["W"]), float(item["L"])),
        ]

        for fr in free_rects:
            for used_L, used_W, actual_L, actual_W in orientations:
                if used_L <= fr["L"] and used_W <= fr["W"]:
                    short_side = min(
                        fr["L"] - used_L,
                        fr["W"] - used_W,
                    )

                    if short_side < best_short_side:
                        best_short_side = short_side
                        best_fit = {
                            "x": fr["x"],
                            "y": fr["y"],
                            "used_L": used_L,
                            "used_W": used_W,
                            "actual_L": actual_L,
                            "actual_W": actual_W,
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
            }
        )
        placed.append(placed_item)

        used_x = best_fit["x"]
        used_y = best_fit["y"]
        used_L = best_fit["used_L"]
        used_W = best_fit["used_W"]

        new_free: List[Dict[str, float]] = []

        for fr in free_rects:
            no_overlap = (
                used_x >= fr["x"] + fr["L"]
                or used_x + used_L <= fr["x"]
                or used_y >= fr["y"] + fr["W"]
                or used_y + used_W <= fr["y"]
            )

            if no_overlap:
                new_free.append(fr)
                continue

            # Left remainder.
            if used_x > fr["x"]:
                new_free.append(
                    {
                        "x": fr["x"],
                        "y": fr["y"],
                        "L": used_x - fr["x"],
                        "W": fr["W"],
                    }
                )

            # Right remainder.
            if used_x + used_L < fr["x"] + fr["L"]:
                new_free.append(
                    {
                        "x": used_x + used_L,
                        "y": fr["y"],
                        "L": (fr["x"] + fr["L"]) - (used_x + used_L),
                        "W": fr["W"],
                    }
                )

            # Bottom remainder.
            if used_y > fr["y"]:
                new_free.append(
                    {
                        "x": fr["x"],
                        "y": fr["y"],
                        "L": fr["L"],
                        "W": used_y - fr["y"],
                    }
                )

            # Top remainder.
            if used_y + used_W < fr["y"] + fr["W"]:
                new_free.append(
                    {
                        "x": fr["x"],
                        "y": used_y + used_W,
                        "L": fr["L"],
                        "W": (fr["y"] + fr["W"]) - (used_y + used_W),
                    }
                )

        free_rects = [
            r
            for r in new_free
            if r["L"] > 1.0 and r["W"] > 1.0
        ]

    return placed, overflow

def pack_across_multiple_containers(
    items: List[Dict[str, Any]],
    vehicle_name: str,
    vehicle_data: Dict[str, float],
    assumptions: LogisticsAssumptions,
) -> List[Dict[str, Any]]:
    """
    Repeats 2D floor packing until all items are placed or no progress is possible.
    """
    container_L = float(vehicle_data["L"])
    container_W = float(vehicle_data["W"])
    container_H = float(vehicle_data["H"])
    container_payload = float(vehicle_data.get("max_lbs", 0) or 0)

    remaining = [dict(item) for item in items]
    loads: List[Dict[str, Any]] = []

    max_loops = 200

    for container_no in range(1, max_loops + 1):
        if not remaining:
            break

        # Items too tall for this vehicle should immediately go to overflow.
        height_ok = []
        height_bad = []

        for item in remaining:
            item_h = float(item.get("H", 0))
            if item_h <= container_H - assumptions.vehicle_height_clearance:
                height_ok.append(item)
            else:
                height_bad.append(item)

        placed, overflow = pack_items_2d_maxrects(
            height_ok,
            container_L,
            container_W,
            assumptions.container_item_clearance,
        )

        # Payload check is warning-oriented.
        # The current heuristic does not reshuffle by weight,
        # but it reports overloaded containers.
        load_weight = sum(float(x.get("weight", 0)) for x in placed)
        payload_over = (
            container_payload > 0
            and load_weight > container_payload
        )

        floor_area = container_L * container_W
        used_area = sum(
            float(x["L"]) * float(x["W"])
            for x in placed
        )

        util = (used_area / floor_area * 100.0) if floor_area > 0 else 0.0

        loads.append(
            {
                "vehicle_name": vehicle_name,
                "container_no": container_no,
                "placed": placed,
                "overflow": overflow + height_bad,
                "util": util,
                "weight": load_weight,
                "payload_over": payload_over,
                "payload_limit": container_payload,
            }
        )

        if not placed:
            break

        remaining = overflow + height_bad

    return loads

# ==========================================================
# 12. MAIN OPTIMIZATION / PLAN BUILDER
# ==========================================================

def build_package_contents_map(
    pallets: List[PalletObject],
    crates: List[Crate],
) -> Dict[str, str]:
    """
    Maps every final piece ID to its assigned pallet/crate ID.
    """
    assignment: Dict[str, str] = {}

    for i, pallet in enumerate(pallets):
        package_id = f"P{i + 1}"
        for p in pallet.pieces:
            assignment[p.piece_id] = package_id

    for i, crate in enumerate(crates):
        package_id = f"C{i + 1}"
        for p in crate.pieces:
            assignment[p.piece_id] = package_id

    return assignment


def build_manifest_df(
    pallets: List[PalletObject],
    crates: List[Crate],
    vehicle_data: Dict[str, float],
    assumptions: LogisticsAssumptions,
) -> pd.DataFrame:
    """
    Creates detailed manifest table for pallets and crates.
    """
    rows: List[Dict[str, Any]] = []

    max_h_ext = get_vehicle_external_crate_height_limit(
        vehicle_data,
        assumptions.vehicle_height_clearance,
    )

    for i, pallet in enumerate(pallets):
        contents = ", ".join([p.piece_id for p in pallet.pieces])

        status = "OK"
        if pallet.weight > pallet.max_wgt:
            status = "OVER PALLET WEIGHT"

        rows.append(
            {
                "Order": pallet.order,
                "ID": f"P{i + 1}",
                "Type": "PALLET",
                "Weight": round(pallet.weight, 0),
                "Dims": dim_text(pallet.L, pallet.W, pallet.H),
                "L": round(pallet.L, 1),
                "W": round(pallet.W, 1),
                "H": round(pallet.H, 1),
                "Status": status,
                "Contents": contents,
            }
        )

    for i, crate in enumerate(crates):
        contents = ", ".join([p.piece_id for p in crate.pieces])

        status_items: List[str] = []

        if crate.weight > assumptions.max_crate_lbs:
            status_items.append("OVER CRATE WEIGHT")

        if crate.L_ext > assumptions.crate_max_len_ext:
            status_items.append("OVER CRATE LENGTH")

        if crate.W_ext > assumptions.crate_max_width_ext:
            status_items.append("OVER CRATE WIDTH")

        if crate.H_ext > max_h_ext:
            status_items.append("OVER VEHICLE HEIGHT")

        status = "; ".join(status_items) if status_items else "OK"

        rows.append(
            {
                "Order": crate.order,
                "ID": f"C{i + 1}",
                "Type": "CRATE",
                "Weight": round(crate.weight, 0),
                "Dims": dim_text(crate.L_ext, crate.W_ext, crate.H_ext),
                "L": round(crate.L_ext, 1),
                "W": round(crate.W_ext, 1),
                "H": round(crate.H_ext, 1),
                "Status": status,
                "Contents": contents,
            }
        )

    return pd.DataFrame(rows)


def build_container_items_from_manifest(
    manifest_df: pd.DataFrame,
) -> List[Dict[str, Any]]:
    """
    Converts manifest rows into floor-packing objects.
    """
    items: List[Dict[str, Any]] = []

    if manifest_df.empty:
        return items

    for _, row in manifest_df.iterrows():
        package_type = clean_str(row["Type"])
        package_id = clean_str(row["ID"])

        items.append(
            {
                "kind": package_type,
                "package_id": package_id,
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


def build_load_summary_df(
    loads: List[Dict[str, Any]],
) -> pd.DataFrame:
    """
    Creates one-row-per-container summary.
    """
    rows: List[Dict[str, Any]] = []

    for load in loads:
        placed = load.get("placed", [])
        overflow = load.get("overflow", [])

        package_ids = [
            clean_str(x.get("package_id"))
            for x in placed
        ]

        rows.append(
            {
                "Container #": load["container_no"],
                "Vehicle": load["vehicle_name"],
                "Packages Loaded": len(placed),
                "Packages Remaining After This Container": len(overflow),
                "Weight": round(float(load.get("weight", 0)), 0),
                "Payload Limit": round(float(load.get("payload_limit", 0)), 0),
                "Payload Status": "OVER PAYLOAD" if load.get("payload_over") else "OK",
                "Floor Utilization %": round(float(load.get("util", 0)), 1),
                "Packages": ", ".join(package_ids),
            }
        )

    return pd.DataFrame(rows)


def assign_packages_to_decision_df(
    decision_df: pd.DataFrame,
    package_map: Dict[str, str],
) -> pd.DataFrame:
    """
    Adds final package assignment to the piece decision table.
    """
    if decision_df.empty:
        return decision_df

    out = decision_df.copy()

    out["Assigned Package"] = out["Final Piece"].map(
        lambda piece_id: package_map.get(piece_id, "UNASSIGNED")
    )

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
    Full end-to-end optimizer.

    Steps:
      1. Validate editable master table
      2. Expand rows into final pieces
      3. Keep orders separated if configured
      4. Palletize disassembled pieces
      5. Crate all remaining pieces
      6. Pack packages into one or more containers
      7. Return all result tables and visualization data
    """
    validation_issues = validate_master_dataframe(master_df)

    blocking_errors = [
        issue
        for issue in validation_issues
        if issue.severity == "ERROR"
    ]

    if blocking_errors:
        return {
            "ok": False,
            "errors": validation_issues_to_df(validation_issues),
            "message": "Fix validation errors before optimizing.",
        }

    pieces, decision_df = build_pieces_from_master(
        master_df,
        vehicle_data,
        assumptions,
    )

    pieces_by_group = group_pieces_for_packing(
        pieces,
        assumptions.no_mixing_orders,
    )

    all_pallets: List[PalletObject] = []
    all_crates: List[Crate] = []
    packing_notes: List[Dict[str, Any]] = []

    for group_name, group_pieces in pieces_by_group.items():
        disassembled = [
            p for p in group_pieces
            if p.mode == "DISASSEMBLED"
        ]

        crate_candidates = [
            p for p in group_pieces
            if p.mode != "DISASSEMBLED"
        ]

        pallets, pallet_leftovers = palletize_disassembled_pieces(
            order_name=group_name,
            pieces=disassembled,
            selected_pallet_names=selected_pallet_names,
            pallet_catalog=pallet_catalog,
            assumptions=assumptions,
        )

        all_pallets.extend(pallets)

        crate_feed = crate_candidates + pallet_leftovers

        crates, crate_notes = crate_pieces(
            order_name=group_name,
            pieces=crate_feed,
            vehicle_data=vehicle_data,
            assumptions=assumptions,
        )

        all_crates.extend(crates)

        for note in crate_notes:
            note["Group"] = group_name
            packing_notes.append(note)

    package_map = build_package_contents_map(
        all_pallets,
        all_crates,
    )

    decision_df = assign_packages_to_decision_df(
        decision_df,
        package_map,
    )

    manifest_df = build_manifest_df(
        all_pallets,
        all_crates,
        vehicle_data,
        assumptions,
    )

    items = build_container_items_from_manifest(manifest_df)

    loads = pack_across_multiple_containers(
        items=items,
        vehicle_name=vehicle_name,
        vehicle_data=vehicle_data,
        assumptions=assumptions,
    )

    load_summary_df = build_load_summary_df(loads)

    order_color_map = build_order_color_map(
        list(master_df["Order"].astype(str).unique())
    )

    packing_notes_df = pd.DataFrame(packing_notes)

    final_overflow = loads[-1]["overflow"] if loads else []

    result = {
        "ok": True,
        "message": "Optimization complete.",
        "vehicle_name": vehicle_name,
        "vehicle_data": vehicle_data,
        "assumptions": assumptions,
        "pieces": pieces,
        "pallets": all_pallets,
        "crates": all_crates,
        "loads": loads,
        "detail_df": manifest_df,
        "manifest_df": manifest_df,
        "decision_df": decision_df,
        "packing_notes_df": packing_notes_df,
        "load_summary_df": load_summary_df,
        "order_color_map": order_color_map,
        "final_overflow": final_overflow,
        "validation_issues_df": validation_issues_to_df(validation_issues),
    }

    return result

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
    Creates a copy of the master table with simple strategy changes.

    Scenario options:
      - Current Settings
      - Auto Slant Tall Units
      - Disassemble Oversized Units
      - Split Oversized Units 2x1
    """
    df = base_df.copy()

    if df.empty:
        return df

    max_h_int = get_vehicle_internal_crate_height_limit(
        vehicle_data,
        assumptions.vehicle_height_clearance,
    )

    if scenario_name == "Current Settings":
        return df

    for idx, row in df.iterrows():
        width = float(row["W"])
        height = float(row["H"])
        lbs = float(row["Lbs"])

        too_tall = height > max_h_int and width > max_h_int
        too_heavy = lbs > assumptions.max_crate_lbs
        needs_handling = too_tall or too_heavy

        if scenario_name == "Auto Slant Tall Units":
            if height > max_h_int:
                df.at[idx, "Mode"] = "SLANT"

        elif scenario_name == "Disassemble Oversized Units":
            if needs_handling:
                df.at[idx, "Mode"] = "DISASSEMBLED"

        elif scenario_name == "Split Oversized Units 2x1":
            if needs_handling:
                df.at[idx, "SR"] = 2
                df.at[idx, "SC"] = 1
                df.at[idx, "Mode"] = "WHOLE"

    return df


def summarize_plan_for_scenario(
    scenario_name: str,
    vehicle_name: str,
    plan: Dict[str, Any],
) -> Dict[str, Any]:
    if not plan.get("ok"):
        return {
            "Scenario": scenario_name,
            "Vehicle": vehicle_name,
            "Status": "ERROR",
            "Containers": "",
            "Crates": "",
            "Pallets": "",
            "Total Weight": "",
            "Avg Floor Util %": "",
            "Unpacked Items": "",
            "Warnings": plan.get("message", "Could not optimize"),
        }

    loads = plan["loads"]
    detail_df = plan["detail_df"]
    final_overflow = plan["final_overflow"]

    total_weight = 0.0
    if not detail_df.empty:
        total_weight = float(detail_df["Weight"].astype(float).sum())

    avg_util = 0.0
    if loads:
        avg_util = sum(float(ld["util"]) for ld in loads) / len(loads)

    over_payload_count = sum(
        1 for ld in loads
        if ld.get("payload_over")
    )

    bad_manifest_count = 0
    if not detail_df.empty and "Status" in detail_df.columns:
        bad_manifest_count = len(
            detail_df[detail_df["Status"].astype(str) != "OK"]
        )

    warnings: List[str] = []

    if over_payload_count:
        warnings.append(f"{over_payload_count} container(s) over payload")

    if bad_manifest_count:
        warnings.append(f"{bad_manifest_count} package issue(s)")

    if final_overflow:
        warnings.append(f"{len(final_overflow)} unpacked item(s)")

    return {
        "Scenario": scenario_name,
        "Vehicle": vehicle_name,
        "Status": "OK" if not warnings else "REVIEW",
        "Containers": len(loads),
        "Crates": len(plan["crates"]),
        "Pallets": len(plan["pallets"]),
        "Total Weight": round(total_weight, 0),
        "Avg Floor Util %": round(avg_util, 1),
        "Unpacked Items": len(final_overflow),
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
    """
    Runs multiple strategy + vehicle combinations and returns summary.
    """
    rows: List[Dict[str, Any]] = []

    for vehicle_name in selected_vehicle_names:
        vehicle_data = vehicle_catalog[vehicle_name]

        for strategy_name in selected_strategy_names:
            scenario_df = create_scenario_master_df(
                base_df=master_df,
                scenario_name=strategy_name,
                vehicle_data=vehicle_data,
                assumptions=assumptions,
            )

            plan = build_logistics_plan(
                master_df=scenario_df,
                vehicle_name=vehicle_name,
                vehicle_data=vehicle_data,
                selected_pallet_names=selected_pallet_names,
                pallet_catalog=pallet_catalog,
                assumptions=assumptions,
            )

            rows.append(
                summarize_plan_for_scenario(
                    scenario_name=strategy_name,
                    vehicle_name=vehicle_name,
                    plan=plan,
                )
            )

    return pd.DataFrame(rows)

# ==========================================================
# 14. VISUALIZATION HELPERS
# ==========================================================

def build_2d_plan(
    placed_items: List[Dict[str, Any]],
    container_L: float,
    container_W: float,
    order_color_map: Dict[str, str],
    title_text: str = "",
) -> go.Figure:
    """
    Interactive 2D floor plan using Plotly.
    """
    fig = go.Figure()

    fig.add_shape(
        type="rect",
        x0=0,
        y0=0,
        x1=container_L,
        y1=container_W,
        line=dict(color="black", width=4),
        fillcolor="rgba(240,240,240,0.5)",
    )

    for item in placed_items:
        order = clean_str(item.get("order"))
        base_hex = order_color_map.get(order, "#888888")

        fill_alpha = 0.55 if item["kind"] == "PALLET" else 0.35
        fill_color = hex_to_rgba(base_hex, fill_alpha)

        fig.add_shape(
            type="rect",
            x0=item["x"],
            y0=item["y"],
            x1=item["x"] + item["L"],
            y1=item["y"] + item["W"],
            fillcolor=fill_color,
            line=dict(color="black", width=2),
        )

        label = clean_str(item.get("package_id"))
        if order:
            label = f"{label}<br>{order}"

        hover_text = (
            f"Package: {clean_str(item.get('package_id'))}<br>"
            f"Type: {clean_str(item.get('kind'))}<br>"
            f"Order: {order}<br>"
            f"Dims: {dim_text(float(item['L']), float(item['W']), float(item.get('H', 0)))}<br>"
            f"Weight: {pounds_text(float(item.get('weight', 0)))}"
        )

        fig.add_trace(
            go.Scatter(
                x=[item["x"] + item["L"] / 2],
                y=[item["y"] + item["W"] / 2],
                mode="text",
                text=[f"<b>{label}</b>"],
                textfont=dict(color="white", size=12),
                hovertext=[hover_text],
                hoverinfo="text",
                showlegend=False,
            )
        )

    fig.update_layout(
        title=title_text,
        xaxis=dict(
            range=[-10, container_L + 10],
            title="Length (in)",
        ),
        yaxis=dict(
            range=[-10, container_W + 10],
            title="Width (in)",
            scaleanchor="x",
        ),
        height=520,
        margin=dict(l=20, r=20, t=50, b=20),
    )

    return fig

def render_mpl_2d_plan(
    placed_items: List[Dict[str, Any]],
    container_L: float,
    container_W: float,
    order_color_map: Dict[str, str],
    title_text: str = "",
) -> BytesIO:
    """
    Matplotlib fallback renderer for PDF export.
    Useful when Plotly/Kaleido image export is unavailable.
    """
    fig = Figure(figsize=(10, 4))
    ax = fig.add_subplot(111)

    ax.add_patch(
        patches.Rectangle(
            (0, 0),
            container_L,
            container_W,
            linewidth=2,
            edgecolor="black",
            facecolor="#f9f9f9",
        )
    )

    for item in placed_items:
        order = clean_str(item.get("order"))
        base_hex = order_color_map.get(order, "#888888")
        alpha = 0.55 if item["kind"] == "PALLET" else 0.35

        rect = patches.Rectangle(
            (item["x"], item["y"]),
            item["L"],
            item["W"],
            linewidth=1,
            edgecolor="black",
            facecolor=base_hex,
            alpha=alpha,
        )
        ax.add_patch(rect)

        center_x = item["x"] + item["L"] / 2
        center_y = item["y"] + item["W"] / 2
        label = clean_str(item.get("package_id"))

        ax.text(
            center_x,
            center_y,
            label,
            ha="center",
            va="center",
            color="white",
            fontsize=8,
            fontweight="bold",
        )

    ax.set_xlim(-10, container_L + 10)
    ax.set_ylim(-10, container_W + 10)
    ax.set_aspect("equal")
    ax.set_title(title_text)
    ax.set_xlabel("Length (in)")
    ax.set_ylabel("Width (in)")

    canvas = FigureCanvasAgg(fig)
    buf = BytesIO()

    fig.tight_layout()
    canvas.print_png(buf)
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
    slant_extra: float = 0.0,
    target_h: float = 0.0,
    hover_text: str = "",
) -> None:
    """
    Adds a rectangular or slanted prism to a Plotly 3D figure.
    """
    if slant_extra > 0:
        v = [
            (x, y, z),
            (x + length, y, z),
            (x + length, y + width, z),
            (x, y + width, z),
            (x + slant_extra, y, z + target_h),
            (x + length + slant_extra, y, z + target_h),
            (x + length + slant_extra, y + width, z + target_h),
            (x + slant_extra, y + width, z + target_h),
        ]
    else:
        v = [
            (x, y, z),
            (x + length, y, z),
            (x + length, y + width, z),
            (x, y + width, z),
            (x, y, z + height),
            (x + length, y, z + height),
            (x + length, y + width, z + height),
            (x, y + width, z + height),
        ]

    xs = [point[0] for point in v]
    ys = [point[1] for point in v]
    zs = [point[2] for point in v]

    i = [7, 0, 0, 0, 4, 4, 6, 6, 4, 0, 3, 2]
    j = [3, 4, 1, 2, 5, 6, 5, 2, 0, 1, 6, 3]
    k = [0, 7, 2, 3, 6, 7, 1, 1, 5, 5, 7, 6]

    fig.add_trace(
        go.Mesh3d(
            x=xs,
            y=ys,
            z=zs,
            i=i,
            j=j,
            k=k,
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
    Builds 3D view of one selected container load.
    """
    container_L = float(vehicle_data["L"])
    container_W = float(vehicle_data["W"])
    container_H = float(vehicle_data["H"])

    fig = go.Figure()

    add_3d_prism(
        fig,
        0,
        0,
        0,
        container_L,
        container_W,
        container_H,
        "gray",
        0.05,
        "Vehicle Shell",
        hover_text="Vehicle Shell",
    )

    pallets: List[PalletObject] = result["pallets"]
    crates: List[Crate] = result["crates"]

    package_lookup: Dict[str, Any] = {}

    for i, pallet in enumerate(pallets):
        package_lookup[f"P{i + 1}"] = pallet

    for i, crate in enumerate(crates):
        package_lookup[f"C{i + 1}"] = crate

    max_h_int = get_vehicle_internal_crate_height_limit(
        vehicle_data,
        result["assumptions"].vehicle_height_clearance,
    )

    for item in load.get("placed", []):
        order_name = clean_str(item.get("order"))
        package_id = clean_str(item.get("package_id"))
        base_hex = order_color_map.get(order_name, "#888888")
        obj = package_lookup.get(package_id)

        if item["kind"] == "PALLET":
            add_3d_prism(
                fig,
                item["x"],
                item["y"],
                0,
                item["L"],
                item["W"],
                item["H"],
                base_hex,
                0.35,
                package_id,
                hover_text=f"{package_id}: Pallet ({order_name})",
            )

        elif item["kind"] == "CRATE":
            add_3d_prism(
                fig,
                item["x"],
                item["y"],
                0,
                item["L"],
                item["W"],
                item["H"],
                base_hex,
                0.22,
                package_id,
                hover_text=f"{package_id}: Crate ({order_name})",
            )

            if isinstance(obj, Crate):
                current_y = item["y"] + CRATE_SIDE_CLEAR

                for p in obj.pieces:
                    vertical = p.oriented_vertical(max_h_int)
                    slant_extra = 0.0

                    if p.mode == "SLANT" and vertical > max_h_int:
                        slant_extra = math.sqrt(
                            max(0.0, vertical ** 2 - max_h_int ** 2)
                        )

                    shown_h = max_h_int if slant_extra > 0 else vertical

                    add_3d_prism(
                        fig,
                        item["x"] + CRATE_SIDE_CLEAR,
                        current_y,
                        CRATE_SIDE_CLEAR,
                        p.oriented_base(max_h_int),
                        p.d,
                        shown_h,
                        "royalblue",
                        0.8,
                        p.piece_id,
                        slant_extra,
                        shown_h,
                        hover_text=(
                            f"{p.piece_id}<br>"
                            f"Mode: {p.mode}<br>"
                            f"Dims: {dim_text(p.w, p.h, p.d)}<br>"
                            f"Weight: {pounds_text(p.lbs)}"
                        ),
                    )

                    current_y += p.d + UNIT_SPACER

    fig.update_layout(
        scene=dict(aspectmode="data"),
        height=620,
        margin=dict(l=0, r=0, b=0, t=0),
    )

    return fig

# ==========================================================
# 15. PDF EXPORT
# ==========================================================

def paragraph_safe(value: Any) -> str:
    """
    Basic escaping for ReportLab Paragraph text.
    """
    text = clean_str(value)
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


def df_to_reportlab_table(
    df: pd.DataFrame,
    max_rows: int = 60,
    font_size: int = 7,
) -> Table:
    """
    Converts a DataFrame to a ReportLab table.
    Keeps the PDF from exploding on very large content.
    """
    if df is None or df.empty:
        data = [["No data"]]
    else:
        shown = df.head(max_rows).copy()

        for col in shown.columns:
            shown[col] = shown[col].astype(str).map(
                lambda x: x[:250] + "..." if len(x) > 250 else x
            )

        data = [shown.columns.tolist()] + shown.values.tolist()

        if len(df) > max_rows:
            data.append(
                [
                    f"... {len(df) - max_rows} additional row(s) not shown in PDF"
                ]
                + [""] * (len(shown.columns) - 1)
            )

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
) -> Table:
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
            (
                f"{vehicle_name} — "
                f'{vehicle_data["L"]:.0f}"L x '
                f'{vehicle_data["W"]:.0f}"W x '
                f'{vehicle_data["H"]:.0f}"H'
            ),
        ],
        ["Vehicle Payload", pounds_text(float(vehicle_data.get("max_lbs", 0)))],
        ["Max Crate Weight", pounds_text(assumptions.max_crate_lbs)],
        ["Max Pallet Weight", pounds_text(assumptions.max_pallet_lbs)],
        ["Height Clearance", inches_text(assumptions.vehicle_height_clearance)],
        ["Planning Note", assumptions.planning_warning],
    ]

    table = Table(data, colWidths=[130, 570])

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

def generate_pdf_report(
    project: ProjectMeta,
    result: Dict[str, Any],
) -> bytes:
    """
    Generates a PDF logistics manifest.

    Includes:
      - cover/project metadata
      - assumptions/warning
      - container summary
      - package manifest
      - decision table
      - 2D plan for each container
    """
    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(letter),
        rightMargin=24,
        leftMargin=24,
        topMargin=24,
        bottomMargin=24,
    )

    styles = getSampleStyleSheet()

    small_style = ParagraphStyle(
        "Small",
        parent=styles["Normal"],
        fontSize=7,
        leading=9,
    )

    story: List[Any] = []

    vehicle_name = result["vehicle_name"]
    vehicle_data = result["vehicle_data"]
    assumptions: LogisticsAssumptions = result["assumptions"]

    manifest_df = result["manifest_df"]
    load_summary_df = result["load_summary_df"]
    decision_df = result["decision_df"]
    order_color_map = result["order_color_map"]
    loads = result["loads"]

    story.append(
        Paragraph(
            f"Logistics Manifest: {paragraph_safe(project.display_name())}",
            styles["Title"],
        )
    )
    story.append(Spacer(1, 10))

    story.append(
        Paragraph(
            paragraph_safe(assumptions.planning_warning),
            small_style,
        )
    )
    story.append(Spacer(1, 10))

    story.append(
        build_pdf_meta_table(
            project,
            vehicle_name,
            vehicle_data,
            assumptions,
        )
    )

    story.append(Spacer(1, 14))

    story.append(Paragraph("Container Summary", styles["Heading2"]))
    story.append(Spacer(1, 6))
    story.append(df_to_reportlab_table(load_summary_df, max_rows=80, font_size=7))

    story.append(Spacer(1, 14))

    story.append(Paragraph("Package Manifest", styles["Heading2"]))
    story.append(Spacer(1, 6))

    manifest_pdf_cols = [
        col
        for col in [
            "Order",
            "ID",
            "Type",
            "Weight",
            "Dims",
            "Status",
            "Contents",
        ]
        if col in manifest_df.columns
    ]

    story.append(
        df_to_reportlab_table(
            manifest_df[manifest_pdf_cols] if not manifest_df.empty else manifest_df,
            max_rows=80,
            font_size=6,
        )
    )

    story.append(PageBreak())

    story.append(Paragraph("Packing Decisions", styles["Heading2"]))
    story.append(Spacer(1, 6))

    decision_pdf_cols = [
        col
        for col in [
            "Order",
            "Original Unit",
            "Final Piece",
            "Mode",
            "Orientation",
            "W",
            "H",
            "Depth",
            "Lbs",
            "Assigned Package",
        ]
        if col in decision_df.columns
    ]

    story.append(
        df_to_reportlab_table(
            decision_df[decision_pdf_cols] if not decision_df.empty else decision_df,
            max_rows=100,
            font_size=6,
        )
    )

    story.append(PageBreak())

    container_L = float(vehicle_data["L"])
    container_W = float(vehicle_data["W"])

    for load in loads:
        title = (
            f"Container #{load['container_no']} — "
            f"Floor Utilization: {load['util']:.1f}% — "
            f"Weight: {pounds_text(float(load.get('weight', 0)))}"
        )

        story.append(Paragraph(paragraph_safe(title), styles["Heading2"]))
        story.append(Spacer(1, 8))

        if load.get("payload_over"):
            story.append(
                Paragraph(
                    "WARNING: This container is over the configured payload limit.",
                    styles["Heading4"],
                )
            )
            story.append(Spacer(1, 6))

        fig2d = build_2d_plan(
            load["placed"],
            container_L,
            container_W,
            order_color_map,
            title_text=f"Container #{load['container_no']}",
        )

        try:
            img_bytes = fig2d.to_image(
                format="png",
                width=900,
                height=380,
                scale=2,
            )
            story.append(RLImage(BytesIO(img_bytes), width=680, height=290))

        except Exception:
            try:
                mpl_buf = render_mpl_2d_plan(
                    load["placed"],
                    container_L,
                    container_W,
                    order_color_map,
                    title_text=f"Container #{load['container_no']}",
                )
                story.append(RLImage(mpl_buf, width=680, height=290))

            except Exception as e:
                story.append(
                    Paragraph(
                        paragraph_safe(f"[Image generation failed: {e}]"),
                        styles["Italic"],
                    )
                )

        story.append(Spacer(1, 10))

        if load["container_no"] != loads[-1]["container_no"]:
            story.append(PageBreak())

    final_overflow = result.get("final_overflow", [])

    if final_overflow:
        story.append(PageBreak())
        story.append(Paragraph("Unpacked / Overflow Items", styles["Heading2"]))
        story.append(Spacer(1, 8))
        story.append(
            df_to_reportlab_table(
                pd.DataFrame(final_overflow),
                max_rows=100,
                font_size=7,
            )
        )

    doc.build(story)

    return buffer.getvalue()

# ==========================================================
# 16. EXCEL EXPORT
# ==========================================================

def safe_sheet_name(name: str) -> str:
    """
    Excel sheet names cannot exceed 31 chars and cannot contain certain chars.
    """
    name = clean_str(name) or "Sheet"
    name = re.sub(r"[\[\]\:\*\?\/\\]", "-", name)
    return name[:31]


def autosize_excel_columns(writer: pd.ExcelWriter) -> None:
    """
    Light formatting for Excel exports.
    Requires openpyxl engine.
    """
    try:
        for sheet_name in writer.sheets:
            worksheet = writer.sheets[sheet_name]

            for column_cells in worksheet.columns:
                max_length = 0
                column_letter = column_cells[0].column_letter

                for cell in column_cells:
                    value = "" if cell.value is None else str(cell.value)
                    max_length = max(max_length, len(value))

                adjusted_width = min(max(max_length + 2, 10), 60)
                worksheet.column_dimensions[column_letter].width = adjusted_width

            worksheet.freeze_panes = "A2"

    except Exception:
        # Formatting failure should not block export.
        pass


def export_plan_to_excel_bytes(
    project: ProjectMeta,
    master_df: pd.DataFrame,
    unit_issue_df: pd.DataFrame,
    result: Dict[str, Any],
    scenario_df: Optional[pd.DataFrame] = None,
) -> bytes:
    """
    Creates an Excel workbook with practical review tabs.

    Tabs:
      - Summary
      - Input Units
      - Unit Issues
      - Packages
      - Container Loads
      - Packing Decisions
      - Packing Notes
      - Overflow
      - Scenarios
      - Assumptions
    """
    buffer = BytesIO()

    manifest_df = result.get("manifest_df", pd.DataFrame())
    load_summary_df = result.get("load_summary_df", pd.DataFrame())
    decision_df = result.get("decision_df", pd.DataFrame())
    packing_notes_df = result.get("packing_notes_df", pd.DataFrame())
    final_overflow = result.get("final_overflow", [])
    assumptions: LogisticsAssumptions = result["assumptions"]
    vehicle_data = result["vehicle_data"]

    total_weight = 0.0
    if manifest_df is not None and not manifest_df.empty:
        total_weight = float(manifest_df["Weight"].astype(float).sum())

    avg_util = 0.0
    loads = result.get("loads", [])
    if loads:
        avg_util = sum(float(load.get("util", 0)) for load in loads) / len(loads)

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
            {
                "Metric": "Vehicle Dims",
                "Value": dim_text(
                    float(vehicle_data["L"]),
                    float(vehicle_data["W"]),
                    float(vehicle_data["H"]),
                ),
            },
            {
                "Metric": "Vehicle Payload",
                "Value": float(vehicle_data.get("max_lbs", 0) or 0),
            },
            {"Metric": "Total Weight", "Value": total_weight},
            {"Metric": "Containers Used", "Value": len(loads)},
            {"Metric": "Crates", "Value": len(result.get("crates", []))},
            {"Metric": "Pallets", "Value": len(result.get("pallets", []))},
            {"Metric": "Average Floor Utilization %", "Value": round(avg_util, 1)},
            {"Metric": "Unpacked Items", "Value": len(final_overflow)},
            {"Metric": "Planning Warning", "Value": assumptions.planning_warning},
        ]
    )

    assumptions_df = pd.DataFrame(
        [
            {"Assumption": "Glass kg/m²", "Value": assumptions.glass_kg_m2},
            {"Assumption": "Std Weight Multiplier", "Value": assumptions.std_weight_multiplier},
            {"Assumption": "LSD Weight Multiplier", "Value": assumptions.lsd_weight_multiplier},
            {"Assumption": "Frame/Kit Weight %", "Value": assumptions.frame_kit_weight_pct},
            {"Assumption": "Max Crate Lbs", "Value": assumptions.max_crate_lbs},
            {"Assumption": "Max Pallet Lbs", "Value": assumptions.max_pallet_lbs},
            {"Assumption": "Max Crate Length Ext", "Value": assumptions.crate_max_len_ext},
            {"Assumption": "Max Crate Width Ext", "Value": assumptions.crate_max_width_ext},
            {"Assumption": "Vehicle Height Clearance", "Value": assumptions.vehicle_height_clearance},
            {"Assumption": "Container Item Clearance", "Value": assumptions.container_item_clearance},
            {"Assumption": "No Mixing Orders", "Value": assumptions.no_mixing_orders},
            {
                "Assumption": "Allow Pallets for Disassembled",
                "Value": assumptions.allow_pallets_for_disassembled,
            },
        ]
    )

    overflow_df = pd.DataFrame(final_overflow)

    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Summary", index=False)

        if master_df is not None and not master_df.empty:
            master_df.to_excel(writer, sheet_name="Input Units", index=False)

        if unit_issue_df is not None and not unit_issue_df.empty:
            unit_issue_df.to_excel(writer, sheet_name="Unit Issues", index=False)

        if manifest_df is not None and not manifest_df.empty:
            manifest_df.to_excel(writer, sheet_name="Packages", index=False)

        if load_summary_df is not None and not load_summary_df.empty:
            load_summary_df.to_excel(writer, sheet_name="Container Loads", index=False)

        if decision_df is not None and not decision_df.empty:
            decision_df.to_excel(writer, sheet_name="Packing Decisions", index=False)

        if packing_notes_df is not None and not packing_notes_df.empty:
            packing_notes_df.to_excel(writer, sheet_name="Packing Notes", index=False)

        if overflow_df is not None and not overflow_df.empty:
            overflow_df.to_excel(writer, sheet_name="Overflow", index=False)

        if scenario_df is not None and not scenario_df.empty:
            scenario_df.to_excel(writer, sheet_name="Scenarios", index=False)

        assumptions_df.to_excel(writer, sheet_name="Assumptions", index=False)

        autosize_excel_columns(writer)

    buffer.seek(0)
    return buffer.getvalue()

# ==========================================================
# 17. SAVE / LOAD JOB JSON HELPERS
# ==========================================================

def project_from_dict(data: Dict[str, Any]) -> ProjectMeta:
    return ProjectMeta(
        project_name=clean_str(data.get("project_name")),
        customer=clean_str(data.get("customer")),
        project_location=clean_str(data.get("project_location")),
        destination=clean_str(data.get("destination")),
        factory=clean_str(data.get("factory")),
        system=clean_str(data.get("system")),
        estimator=clean_str(data.get("estimator")),
        estimator_email=clean_str(data.get("estimator_email")),
        quote_or_job_ref=clean_str(data.get("quote_or_job_ref")),
        revision=clean_str(data.get("revision")),
        notes=clean_str(data.get("notes")),
    )


def assumptions_from_dict(data: Dict[str, Any]) -> LogisticsAssumptions:
    return LogisticsAssumptions(
        glass_kg_m2=float(data.get("glass_kg_m2", 30.0)),
        std_weight_multiplier=float(data.get("std_weight_multiplier", 1.35)),
        lsd_weight_multiplier=float(data.get("lsd_weight_multiplier", 1.40)),
        frame_kit_weight_pct=float(data.get("frame_kit_weight_pct", 0.20)),
        max_crate_lbs=float(data.get("max_crate_lbs", 2500.0)),
        max_pallet_lbs=float(data.get("max_pallet_lbs", 2200.0)),
        crate_max_len_ext=float(data.get("crate_max_len_ext", DEFAULT_CRATE_MAX_LEN_EXT)),
        crate_max_width_ext=float(data.get("crate_max_width_ext", DEFAULT_CRATE_MAX_WIDTH_EXT)),
        vehicle_height_clearance=float(data.get("vehicle_height_clearance", DEFAULT_HEIGHT_CLEARANCE)),
        container_item_clearance=float(data.get("container_item_clearance", DEFAULT_CONTAINER_ITEM_CLEARANCE)),
        no_mixing_orders=bool(data.get("no_mixing_orders", True)),
        allow_pallets_for_disassembled=bool(data.get("allow_pallets_for_disassembled", True)),
        planning_warning=clean_str(
            data.get(
                "planning_warning",
                LogisticsAssumptions().planning_warning,
            )
        ),
    )


def build_job_save_payload(
    project: ProjectMeta,
    assumptions: LogisticsAssumptions,
    orders: Dict[str, str],
    master_df: Optional[pd.DataFrame],
    selected_vehicle: str,
    selected_pallets: List[str],
) -> Dict[str, Any]:
    """
    Creates a JSON-safe payload for saving/reloading a job.
    """
    if master_df is not None and not master_df.empty:
        master_records = master_df.to_dict(orient="records")
    else:
        master_records = []

    return {
        "app": "UltraLogistics Pro",
        "version": "2026-07-02",
        "saved_at": now_stamp(),
        "project": asdict(project),
        "assumptions": asdict(assumptions),
        "orders": orders,
        "master_rows": master_records,
        "selected_vehicle": selected_vehicle,
        "selected_pallets": selected_pallets,
    }


def load_job_payload(
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Converts saved job JSON back into typed app objects.
    """
    project = project_from_dict(payload.get("project", {}))
    assumptions = assumptions_from_dict(payload.get("assumptions", {}))

    orders = payload.get("orders", {})
    if not isinstance(orders, dict):
        orders = {}

    master_rows = payload.get("master_rows", [])
    master_df = pd.DataFrame(master_rows)

    if not master_df.empty:
        for col in MASTER_COLUMNS:
            if col not in master_df.columns:
                master_df[col] = ""

        master_df = master_df[MASTER_COLUMNS]

    selected_vehicle = clean_str(payload.get("selected_vehicle"))
    selected_pallets = payload.get("selected_pallets", [])

    if not isinstance(selected_pallets, list):
        selected_pallets = []

    return {
        "project": project,
        "assumptions": assumptions,
        "orders": orders,
        "master_df": master_df,
        "selected_vehicle": selected_vehicle,
        "selected_pallets": selected_pallets,
    }


def read_uploaded_json(uploaded_file) -> Dict[str, Any]:
    raw = uploaded_file.read()

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")

    return json.loads(raw)

# ==========================================================
# 18. CUSTOM VEHICLE / PALLET CONFIG HELPERS
# ==========================================================

def sample_config_json() -> Dict[str, Any]:
    """
    Example config users can copy, edit, and upload.
    """
    return {
        "vehicles": {
            "Custom Trailer Example": {
                "L": 600,
                "W": 96,
                "H": 106,
                "max_lbs": 44000,
            }
        },
        "pallets": {
            "Factory Rack Example": {
                "L": 120,
                "W": 48,
                "H": 8,
                "max_lbs": 3000,
            }
        },
    }


def merge_custom_config(
    uploaded_config: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, float]], Dict[str, Dict[str, float]], List[str]]:
    """
    Merges optional uploaded JSON config with default vehicles/pallets.

    Expected JSON format:
        {
          "vehicles": {
            "Name": {"L": 473, "W": 92, "H": 105, "max_lbs": 44000}
          },
          "pallets": {
            "Name": {"L": 48, "W": 40, "H": 6, "max_lbs": 2200}
          }
        }
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
            vehicles[clean_str(name)] = {
                "L": float(spec["L"]),
                "W": float(spec["W"]),
                "H": float(spec["H"]),
                "max_lbs": float(spec.get("max_lbs", 0) or 0),
            }
        except Exception:
            warnings.append(f"Skipped invalid vehicle config: {name}")

    for name, spec in pallet_rows.items():
        try:
            pallets[clean_str(name)] = {
                "L": float(spec["L"]),
                "W": float(spec["W"]),
                "H": float(spec.get("H", PALLET_H)),
                "max_lbs": float(spec.get("max_lbs", 2200) or 2200),
            }
        except Exception:
            warnings.append(f"Skipped invalid pallet config: {name}")

    return vehicles, pallets, warnings

# ==========================================================
# 19. STREAMLIT SESSION STATE
# ==========================================================

def init_session_state() -> None:
    """
    Initializes all app-level session state keys.
    """
    defaults = {
        "project": ProjectMeta(),
        "assumptions": LogisticsAssumptions(),
        "orders": {},
        "df_master": None,
        "results": None,
        "unit_issue_df": pd.DataFrame(),
        "scenario_df": pd.DataFrame(),
        "uploaded_source_df": None,
        "uploaded_source_name": "",
        "uploaded_mapping": {},
        "vehicle_catalog": DEFAULT_CONTAINERS,
        "pallet_catalog": DEFAULT_PALLETS,
        "selected_vehicle": "40' HC Container",
        "selected_pallets": ["US GMA (48x40)"],
        "last_validation_df": pd.DataFrame(),
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def clear_results() -> None:
    st.session_state.results = None
    st.session_state.scenario_df = pd.DataFrame()


def set_project_from_widgets(project: ProjectMeta) -> None:
    st.session_state.project = project


def set_assumptions_from_widgets(assumptions: LogisticsAssumptions) -> None:
    st.session_state.assumptions = assumptions


def apply_loaded_job_to_session(loaded: Dict[str, Any]) -> None:
    """
    Applies loaded JSON job data into Streamlit session state.
    """
    st.session_state.project = loaded["project"]
    st.session_state.assumptions = loaded["assumptions"]
    st.session_state.orders = loaded["orders"]
    st.session_state.df_master = loaded["master_df"]

    if loaded["selected_vehicle"]:
        st.session_state.selected_vehicle = loaded["selected_vehicle"]

    if loaded["selected_pallets"]:
        st.session_state.selected_pallets = loaded["selected_pallets"]

    clear_results()

# ==========================================================
# 20. MAIN APP SETUP
# ==========================================================

st.set_page_config(
    page_title="UltraLogistics Pro",
    layout="wide",
)

init_session_state()


# ==========================================================
# LOGIN / AUTHENTICATION
# ==========================================================

def check_password() -> bool:
    """
    Simple username/password gate using Streamlit secrets.

    Expected secrets format:

    [passwords]
    admin = "your_password"
    estimating = "another_password"
    """
    if st.session_state.get("authenticated", False):
        return True

    st.title("🔐 UltraLogistics Pro Login")

    try:
        valid_passwords = dict(st.secrets.get("passwords", {}))
    except Exception:
        valid_passwords = {}

    if not valid_passwords:
        st.error(
            "No passwords are configured. Add a [passwords] section in Streamlit secrets."
        )
        st.stop()

    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")

    if submitted:
        username = clean_str(username)

        if username in valid_passwords and hmac.compare_digest(
            password,
            str(valid_passwords[username]),
        ):
            st.session_state.authenticated = True
            st.session_state.username = username
            st.rerun()

        else:
            st.error("Invalid username or password.")

    st.stop()


check_password()

st.title("🚚 UltraLogistics Pro")

# ==========================================================
# 21. SIDEBAR: CONFIG, PROJECT, ASSUMPTIONS
# ==========================================================

with st.sidebar:
    st.title("⚙️ Setup")

    st.caption(f"Logged in as: {st.session_state.get('username', '')}")

    if st.button("Logout", use_container_width=True):
        st.session_state.authenticated = False
        st.session_state.username = ""
        st.rerun()

    st.markdown("---")

    # ------------------------------------------------------
    # Custom vehicle / pallet config
    # ------------------------------------------------------
    with st.expander("Custom Vehicles / Pallets", expanded=False):
        st.caption("Optional JSON config for custom vehicles and pallet/rack sizes.")

        st.download_button(
            "Download Sample Config JSON",
            data=json_download_bytes(sample_config_json()),
            file_name="ultralogistics_config_sample.json",
            mime="application/json",
            use_container_width=True,
        )

        config_upload = st.file_uploader(
            "Upload Config JSON",
            type=["json"],
            key="config_upload",
        )

        uploaded_config = None

        if config_upload is not None:
            try:
                uploaded_config = read_uploaded_json(config_upload)
                st.success("Config JSON loaded.")
            except Exception as e:
                st.error(f"Could not read config JSON: {e}")

        vehicle_catalog, pallet_catalog, config_warnings = merge_custom_config(
            uploaded_config
        )

        for warning in config_warnings:
            st.warning(warning)

        st.session_state.vehicle_catalog = vehicle_catalog
        st.session_state.pallet_catalog = pallet_catalog

    # ------------------------------------------------------
    # Load saved job
    # ------------------------------------------------------
    with st.expander("Load Saved Job", expanded=False):
        job_upload = st.file_uploader(
            "Upload saved job JSON",
            type=["json"],
            key="job_upload",
        )

        if st.button("Load Job JSON", use_container_width=True):
            if job_upload is None:
                st.warning("Upload a job JSON first.")
            else:
                try:
                    payload = read_uploaded_json(job_upload)
                    loaded = load_job_payload(payload)
                    apply_loaded_job_to_session(loaded)
                    st.success("Job loaded.")
                except Exception as e:
                    st.error(f"Could not load job JSON: {e}")

    # ------------------------------------------------------
    # Project metadata
    # ------------------------------------------------------
    with st.expander("Project Metadata", expanded=True):
        p0: ProjectMeta = st.session_state.project

        project = ProjectMeta(
            project_name=st.text_input("Project Name", value=p0.project_name),
            customer=st.text_input("Customer", value=p0.customer),
            project_location=st.text_input("Project Location", value=p0.project_location),
            destination=st.text_input("Destination", value=p0.destination),
            factory=st.text_input("Factory", value=p0.factory),
            system=st.text_input("System", value=p0.system),
            estimator=st.text_input("Estimator", value=p0.estimator),
            estimator_email=st.text_input("Estimator Email", value=p0.estimator_email),
            quote_or_job_ref=st.text_input("Quote / Job Ref", value=p0.quote_or_job_ref),
            revision=st.text_input("Revision", value=p0.revision),
            notes=st.text_area("Project Notes", value=p0.notes, height=80),
        )

        set_project_from_widgets(project)

    # ------------------------------------------------------
    # Weight and packing assumptions
    # ------------------------------------------------------
    with st.expander("Weight / Packing Assumptions", expanded=True):
        a0: LogisticsAssumptions = st.session_state.assumptions

        assumptions = LogisticsAssumptions(
            glass_kg_m2=st.number_input(
                "Glass kg/m²",
                min_value=10.0,
                max_value=80.0,
                value=float(a0.glass_kg_m2),
                step=1.0,
            ),
            std_weight_multiplier=st.number_input(
                "Total Weight Multiplier - Standard",
                min_value=1.0,
                max_value=3.0,
                value=float(a0.std_weight_multiplier),
                step=0.05,
            ),
            lsd_weight_multiplier=st.number_input(
                "Total Weight Multiplier - LSD / Sliding",
                min_value=1.0,
                max_value=3.0,
                value=float(a0.lsd_weight_multiplier),
                step=0.05,
            ),
            frame_kit_weight_pct=st.slider(
                "Frame / Kit Weight %",
                min_value=0.0,
                max_value=0.5,
                value=float(a0.frame_kit_weight_pct),
                step=0.01,
            ),
            max_crate_lbs=st.number_input(
                "Max Crate Lbs",
                min_value=500.0,
                max_value=12000.0,
                value=float(a0.max_crate_lbs),
                step=100.0,
            ),
            max_pallet_lbs=st.number_input(
                "Max Pallet Lbs",
                min_value=500.0,
                max_value=10000.0,
                value=float(a0.max_pallet_lbs),
                step=100.0,
            ),
            crate_max_len_ext=st.number_input(
                "Max Crate Length Ext",
                min_value=48.0,
                max_value=800.0,
                value=float(a0.crate_max_len_ext),
                step=6.0,
            ),
            crate_max_width_ext=st.number_input(
                "Max Crate Width / Depth Ext",
                min_value=12.0,
                max_value=120.0,
                value=float(a0.crate_max_width_ext),
                step=1.0,
            ),
            vehicle_height_clearance=st.number_input(
                "Vehicle Height Clearance",
                min_value=0.0,
                max_value=24.0,
                value=float(a0.vehicle_height_clearance),
                step=0.5,
            ),
            container_item_clearance=st.number_input(
                "Floor Item Clearance",
                min_value=0.0,
                max_value=12.0,
                value=float(a0.container_item_clearance),
                step=0.5,
            ),
            no_mixing_orders=st.checkbox(
                "Do not mix orders inside crates/pallets",
                value=bool(a0.no_mixing_orders),
            ),
            allow_pallets_for_disassembled=st.checkbox(
                "Use pallets for disassembled pieces",
                value=bool(a0.allow_pallets_for_disassembled),
            ),
            planning_warning=a0.planning_warning,
        )

        set_assumptions_from_widgets(assumptions)

    # ------------------------------------------------------
    # Vehicle and pallet selection
    # ------------------------------------------------------
    st.markdown("---")
    st.subheader("Vehicle / Pallets")

    vehicle_names = list(st.session_state.vehicle_catalog.keys())

    if st.session_state.selected_vehicle not in vehicle_names:
        st.session_state.selected_vehicle = vehicle_names[0]

    selected_vehicle = st.selectbox(
        "Vehicle",
        vehicle_names,
        index=vehicle_names.index(st.session_state.selected_vehicle),
    )

    st.session_state.selected_vehicle = selected_vehicle

    pallet_names = list(st.session_state.pallet_catalog.keys())

    valid_default_pallets = [
        p
        for p in st.session_state.selected_pallets
        if p in pallet_names
    ]

    if not valid_default_pallets and pallet_names:
        valid_default_pallets = [pallet_names[0]]

    selected_pallets = st.multiselect(
        "Pallets / Racks Allowed",
        pallet_names,
        default=valid_default_pallets,
    )

    st.session_state.selected_pallets = selected_pallets

    vehicle_data = st.session_state.vehicle_catalog[selected_vehicle]

    st.caption(
        f'Vehicle dims: {vehicle_data["L"]:.0f}"L x '
        f'{vehicle_data["W"]:.0f}"W x {vehicle_data["H"]:.0f}"H'
    )

    st.caption(
        f'Usable internal crate height: '
        f'{get_vehicle_internal_crate_height_limit(vehicle_data, assumptions.vehicle_height_clearance):.1f}"'
    )

    # ------------------------------------------------------
    # Save job
    # ------------------------------------------------------
    st.markdown("---")
    st.subheader("Save Job")

    save_payload = build_job_save_payload(
        project=st.session_state.project,
        assumptions=st.session_state.assumptions,
        orders=st.session_state.orders,
        master_df=st.session_state.df_master,
        selected_vehicle=st.session_state.selected_vehicle,
        selected_pallets=st.session_state.selected_pallets,
    )

    save_name = (
        f"{today_file_stamp()}-"
        f"{slugify(st.session_state.project.display_name())}-"
        f"logistics-plan.json"
    )

    st.download_button(
        "Download Job JSON",
        data=json_download_bytes(save_payload),
        file_name=save_name,
        mime="application/json",
        use_container_width=True,
    )

# ==========================================================
# 22. MAIN TABS
# ==========================================================

tab_input, tab_edit, tab_optimize, tab_results, tab_scenarios = st.tabs(
    [
        "1️⃣ Data Input",
        "2️⃣ Edit & Validate",
        "3️⃣ Optimize",
        "4️⃣ Results & Export",
        "5️⃣ Scenarios",
    ]
)

# ==========================================================
# 23. TAB 1 — DATA INPUT
# ==========================================================

with tab_input:
    st.header("Data Input")

    st.info(
        "You can paste order rows manually or upload an Excel/CSV file. "
        "Expected manual paste format: ID, W, H, Type, Qty"
    )

    c_left, c_right = st.columns([1, 1])

    # ------------------------------------------------------
    # Manual paste orders
    # ------------------------------------------------------
    with c_left:
        st.subheader("Manual Order Paste")

        order_name = st.text_input(
            "Order Name",
            value="Order 1",
            key="manual_order_name",
        )

        raw_in = st.text_area(
            "Paste rows: ID, W, H, Type, Qty",
            height=180,
            key="manual_raw_input",
            placeholder="A1, 36, 72, FIXED, 2\nD1, 96, 108, LSD, 1",
        )

        c_add, c_clear = st.columns(2)

        with c_add:
            if st.button("➕ Add / Update Order", use_container_width=True):
                name = clean_str(order_name) or "Order 1"
                st.session_state.orders[name] = raw_in
                clear_results()
                st.success(f"Saved order: {name}")

        with c_clear:
            st.button(
                "🧹 Clear Paste Box",
                use_container_width=True,
                on_click=lambda: st.session_state.update({"manual_raw_input": ""}),
            )

        if st.session_state.orders:
            st.markdown("#### Saved Orders")

            for saved_name in list(st.session_state.orders.keys()):
                row_cols = st.columns([4, 1])

                with row_cols[0]:
                    st.caption(
                        f"**{saved_name}** — "
                        f"{len(st.session_state.orders[saved_name].splitlines())} pasted line(s)"
                    )

                with row_cols[1]:
                    if st.button(
                        "Delete",
                        key=f"delete_order_{saved_name}",
                        use_container_width=True,
                    ):
                        st.session_state.orders.pop(saved_name, None)
                        clear_results()
                        st.rerun()

        process_scope = "ALL ORDERS"

        if st.session_state.orders:
            process_scope = st.selectbox(
                "Process Scope",
                ["ALL ORDERS"] + list(st.session_state.orders.keys()),
            )

        if st.button("Process Manual Orders", type="primary", use_container_width=True):
            all_rows: List[Dict[str, Any]] = []
            all_issues: List[ValidationIssue] = []

            if not st.session_state.orders:
                st.error("No orders saved yet.")
            else:
                if process_scope == "ALL ORDERS":
                    items_to_process = st.session_state.orders.items()
                else:
                    items_to_process = [
                        (
                            process_scope,
                            st.session_state.orders.get(process_scope, ""),
                        )
                    ]

                for name, text in items_to_process:
                    rows, issues = parse_order_text_to_rows(
                        order_name=name,
                        raw_text=text,
                        assumptions=st.session_state.assumptions,
                    )
                    all_rows.extend(rows)
                    all_issues.extend(issues)

                st.session_state.df_master = (
                    pd.DataFrame(all_rows, columns=MASTER_COLUMNS)
                    if all_rows
                    else pd.DataFrame(columns=MASTER_COLUMNS)
                )

                st.session_state.last_validation_df = validation_issues_to_df(all_issues)
                clear_results()

                if all_rows:
                    st.success(f"Loaded {len(all_rows)} unit row(s).")
                else:
                    st.error("No valid rows loaded.")

                if all_issues:
                    st.warning(f"{len(all_issues)} issue(s) found while parsing.")
                    st.dataframe(
                        validation_issues_to_df(all_issues),
                        use_container_width=True,
                        hide_index=True,
                    )
    # ------------------------------------------------------
    # Excel / CSV upload
    # ------------------------------------------------------
    with c_right:
        st.subheader("Excel / CSV Upload")

        upload_order_name = st.text_input(
            "Order Name for Upload",
            value="Uploaded Order",
            key="upload_order_name",
        )

        uploaded_file = st.file_uploader(
            "Upload CSV or Excel",
            type=["csv", "xlsx", "xlsm", "xls"],
            key="source_file_upload",
        )

        if uploaded_file is not None:
            try:
                uploaded_df = read_uploaded_dataframe(uploaded_file)
                st.session_state.uploaded_source_df = uploaded_df
                st.session_state.uploaded_source_name = uploaded_file.name

                st.success(
                    f"Loaded source file: {uploaded_file.name} "
                    f"({len(uploaded_df)} row(s))"
                )

                st.dataframe(
                    uploaded_df.head(20),
                    use_container_width=True,
                )

                columns = [str(col) for col in uploaded_df.columns]
                options = ["<none>"] + columns
                default_mapping = build_default_column_mapping(columns)

                st.markdown("#### Column Mapping")

                mapping: Dict[str, str] = {}

                def mapping_select(
                    label: str,
                    logical_name: str,
                    required: bool = False,
                ) -> str:
                    default_col = default_mapping.get(logical_name, "<none>")
                    default_index = (
                        options.index(default_col)
                        if default_col in options
                        else 0
                    )

                    label_text = f"{label} {'*' if required else ''}"

                    return st.selectbox(
                        label_text,
                        options,
                        index=default_index,
                        key=f"mapping_{logical_name}",
                    )

                m1, m2 = st.columns(2)

                with m1:
                    mapping["id"] = mapping_select("ID / Mark", "id", True)
                    mapping["width"] = mapping_select("Width", "width", True)
                    mapping["height"] = mapping_select("Height", "height", True)
                    mapping["type"] = mapping_select("Type / System", "type", False)

                with m2:
                    mapping["qty"] = mapping_select("Quantity", "qty", False)
                    mapping["depth"] = mapping_select("Depth", "depth", False)
                    mapping["weight"] = mapping_select("Weight", "weight", False)
                    mapping["notes"] = mapping_select("Notes", "notes", False)

                import_mode = st.radio(
                    "Import Mode",
                    ["Replace current master table", "Append to current master table"],
                    horizontal=True,
                )

                if st.button(
                    "Import Uploaded File",
                    type="primary",
                    use_container_width=True,
                ):
                    rows, issues = normalize_uploaded_table(
                        df=uploaded_df,
                        order_name=clean_str(upload_order_name) or "Uploaded Order",
                        mapping=mapping,
                        assumptions=st.session_state.assumptions,
                        source_name=uploaded_file.name,
                    )

                    new_df = (
                        pd.DataFrame(rows, columns=MASTER_COLUMNS)
                        if rows
                        else pd.DataFrame(columns=MASTER_COLUMNS)
                    )

                    if (
                        import_mode == "Append to current master table"
                        and st.session_state.df_master is not None
                        and not st.session_state.df_master.empty
                    ):
                        st.session_state.df_master = pd.concat(
                            [st.session_state.df_master, new_df],
                            ignore_index=True,
                        )
                    else:
                        st.session_state.df_master = new_df

                    st.session_state.last_validation_df = validation_issues_to_df(issues)
                    clear_results()

                    if rows:
                        st.success(f"Imported {len(rows)} unit row(s).")
                    else:
                        st.error("No valid rows imported.")

                    if issues:
                        st.warning(f"{len(issues)} issue(s) found while importing.")
                        st.dataframe(
                            validation_issues_to_df(issues),
                            use_container_width=True,
                            hide_index=True,
                        )

            except Exception as e:
                st.error(f"Could not read uploaded file: {e}")

    # ------------------------------------------------------
    # Current master table preview
    # ------------------------------------------------------
    st.divider()
    st.subheader("Current Master Table Preview")

    if st.session_state.df_master is not None and not st.session_state.df_master.empty:
        st.dataframe(
            st.session_state.df_master,
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No master table loaded yet.")

# ==========================================================
# 24. TAB 2 — EDIT & VALIDATE
# ==========================================================

with tab_edit:
    st.header("Edit & Validate")

    if st.session_state.df_master is None or st.session_state.df_master.empty:
        st.info("Load data first in the Data Input tab.")

    else:
        st.caption(
            "Edit Mode, Orientation, Split Rows, Split Cols, Depth, and Weight as needed. "
            "Depth and weight are now used directly in crate and pallet planning."
        )

        current_master = st.session_state.df_master.copy()

        # Make sure all expected columns exist.
        for col in MASTER_COLUMNS:
            if col not in current_master.columns:
                current_master[col] = ""

        current_master = current_master[MASTER_COLUMNS]

        edited_df = st.data_editor(
            current_master,
            key="master_data_editor",
            use_container_width=True,
            hide_index=True,
            num_rows="dynamic",
            column_config={
                "Mode": st.column_config.SelectboxColumn(
                    "Mode",
                    options=["WHOLE", "SLANT", "DISASSEMBLED"],
                    required=True,
                ),
                "Orient": st.column_config.SelectboxColumn(
                    "Orientation",
                    options=["AUTO", "UPRIGHT", "SIDE"],
                    required=True,
                ),
                "SR": st.column_config.NumberColumn(
                    "Split Rows",
                    min_value=1,
                    step=1,
                ),
                "SC": st.column_config.NumberColumn(
                    "Split Cols",
                    min_value=1,
                    step=1,
                ),
                "W": st.column_config.NumberColumn(
                    "Width",
                    min_value=0.01,
                    step=0.125,
                    format="%.3f",
                ),
                "H": st.column_config.NumberColumn(
                    "Height",
                    min_value=0.01,
                    step=0.125,
                    format="%.3f",
                ),
                "Depth": st.column_config.NumberColumn(
                    "Depth",
                    min_value=0.01,
                    step=0.01,
                    format="%.3f",
                ),
                "Lbs": st.column_config.NumberColumn(
                    "Weight Lbs",
                    min_value=0.01,
                    step=1.0,
                    format="%.1f",
                ),
                "Qty": st.column_config.NumberColumn(
                    "Qty",
                    disabled=True,
                ),
            },
            disabled=[
                "ID",
                "Orig",
                "Source",
            ],
        )

        c_save, c_recalc, c_clear = st.columns([1, 1, 1])

        with c_save:
            if st.button(
                "💾 Save Edited Master Table",
                type="primary",
                use_container_width=True,
            ):
                st.session_state.df_master = edited_df.copy()
                clear_results()
                st.success("Edited master table saved.")

        with c_recalc:
            if st.button(
                "🔎 Validate Current Table",
                use_container_width=True,
            ):
                st.session_state.df_master = edited_df.copy()

                validation_issues = validate_master_dataframe(
                    st.session_state.df_master
                )

                st.session_state.last_validation_df = validation_issues_to_df(
                    validation_issues
                )

                selected_vehicle_name = st.session_state.selected_vehicle
                vehicle_data_now = st.session_state.vehicle_catalog[selected_vehicle_name]

                st.session_state.unit_issue_df = build_unit_issue_report(
                    st.session_state.df_master,
                    vehicle_data_now,
                    st.session_state.assumptions,
                )

                clear_results()
                st.success("Validation complete.")

        with c_clear:
            if st.button(
                "🧹 Clear Master Table",
                use_container_width=True,
            ):
                st.session_state.df_master = pd.DataFrame(columns=MASTER_COLUMNS)
                st.session_state.last_validation_df = pd.DataFrame()
                st.session_state.unit_issue_df = pd.DataFrame()
                clear_results()
                st.rerun()

        st.divider()

        st.subheader("Validation Issues")

        if (
            st.session_state.last_validation_df is not None
            and not st.session_state.last_validation_df.empty
        ):
            st.dataframe(
                st.session_state.last_validation_df,
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.success("No parsing/master-table validation issues currently stored.")

        st.divider()

        st.subheader("Practical Fit / Oversized Unit Report")

        selected_vehicle_name = st.session_state.selected_vehicle
        vehicle_data_now = st.session_state.vehicle_catalog[selected_vehicle_name]

        issue_df_preview = build_unit_issue_report(
            edited_df,
            vehicle_data_now,
            st.session_state.assumptions,
        )

        st.session_state.unit_issue_df = issue_df_preview

        show_issue_summary(issue_df_preview)

        st.caption(
            "This report is based on the currently selected vehicle and height clearance. "
            "Changing the vehicle can change which units are considered problematic."
        )
# ==========================================================
# 25. TAB 3 — OPTIMIZE
# ==========================================================

with tab_optimize:
    st.header("Optimize Load Plan")

    if st.session_state.df_master is None or st.session_state.df_master.empty:
        st.info("Load and validate data first.")

    else:
        selected_vehicle_name = st.session_state.selected_vehicle
        vehicle_data_now = st.session_state.vehicle_catalog[selected_vehicle_name]
        assumptions_now = st.session_state.assumptions

        max_h_int_now = get_vehicle_internal_crate_height_limit(
            vehicle_data_now,
            assumptions_now.vehicle_height_clearance,
        )

        max_h_ext_now = get_vehicle_external_crate_height_limit(
            vehicle_data_now,
            assumptions_now.vehicle_height_clearance,
        )

        c1, c2, c3, c4 = st.columns(4)

        c1.metric("Vehicle", selected_vehicle_name)
        c2.metric("Vehicle Height", inches_text(float(vehicle_data_now["H"])))
        c3.metric("Usable Crate Internal H", inches_text(max_h_int_now))
        c4.metric("Max Crate External H", inches_text(max_h_ext_now))

        st.warning(assumptions_now.planning_warning)

        st.subheader("Pre-Optimization Checks")

        validation_issues = validate_master_dataframe(st.session_state.df_master)
        validation_df = validation_issues_to_df(validation_issues)

        unit_issue_df = build_unit_issue_report(
            st.session_state.df_master,
            vehicle_data_now,
            assumptions_now,
        )

        if not validation_df.empty:
            st.markdown("#### Master Table Validation")
            st.dataframe(
                validation_df,
                use_container_width=True,
                hide_index=True,
            )

        st.markdown("#### Practical Fit Issues")
        show_issue_summary(unit_issue_df)

        blocking_errors = [
            issue
            for issue in validation_issues
            if issue.severity == "ERROR"
        ]

        if blocking_errors:
            st.error("Fix validation errors before optimizing.")

        st.divider()

        c_opt, c_reset = st.columns([2, 1])

        with c_opt:
            optimize_clicked = st.button(
                "🚀 Optimize Load",
                type="primary",
                use_container_width=True,
                disabled=bool(blocking_errors),
            )

        with c_reset:
            if st.button("Clear Results", use_container_width=True):
                clear_results()
                st.success("Results cleared.")

        if optimize_clicked:
            plan = build_logistics_plan(
                master_df=st.session_state.df_master,
                vehicle_name=selected_vehicle_name,
                vehicle_data=vehicle_data_now,
                selected_pallet_names=st.session_state.selected_pallets,
                pallet_catalog=st.session_state.pallet_catalog,
                assumptions=assumptions_now,
            )

            if not plan.get("ok"):
                st.session_state.results = None
                st.error(plan.get("message", "Optimization failed."))

                if "errors" in plan:
                    st.dataframe(
                        plan["errors"],
                        use_container_width=True,
                        hide_index=True,
                    )

            else:
                st.session_state.results = plan
                st.session_state.unit_issue_df = unit_issue_df
                st.success("Optimization complete.")

        st.divider()

        if st.session_state.results:
            result = st.session_state.results

            manifest_df = result["manifest_df"]
            load_summary_df = result["load_summary_df"]
            final_overflow = result["final_overflow"]

            total_weight = 0.0
            if manifest_df is not None and not manifest_df.empty:
                total_weight = float(manifest_df["Weight"].astype(float).sum())

            loads = result["loads"]
            avg_util = 0.0
            if loads:
                avg_util = sum(float(load["util"]) for load in loads) / len(loads)

            m1, m2, m3, m4, m5 = st.columns(5)

            m1.metric("Total Weight", pounds_text(total_weight))
            m2.metric("Containers Used", len(loads))
            m3.metric("Crates", len(result["crates"]))
            m4.metric("Pallets", len(result["pallets"]))
            m5.metric("Avg Floor Util", f"{avg_util:.1f}%")

            if final_overflow:
                st.error(
                    f"{len(final_overflow)} package(s) could not be packed into the selected vehicle."
                )
                st.dataframe(
                    pd.DataFrame(final_overflow),
                    use_container_width=True,
                    hide_index=True,
                )

            overloaded_loads = [
                load
                for load in loads
                if load.get("payload_over")
            ]

            if overloaded_loads:
                st.warning(
                    f"{len(overloaded_loads)} container(s) exceed configured payload limit."
                )

            bad_packages = pd.DataFrame()

            if manifest_df is not None and not manifest_df.empty:
                bad_packages = manifest_df[
                    manifest_df["Status"].astype(str) != "OK"
                ]

            if not bad_packages.empty:
                st.warning(
                    f"{len(bad_packages)} package(s) have crate/pallet status warnings."
                )
                st.dataframe(
                    bad_packages,
                    use_container_width=True,
                    hide_index=True,
                )

            st.subheader("Container Load Summary")
            st.dataframe(
                load_summary_df,
                use_container_width=True,
                hide_index=True,
            )

# ==========================================================
# 26. TAB 4 — RESULTS & EXPORT
# ==========================================================

with tab_results:
    st.header("Results & Export")

    if not st.session_state.results:
        st.info("Run optimization first.")

    else:
        result = st.session_state.results

        manifest_df = result["manifest_df"]
        load_summary_df = result["load_summary_df"]
        decision_df = result["decision_df"]
        packing_notes_df = result["packing_notes_df"]
        loads = result["loads"]
        order_color_map = result["order_color_map"]
        vehicle_data_now = result["vehicle_data"]
        vehicle_name_now = result["vehicle_name"]

        total_weight = 0.0
        if manifest_df is not None and not manifest_df.empty:
            total_weight = float(manifest_df["Weight"].astype(float).sum())

        avg_util = 0.0
        if loads:
            avg_util = sum(float(load["util"]) for load in loads) / len(loads)

        c1, c2, c3, c4, c5 = st.columns(5)

        c1.metric("Total Weight", pounds_text(total_weight))
        c2.metric("Containers Used", len(loads))
        c3.metric("Crates", len(result["crates"]))
        c4.metric("Pallets", len(result["pallets"]))
        c5.metric("Avg Floor Util", f"{avg_util:.1f}%")

        st.warning(result["assumptions"].planning_warning)

        st.divider()

        st.subheader("Package Manifest")
        st.dataframe(
            manifest_df,
            use_container_width=True,
            hide_index=True,
        )

        st.subheader("Container Load Summary")
        st.dataframe(
            load_summary_df,
            use_container_width=True,
            hide_index=True,
        )

        st.subheader("Packing Decisions")
        st.dataframe(
            decision_df,
            use_container_width=True,
            hide_index=True,
        )

        if packing_notes_df is not None and not packing_notes_df.empty:
            st.subheader("Packing Notes")
            st.dataframe(
                packing_notes_df,
                use_container_width=True,
                hide_index=True,
            )

        st.divider()

        st.subheader("Container Plans")

        if loads:
            selected_container_no = st.selectbox(
                "View Container",
                [load["container_no"] for load in loads],
                key="results_container_select",
            )

            selected_load = next(
                load
                for load in loads
                if load["container_no"] == selected_container_no
            )

            st.caption(
                f"{vehicle_name_now} — Container #{selected_container_no} — "
                f"Utilization: {selected_load['util']:.1f}% — "
                f"Weight: {pounds_text(float(selected_load.get('weight', 0)))}"
            )

            if selected_load.get("payload_over"):
                st.warning("This container exceeds the configured payload limit.")

            container_L = float(vehicle_data_now["L"])
            container_W = float(vehicle_data_now["W"])

            fig2d = build_2d_plan(
                selected_load["placed"],
                container_L,
                container_W,
                order_color_map,
                title_text=f"Container #{selected_container_no} 2D Plan",
            )

            st.plotly_chart(fig2d, use_container_width=True)

            fig3d = build_3d_plan(
                selected_load,
                result,
                vehicle_data_now,
                order_color_map,
            )

            st.plotly_chart(fig3d, use_container_width=True)

        final_overflow = result.get("final_overflow", [])

        if final_overflow:
            st.subheader("Unpacked / Overflow Items")
            st.error(
                f"{len(final_overflow)} package(s) could not be packed into any selected vehicle load."
            )
            st.dataframe(
                pd.DataFrame(final_overflow),
                use_container_width=True,
                hide_index=True,
            )

        st.divider()

        st.subheader("Exports")

        export_base_name = (
            f"{today_file_stamp()}-"
            f"{slugify(st.session_state.project.display_name())}-"
            f"logistics-manifest"
        )

        col_pdf, col_xlsx, col_json = st.columns(3)

        with col_pdf:
            try:
                pdf_bytes = generate_pdf_report(
                    st.session_state.project,
                    result,
                )

                st.download_button(
                    "Download PDF Manifest",
                    data=pdf_bytes,
                    file_name=f"{export_base_name}.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                )

            except Exception as e:
                st.error(f"Could not generate PDF: {e}")

        with col_xlsx:
            try:
                excel_bytes = export_plan_to_excel_bytes(
                    project=st.session_state.project,
                    master_df=st.session_state.df_master,
                    unit_issue_df=st.session_state.unit_issue_df,
                    result=result,
                    scenario_df=st.session_state.scenario_df,
                )

                st.download_button(
                    "Download Excel Workbook",
                    data=excel_bytes,
                    file_name=f"{export_base_name}.xlsx",
                    mime=(
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet"
                    ),
                    use_container_width=True,
                )

            except Exception as e:
                st.error(f"Could not generate Excel workbook: {e}")

        with col_json:
            save_payload = build_job_save_payload(
                project=st.session_state.project,
                assumptions=st.session_state.assumptions,
                orders=st.session_state.orders,
                master_df=st.session_state.df_master,
                selected_vehicle=st.session_state.selected_vehicle,
                selected_pallets=st.session_state.selected_pallets,
            )

            st.download_button(
                "Download Job JSON",
                data=json_download_bytes(save_payload),
                file_name=f"{export_base_name}.json",
                mime="application/json",
                use_container_width=True,
            )

# ==========================================================
# 27. TAB 5 — SCENARIOS
# ==========================================================

with tab_scenarios:
    st.header("Scenario Comparison")

    if st.session_state.df_master is None or st.session_state.df_master.empty:
        st.info("Load unit data first.")

    else:
        st.caption(
            "Compare different vehicles and handling strategies before committing "
            "to the final optimized plan."
        )

        vehicle_options = list(st.session_state.vehicle_catalog.keys())

        default_vehicle_compare = [
            st.session_state.selected_vehicle
        ]

        selected_vehicle_names = st.multiselect(
            "Vehicles to Compare",
            vehicle_options,
            default=default_vehicle_compare,
        )

        strategy_options = [
            "Current Settings",
            "Auto Slant Tall Units",
            "Disassemble Oversized Units",
            "Split Oversized Units 2x1",
        ]

        selected_strategy_names = st.multiselect(
            "Strategies to Compare",
            strategy_options,
            default=[
                "Current Settings",
                "Auto Slant Tall Units",
                "Disassemble Oversized Units",
            ],
        )

        st.caption(
            "Scenario comparison is a planning aid. It does not replace final review "
            "of handling rules, crate construction, carrier requirements, or shop/factory packing constraints."
        )

        run_scenarios_clicked = st.button(
            "Run Scenario Comparison",
            type="primary",
            use_container_width=True,
        )

        if run_scenarios_clicked:
            if not selected_vehicle_names:
                st.error("Select at least one vehicle.")

            elif not selected_strategy_names:
                st.error("Select at least one strategy.")

            else:
                scenario_df = run_scenario_comparison(
                    master_df=st.session_state.df_master,
                    vehicle_catalog=st.session_state.vehicle_catalog,
                    selected_vehicle_names=selected_vehicle_names,
                    selected_pallet_names=st.session_state.selected_pallets,
                    pallet_catalog=st.session_state.pallet_catalog,
                    assumptions=st.session_state.assumptions,
                    selected_strategy_names=selected_strategy_names,
                )

                st.session_state.scenario_df = scenario_df

                st.success("Scenario comparison complete.")

        if (
            st.session_state.scenario_df is not None
            and not st.session_state.scenario_df.empty
        ):
            st.subheader("Scenario Results")

            st.dataframe(
                st.session_state.scenario_df,
                use_container_width=True,
                hide_index=True,
            )

            st.markdown("#### Best-Looking Options")

            scenario_df = st.session_state.scenario_df.copy()

            usable = scenario_df[
                scenario_df["Status"].isin(["OK", "REVIEW"])
            ].copy()

            if usable.empty:
                st.warning("No usable scenario results found.")

            else:
                usable["Containers"] = pd.to_numeric(
                    usable["Containers"],
                    errors="coerce",
                )

                usable["Unpacked Items"] = pd.to_numeric(
                    usable["Unpacked Items"],
                    errors="coerce",
                )

                usable["Avg Floor Util %"] = pd.to_numeric(
                    usable["Avg Floor Util %"],
                    errors="coerce",
                )

                usable = usable.sort_values(
                    by=[
                        "Unpacked Items",
                        "Containers",
                        "Status",
                        "Avg Floor Util %",
                    ],
                    ascending=[
                        True,
                        True,
                        True,
                        False,
                    ],
                )

                st.dataframe(
                    usable.head(10),
                    use_container_width=True,
                    hide_index=True,
                )

        st.divider()

        st.subheader("Scenario Export")

        if (
            st.session_state.scenario_df is not None
            and not st.session_state.scenario_df.empty
        ):
            scenario_export_name = (
                f"{today_file_stamp()}-"
                f"{slugify(st.session_state.project.display_name())}-"
                f"scenario-comparison.csv"
            )

            st.download_button(
                "Download Scenario CSV",
                data=st.session_state.scenario_df.to_csv(index=False).encode("utf-8"),
                file_name=scenario_export_name,
                mime="text/csv",
                use_container_width=True,
            )

        else:
            st.info("Run a scenario comparison to enable scenario export.")

# ==========================================================
# 28. FOOTER
# ==========================================================

st.divider()

st.caption(
    "UltraLogistics Pro — planning tool only. "
    "Final crate design, glass handling, blocking/bracing, freight loading, "
    "payload, route restrictions, and carrier requirements must be verified before shipment."
)
