from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Listing:
    """Одне оголошення, як його бачить джерело (OLX тощо)."""

    source: str
    ext_id: str
    url: str
    title: str
    description: str
    price: int | None  # у валюті currency
    currency: str
    negotiable: bool
    photos: list[str]
    city: str
    region: str
    is_business: bool
    seller_name: str
    created_at: str
    refreshed_at: str
    params: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.ext_id}"


@dataclass
class Specs:
    """Що вдалося витягти регулярками з заголовка/опису (до AI)."""

    chip: str | None = None  # "M1" / "M2"
    ram_gb: int | None = None
    ssd_gb: int | None = None
    screen: str | None = None  # "13" / "15"
    battery_pct: int | None = None
    battery_cycles: int | None = None
    reject_reason: str | None = None  # чому відкинули (аксесуар, Pro, M3 тощо)
