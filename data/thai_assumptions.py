"""
Official Thai / BMTA parameters for cost & emissions modelling.

Sources are cited on each constant. Update when ERC / EPPO / TGO / BMTA refresh.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Electricity — ERC / MEA·PEA retail structure
# ---------------------------------------------------------------------------
# Medium business TOU (Type 3) energy charges + Ft fuel adjustment.
# Period: May–Aug 2026 (พ.ค.–ส.ค. 2569) per ERC Ft announcement.
# EV depot charging is modelled as overnight Off-Peak.
# Refs:
#   - ERC tariff tables (กกพ.): https://www.erc.or.th/th/tariff
#   - MEA / PEA TOU Type 3 Off-Peak energy ≈ 2.6037 THB/kWh
#   - ERC Ft May–Aug 2026 = 0.1623 THB/kWh
ELECTRICITY_OFFPEAK_THB_PER_KWH = 2.6037
ELECTRICITY_FT_THB_PER_KWH = 0.1623
ELECTRICITY_THB_PER_KWH = round(
    ELECTRICITY_OFFPEAK_THB_PER_KWH + ELECTRICITY_FT_THB_PER_KWH, 4
)  # 2.766 → overnight depot charge

# ---------------------------------------------------------------------------
# Diesel — Bangkok retail diesel (EPPO-tracked oil company prices)
# ---------------------------------------------------------------------------
# High-speed diesel retail Bangkok, OR / Bangchak announce 8 Jul 2026:
# Diesel 34.94 THB/litre (excl. some local maintenance tax).
# Official statistics home: https://www.eppo.go.th/ (Petroleum Price Statistic)
# EPPO open catalog: https://catalog.eppo.go.th/ne/dataset/dataset_11_58
DIESEL_THB_PER_LITRE = 34.94

# ---------------------------------------------------------------------------
# Grid emission factor — TGO (Thailand Greenhouse Gas Management Organization)
# ---------------------------------------------------------------------------
# Demand-side electricity consumption EF for Standard T-VER / LESS (latest
# published demand-side series ≈ 0.4758 tCO2/MWh = 0.4758 kgCO2/kWh).
# TGO Carbon Footprint for Organization Scope 2 grid mix 2022–2024: 0.475
# kgCO2e/kWh (Thai National LCI / TGO).
# https://ghgreduction.tgo.or.th/
# https://thaicarbonlabel.tgo.or.th/
GRID_CO2_KG_PER_KWH = 0.4758

# Diesel tank-to-wheel CO2 — IPCC 2006 Guidelines (mobile combustion, diesel)
# ≈ 2.68 kg CO2 / litre (widely used with EPPO fuel volumes)
DIESEL_CO2_KG_PER_LITRE = 2.68

# ---------------------------------------------------------------------------
# BMTA vehicle capacity
# ---------------------------------------------------------------------------
# BMTA EV bus TOR (ขสมก. clean-energy AC EV lease specs): single-deck 10–12 m,
# seating capacity ≥ 31 (incl. 4 priority seats for persons with disabilities).
# Total design / crush load for a 10–12 m urban single-deck AC bus in Bangkok
# operations is modelled at 60 passengers (seated + standing) — consistent with
# BMTA urban service capacity used in fleet planning and EGAT demo buses
# (≈30–50 seated class, higher with standees).
# Refs: BMTA EV procurement TOR / BMTA restructuring EV lease programme.
BMTA_EV_SEATED_MIN = 31
BMTA_BUS_CAPACITY = 60


ASSUMPTION_SOURCES: dict[str, Any] = {
    "electricity_thb_per_kwh": {
        "value": ELECTRICITY_THB_PER_KWH,
        "label": "MEA/PEA Type 3 TOU Off-Peak + ERC Ft (May–Aug 2026)",
        "url": "https://www.erc.or.th/th/tariff",
        "note": "Depot overnight charging; Ft = 0.1623 THB/kWh.",
    },
    "diesel_thb_per_litre": {
        "value": DIESEL_THB_PER_LITRE,
        "label": "Bangkok retail diesel (OR/Bangchak 8 Jul 2026; EPPO statistics)",
        "url": "https://www.eppo.go.th/index.php/en/en-energystatistics/petroleumprice-statistic",
        "note": "34.94 THB/L high-speed diesel, Bangkok announced pump price.",
    },
    "grid_co2_kg_per_kwh": {
        "value": GRID_CO2_KG_PER_KWH,
        "label": "TGO demand-side / CFO grid EF (≈0.4758 kgCO2/kWh)",
        "url": "https://ghgreduction.tgo.or.th/",
        "note": "Thailand Greenhouse Gas Management Organization electricity EF.",
    },
    "diesel_co2_kg_per_l": {
        "value": DIESEL_CO2_KG_PER_LITRE,
        "label": "IPCC 2006 diesel tank-to-wheel ≈ 2.68 kgCO2/L",
        "url": "https://www.ipcc-nggip.iges.or.jp/",
        "note": "Paired with EPPO diesel litres for baseline fleet CO₂.",
    },
    "bus_capacity": {
        "value": BMTA_BUS_CAPACITY,
        "label": "BMTA 10–12 m EV/AC urban bus ≈ 60 pax (TOR seated ≥31)",
        "url": "https://www.bmta.co.th/",
        "note": "Seated minimum from BMTA EV TOR; 60 = seated + standing crush load.",
    },
}
