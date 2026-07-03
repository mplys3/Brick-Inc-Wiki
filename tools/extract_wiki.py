from __future__ import annotations

import json
import re
import struct
import csv
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "$out"
WIKI = ROOT / "wiki_extract"

DUMP_CS = OUT / "il2cpp_dump" / "dump.cs"
STRING_LITERALS = OUT / "il2cpp_dump" / "stringliteral.json"
SCRIPT_JSON = OUT / "il2cpp_dump" / "script.json"
BASE_ASSETS = OUT / "base_apk" / "assets"
XAPK_MANIFEST = OUT / "xapk" / "manifest.json"


TYPE_RE = re.compile(
    r"^(?:public|private|internal|protected)?\s*"
    r"(?:sealed\s+|static\s+|abstract\s+|partial\s+)*"
    r"(class|struct|enum|interface)\s+([A-Za-z0-9_.<>`]+)"
)
FIELD_RE = re.compile(
    r"^\s*(?:public|private|protected|internal)\s+"
    r"(?:static\s+)?(?:readonly\s+)?"
    r"(?P<type>[A-Za-z0-9_.<>,\[\]\s]+?)\s+"
    r"(?P<name>[A-Za-z0-9_<>$]+)\s*;\s*//\s*(?P<offset>0x[0-9A-Fa-f]+)"
)
JSON_PROP_RE = re.compile(r'\[JsonProperty\("([^"]+)"\)\]')
ENUM_VALUE_RE = re.compile(
    r"^\s*public\s+const\s+(?P<enum>[A-Za-z0-9_.<>`]+)\s+"
    r"(?P<name>[A-Za-z0-9_]+)\s*=\s*(?P<value>[-0-9]+)"
)
METHOD_RVA_RE = re.compile(r"^\s*// RVA: (?P<rva>0x[0-9A-Fa-f]+)")
METHOD_RE = re.compile(
    r"^\s*(?:public|private|protected|internal)\s+"
    r"(?:static\s+|virtual\s+|override\s+|sealed\s+|async\s+)*"
    r"(?P<return>[A-Za-z0-9_.<>,\[\]\s]+?)\s+"
    r"(?P<name>[A-Za-z0-9_<>.]+)\s*\("
)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def write_md(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict], headers: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_dump():
    enums: dict[str, dict] = {}
    types: dict[str, dict] = {}
    methods: list[dict] = []
    master_settings: list[dict] = []

    current_type: str | None = None
    current_kind: str | None = None
    pending_json: str | None = None
    pending_rva: str | None = None

    with DUMP_CS.open("r", encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, 1):
            type_match = TYPE_RE.match(line.strip())
            if type_match:
                current_kind, current_type = type_match.groups()
                if current_kind == "enum":
                    enums.setdefault(current_type, {"name": current_type, "values": []})
                else:
                    types.setdefault(
                        current_type,
                        {
                            "name": current_type,
                            "kind": current_kind,
                            "line": line_no,
                            "json_fields": [],
                            "methods": [],
                        },
                    )
                pending_json = None
                pending_rva = None
                continue

            if current_type:
                enum_match = ENUM_VALUE_RE.match(line)
                if current_kind == "enum" and enum_match:
                    enum_name = enum_match.group("enum")
                    if enum_name == current_type:
                        enums[current_type]["values"].append(
                            {
                                "name": enum_match.group("name"),
                                "value": int(enum_match.group("value")),
                                "line": line_no,
                            }
                        )
                    continue

                json_match = JSON_PROP_RE.search(line)
                if json_match:
                    pending_json = json_match.group(1)
                    continue

                field_match = FIELD_RE.match(line)
                if field_match:
                    raw_name = field_match.group("name")
                    field = {
                        "json": pending_json,
                        "name": raw_name,
                        "type": " ".join(field_match.group("type").split()),
                        "offset": field_match.group("offset"),
                        "line": line_no,
                    }
                    types.setdefault(
                        current_type,
                        {
                            "name": current_type,
                            "kind": current_kind,
                            "line": line_no,
                            "json_fields": [],
                            "methods": [],
                        },
                    )["json_fields"].append(field)
                    if current_type == "M.Root" and field["type"].startswith("Master"):
                        clean_name = raw_name
                        if raw_name.startswith("<") and ">k__BackingField" in raw_name:
                            clean_name = raw_name[1:].split(">k__BackingField", 1)[0]
                        master_settings.append(
                            {
                                "json": "",
                                "field": clean_name,
                                "type": field["type"],
                                "offset": field["offset"],
                            }
                        )
                    elif pending_json and current_type == "Master_Content":
                        master_settings.append(
                            {
                                "json": pending_json,
                                "field": field["name"],
                                "type": field["type"],
                                "offset": field["offset"],
                            }
                        )
                    pending_json = None
                    continue

                rva_match = METHOD_RVA_RE.match(line)
                if rva_match:
                    pending_rva = rva_match.group("rva")
                    continue

                method_match = METHOD_RE.match(line)
                if method_match and pending_rva:
                    method = {
                        "type": current_type,
                        "name": method_match.group("name"),
                        "return": " ".join(method_match.group("return").split()),
                        "rva": pending_rva,
                        "line": line_no,
                    }
                    methods.append(method)
                    if current_type in types:
                        types[current_type]["methods"].append(method)
                    pending_rva = None

    interesting_types = {
        name: info
        for name, info in types.items()
        if (
            name.startswith("Master")
            or name.startswith("UserData")
            or name in {"Currency", "LargeNumber", "ElementId", "ElementInfo"}
        )
    }
    return enums, interesting_types, methods, master_settings


def summarize_strings():
    raw = json.loads(STRING_LITERALS.read_text(encoding="utf-8"))
    printable = []
    for item in raw:
        value = item.get("value", "")
        if not isinstance(value, str):
            continue
        if len(value) < 2 or len(value) > 240:
            continue
        if sum(ch.isprintable() and ch not in "\x00\r\n\t" for ch in value) < max(2, len(value) * 0.75):
            continue
        printable.append({"value": value, "address": item.get("address")})

    keywords = [
        "gold",
        "gem",
        "brick",
        "prestige",
        "upgrade",
        "weapon",
        "challenge",
        "science",
        "dimension",
        "artifact",
        "forge",
        "mining",
        "gacha",
        "rank",
        "reward",
        "mission",
        "cloud",
        "save",
    ]
    buckets = defaultdict(list)
    for item in printable:
        low = item["value"].lower()
        for key in keywords:
            if key in low and len(buckets[key]) < 300:
                buckets[key].append(item)

    return {
        "total": len(raw),
        "printable_count": len(printable),
        "sample": printable[:300],
        "keyword_buckets": buckets,
    }


def summarize_assets():
    files = []
    extension_counts = Counter()
    extension_sizes = Counter()
    for path in BASE_ASSETS.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(BASE_ASSETS).as_posix()
        size = path.stat().st_size
        ext = path.suffix.lower() or "<no_ext>"
        extension_counts[ext] += 1
        extension_sizes[ext] += size
        files.append({"path": rel, "size": size, "ext": ext})

    files.sort(key=lambda x: (-x["size"], x["path"]))
    return {
        "file_count": len(files),
        "total_size": sum(f["size"] for f in files),
        "extensions": [
            {"ext": ext, "count": extension_counts[ext], "size": extension_sizes[ext]}
            for ext, _ in extension_counts.most_common()
        ],
        "largest_files": files[:250],
        "addressables_files": [
            f for f in files if f["path"].startswith("aa/")
        ],
        "weapon_tokens": extract_asset_tokens(
            [
                rb"\bui_we_[a-z0-9_]+",
                rb"\bui_as_weapon[a-z0-9_]*",
                rb"\bui_bo_[a-z0-9_]*weapon[a-z0-9_]*",
                rb"\bbg_weapon_[a-z0-9_]+",
                rb"\btu_weapon[a-z0-9_]*",
                rb"\bweapon_ch\b",
            ],
            limit=500,
        ),
    }


def extract_asset_tokens(patterns: list[bytes], limit: int = 500) -> list[dict]:
    compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    hits: dict[str, set[str]] = defaultdict(set)
    for path in BASE_ASSETS.rglob("*"):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        for pattern in compiled:
            for match in pattern.finditer(data):
                token = match.group(0).decode("utf-8", errors="ignore")
                if not token:
                    continue
                rel = path.relative_to(BASE_ASSETS).as_posix()
                hits[token].add(rel)
                if len(hits) >= limit:
                    break
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break
    return [
        {"token": token, "sources": sorted(sources)[:5], "source_count": len(sources)}
        for token, sources in sorted(hits.items(), key=lambda item: item[0])
    ]


def export_weapon_icons(tokens: list[dict]) -> dict[str, str]:
    try:
        import UnityPy
    except ImportError:
        return {}

    icon_dir = WIKI / "assets" / "weapons" / "icons"
    icon_dir.mkdir(parents=True, exist_ok=True)
    exported: dict[str, str] = {}
    skip_prefixes = ("ui_we_btn_", "ui_we_icon_")
    for item in tokens:
        token = item["token"]
        if not token.startswith("ui_we_") or token.startswith(skip_prefixes):
            continue
        for source in sorted(item["sources"], key=lambda rel: (BASE_ASSETS / rel).stat().st_size):
            path = BASE_ASSETS / source
            try:
                env = UnityPy.load(str(path))
            except Exception:
                continue
            for obj in env.objects:
                try:
                    data = obj.read()
                    if obj.type.name not in {"Sprite", "Texture2D"} or not hasattr(data, "image"):
                        continue
                    image = data.image
                    if image is None:
                        continue
                    out_path = icon_dir / f"{token}.png"
                    image.save(out_path)
                    exported[token] = out_path.relative_to(WIKI).as_posix()
                    break
                except Exception:
                    continue
            if token in exported:
                break
    return exported


class FlatReader:
    def __init__(self, data: bytes):
        self.data = data

    def u8(self, offset: int) -> int:
        return self.data[offset]

    def u16(self, offset: int) -> int:
        return struct.unpack_from("<H", self.data, offset)[0]

    def i32(self, offset: int) -> int:
        return struct.unpack_from("<i", self.data, offset)[0]

    def u32(self, offset: int) -> int:
        return struct.unpack_from("<I", self.data, offset)[0]

    def f32(self, offset: int) -> float:
        return struct.unpack_from("<f", self.data, offset)[0]

    def f64(self, offset: int) -> float:
        return struct.unpack_from("<d", self.data, offset)[0]

    def root(self) -> int:
        return self.u32(0)

    def vtable(self, table: int) -> int:
        return table - self.i32(table)

    def field(self, table: int, index: int) -> int:
        vtable = self.vtable(table)
        slot = 4 + (index * 2)
        if slot >= self.u16(vtable):
            return 0
        field_offset = self.u16(vtable + slot)
        return table + field_offset if field_offset else 0

    def indirect(self, offset: int) -> int:
        return offset + self.i32(offset)

    def vector(self, offset: int) -> tuple[int, int]:
        vector = offset + self.i32(offset)
        return vector + 4, self.u32(vector)

    def string(self, offset: int) -> str:
        if not offset:
            return ""
        start, length = self.vector(offset)
        return self.data[start : start + length].decode("utf-8", errors="replace")

    def string_vector(self, table: int, index: int) -> list[str]:
        offset = self.field(table, index)
        if not offset:
            return []
        start, length = self.vector(offset)
        return [self.string(start + (i * 4)) for i in range(length)]

    def bool_vector(self, table: int, index: int) -> list[bool]:
        offset = self.field(table, index)
        if not offset:
            return []
        start, length = self.vector(offset)
        return [bool(self.data[start + i]) for i in range(length)]

    def int_vector(self, table: int, index: int) -> list[int]:
        offset = self.field(table, index)
        if not offset:
            return []
        start, length = self.vector(offset)
        return [self.i32(start + (i * 4)) for i in range(length)]

    def double_vector(self, table: int, index: int) -> list[float]:
        offset = self.field(table, index)
        if not offset:
            return []
        start, length = self.vector(offset)
        return [self.f64(start + (i * 8)) for i in range(length)]

    def get_i32(self, table: int, index: int, default: int = 0) -> int:
        offset = self.field(table, index)
        return self.i32(offset) if offset else default

    def get_f32(self, table: int, index: int, default: float = 0.0) -> float:
        offset = self.field(table, index)
        return self.f32(offset) if offset else default

    def get_f64(self, table: int, index: int, default: float = 0.0) -> float:
        offset = self.field(table, index)
        return self.f64(offset) if offset else default

    def get_bool(self, table: int, index: int, default: bool = False) -> bool:
        offset = self.field(table, index)
        return bool(self.u8(offset)) if offset else default

    def get_string(self, table: int, index: int) -> str:
        return self.string(self.field(table, index))


def load_text_asset_bytes(asset_name: str) -> bytes | None:
    try:
        import UnityPy
    except ImportError:
        return None

    for path in BASE_ASSETS.rglob("*"):
        if not path.is_file() or path.stat().st_size == 0:
            continue
        try:
            env = UnityPy.load(str(path))
        except Exception:
            continue
        for obj in env.objects:
            if obj.type.name != "TextAsset":
                continue
            try:
                data = obj.read()
            except Exception:
                continue
            if getattr(data, "m_Name", "") != asset_name:
                continue
            script = getattr(data, "m_Script", None)
            if script is None:
                return None
            if isinstance(script, str):
                return script.encode("utf-8", errors="surrogateescape")
            return bytes(script)
    return None


def decode_master_texts() -> dict[str, dict]:
    raw = load_text_asset_bytes("MText")
    if not raw:
        return {}
    reader = FlatReader(raw)
    root = reader.root()
    text_vector = reader.field(root, 0)
    if not text_vector:
        return {}
    start, length = reader.vector(text_vector)
    texts = {}
    for index in range(length):
        table = reader.indirect(start + (index * 4))
        row = {
            "id": reader.get_string(table, 0),
            "replaced": reader.get_string(table, 1),
            "ko": reader.get_string(table, 2),
            "en": reader.get_string(table, 3),
            "ja": reader.get_string(table, 4),
            "zh_tw": reader.get_string(table, 5),
            "es": reader.get_string(table, 6),
            "zh_cn": reader.get_string(table, 7),
            "ru": reader.get_string(table, 8),
        }
        if row["id"]:
            texts[row["id"]] = row
    return texts


def decode_master_weapons() -> dict:
    raw = load_text_asset_bytes("M")
    if not raw:
        return {"source": "M", "error": "TextAsset M not found", "setting": {}, "weapons": []}

    reader = FlatReader(raw)
    root = reader.root()
    texts = decode_master_texts()

    setting = {}
    setting_offset = reader.field(root, 3)
    if setting_offset:
        table = reader.indirect(setting_offset)
        setting = {
            "CooltimeMin": reader.get_f64(table, 0),
            "StandardPower": reader.get_f64(table, 1),
            "EnhanceDmgBonus": reader.get_i32(table, 2),
            "EnhanceMaxDefault": reader.get_i32(table, 3),
            "EnhancePriceUnit": reader.get_i32(table, 4),
            "EnhancePriceMultiplier": reader.get_f64(table, 5),
            "SpecialGradeEnhances": reader.int_vector(table, 6),
            "SpecialGrade1Power": reader.get_f64(table, 7),
            "SpecialGrade2Dmg": reader.get_f64(table, 8),
            "SpecialGrade3Dmg": reader.get_f64(table, 9),
        }

    weapons = []
    weapon_vector = reader.field(root, 65)
    if weapon_vector:
        start, length = reader.vector(weapon_vector)
        for index in range(length):
            table = reader.indirect(start + (index * 4))
            key = reader.get_string(table, 1)
            name_text = texts.get(f"name_{key}", {})
            desc_text = texts.get(f"desc_{key}", {})
            special_texts = [
                texts[text_key]["en"]
                for text_key in [f"sp1_{key}", f"sp2_{key}", f"sp3_{key}"]
                if text_key in texts and texts[text_key].get("en")
            ]
            weapons.append(
                {
                    "Id": reader.get_i32(table, 0),
                    "NameKey": key,
                    "English": name_text.get("en", ""),
                    "Description": desc_text.get("en", ""),
                    "SpecialText": special_texts,
                    "Sort": reader.get_i32(table, 2),
                    "IsDefault": reader.get_bool(table, 3),
                    "UnlockIndex": reader.get_i32(table, 4),
                    "UnlockWeapons": reader.string_vector(table, 5),
                    "UnlockWeaponEnhance": reader.get_i32(table, 6),
                    "Price": reader.get_string(table, 7),
                    "EnhanceCost": reader.get_string(table, 8),
                    "EnhanceCostUp": reader.get_f64(table, 9),
                    "SoulDmgUpgradeCost": reader.get_string(table, 10),
                    "SoulCostUpgradeCost": reader.get_string(table, 11),
                    "SoulUpgradeCostUp": reader.get_f64(table, 12),
                    "SoulDmgEnhanceCost": reader.get_string(table, 13),
                    "SoulCostEnhanceCost": reader.get_string(table, 14),
                    "SoulEnhanceCostUp": reader.get_f64(table, 15),
                    "EpicUpgradeCost": reader.get_i32(table, 16),
                    "Power": reader.get_string(table, 17),
                    "RagrangeBattleBonus": reader.get_f64(table, 18),
                    "NoSpeed": reader.bool_vector(table, 19),
                    "NoDuration": reader.bool_vector(table, 20),
                    "NoSize": reader.bool_vector(table, 21),
                    "UseSkill": reader.get_bool(table, 22),
                    "Cooldown": reader.get_f32(table, 23),
                    "BasicData": reader.get_string(table, 24),
                    "InfoData": reader.string_vector(table, 25),
                }
            )

    return {
        "source": "M",
        "root_table": root,
        "weapon_count": len(weapons),
        "text_count": len(texts),
        "setting": setting,
        "weapons": weapons,
    }


def enum_lookup(enums: dict, enum_name: str) -> dict[int, str]:
    enum = enums.get(enum_name, {})
    return {item["value"]: item["name"] for item in enum.get("values", [])}


def decode_master_science(enums: dict) -> dict:
    raw = load_text_asset_bytes("M")
    if not raw:
        return {"source": "M", "error": "TextAsset M not found", "science_setting": {}, "mode_content_science_setting": {}, "mode_science": []}

    reader = FlatReader(raw)
    root = reader.root()
    texts = decode_master_texts()
    upgrade_types = enum_lookup(enums, "MasterUpgradeType")
    value_types = enum_lookup(enums, "MasterUpgradeValueType")

    def read_table(field_index: int, fields: list[tuple[str, str, int]]) -> dict:
        offset = reader.field(root, field_index)
        if not offset:
            return {}
        table = reader.indirect(offset)
        row = {}
        for name, kind, index in fields:
            if kind == "int":
                row[name] = reader.get_i32(table, index)
            elif kind == "double":
                row[name] = reader.get_f64(table, index)
            elif kind == "string":
                row[name] = reader.get_string(table, index)
        return row

    science_setting = read_table(
        21,
        [
            ("MaxRank", "int", 0),
            ("Point", "double", 1),
            ("PointTime", "double", 2),
            ("RankExp", "double", 3),
            ("RankExpUp", "double", 4),
            ("PointUpgradeLevelMax", "int", 5),
            ("PointUpgrade", "double", 6),
            ("PointUpgradeCost", "double", 7),
            ("PointUpgradeCostUp", "double", 8),
            ("PointUpgradeDevilOrderLevelMax", "int", 9),
            ("PointUpgradeDevilOrder", "double", 10),
            ("PointUpgradeDevilOrderCost", "string", 11),
            ("PointUpgradeDevilOrderCostUp", "double", 12),
            ("PointUpgradeDevilOrderCostUpPow", "double", 13),
            ("PointUpgradeWarAILevelMax", "int", 14),
            ("PointUpgradeWarAI", "double", 15),
            ("PointUpgradeWarAICost", "string", 16),
            ("PointUpgradeWarAICostUp", "double", 17),
            ("PointUpgradeWarAICostUpPow", "double", 18),
            ("PointUpgradeGoldenCubeLevelMax", "int", 19),
            ("PointUpgradeGoldenCube", "double", 20),
            ("PointUpgradeGoldenCubeCost", "string", 21),
            ("PointUpgradeGoldenCubeCostUp", "double", 22),
            ("PointUpgradeGoldenCubeCostUpPow", "double", 23),
            ("AdsSciencePointTime", "int", 24),
        ],
    )
    mode_content_science_setting = read_table(
        19,
        [
            ("PointUpgradeScience", "double", 0),
            ("PointUpgradeScienceCost", "double", 1),
            ("PointUpgradeScienceCostUp", "double", 2),
            ("PointUpgradeGold", "double", 3),
            ("PointUpgradeGoldCost", "string", 4),
            ("PointUpgradeGoldCostUp", "double", 5),
            ("PointUpgradeGoldCostUpPow", "double", 6),
            ("PointUpgradePower", "double", 7),
            ("PointUpgradePowerCost", "string", 8),
            ("PointUpgradePowerCostUp", "double", 9),
            ("PointUpgradePowerCostUpPow", "double", 10),
            ("PointUpgradeSoul", "double", 11),
            ("PointUpgradeSoulCost", "string", 12),
            ("PointUpgradeSoulCostUp", "double", 13),
            ("PointUpgradeSoulCostUpPow", "double", 14),
        ],
    )

    mode_science = []
    vector_offset = reader.field(root, 112)
    if vector_offset:
        start, length = reader.vector(vector_offset)
        for index in range(length):
            table = reader.indirect(start + (index * 4))
            key = reader.get_string(table, 1)
            type_ids = reader.int_vector(table, 3)
            value_type_ids = reader.int_vector(table, 4)
            row = {
                "Id": reader.get_i32(table, 0),
                "NameKey": key,
                "English": texts.get(f"name_{key}", {}).get("en", texts.get(key, {}).get("en", "")),
                "Description": texts.get(f"desc_{key}", {}).get("en", ""),
                "Sort": reader.get_i32(table, 2),
                "TypeList": [upgrade_types.get(value, str(value)) for value in type_ids],
                "TypeListRaw": type_ids,
                "ValueTypeList": [value_types.get(value, str(value)) for value in value_type_ids],
                "ValueTypeListRaw": value_type_ids,
                "MaxLevel": reader.get_i32(table, 5),
                "ValueUp": reader.double_vector(table, 6),
                "Cost": reader.get_string(table, 7),
                "CostUp": reader.get_f64(table, 8),
            }
            mode_science.append(row)

    return {
        "source": "M",
        "root_table": root,
        "text_count": len(texts),
        "science_setting": science_setting,
        "mode_content_science_setting": mode_content_science_setting,
        "mode_science_count": len(mode_science),
        "mode_science": mode_science,
    }


def load_configs():
    configs = {}
    for name in [
        "UnityServicesProjectConfiguration.json",
        "google-services-desktop.json",
        "aa/settings.json",
    ]:
        path = BASE_ASSETS / name
        if path.exists():
            try:
                configs[name] = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                configs[name] = {"raw": path.read_text(encoding="utf-8", errors="replace")}
    if XAPK_MANIFEST.exists():
        configs["xapk_manifest.json"] = json.loads(XAPK_MANIFEST.read_text(encoding="utf-8"))
    return configs


def table(rows, headers):
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(h, "")).replace("|", "\\|") for h in headers) + " |")
    return "\n".join(lines)


def emit_markdown(enums, types, methods, master_settings, strings, assets, configs):
    manifest = configs.get("xapk_manifest.json", {})
    services = configs.get("UnityServicesProjectConfiguration.json", {})
    service_pairs = []
    if "Keys" in services and "Values" in services:
        for key, value in zip(services["Keys"], services["Values"]):
            service_pairs.append({"key": key, "value": value.get("m_Value", "")})

    systems_rows = [
        {
            "field": item["field"],
            "type": item["type"],
            "json": item["json"],
            "offset": item["offset"],
        }
        for item in master_settings
    ]

    save_types = {k: v for k, v in types.items() if k in {"UserData", "UserData.GameData", "Currency", "LargeNumber"}}

    write_md(
        WIKI / "README.md",
        f"""# Brick Inc Wiki Extract

Source build: `{manifest.get("name", "Brick Inc")}` `{manifest.get("version_name", "unknown")}` / versionCode `{manifest.get("version_code", "unknown")}`.

This folder is generated from the local APK/XAPK and IL2CPP dump. It is a first-pass wiki seed, not a complete public wiki yet.

## Pages

- [Overview](Overview.md)
- [Systems Index](Systems.md)
- [Weapons](Weapons.md)
- [Science](Science.md)
- [Save Data Structure](SaveData.md)
- [Enums](Enums.md)
- [String Literals](Strings.md)
- [Assets](Assets.md)

## Extracted Data

- Master systems found: `{len(master_settings)}`
- Interesting types documented: `{len(types)}`
- Enums found: `{len(enums)}`
- Printable string literals sampled: `{strings["printable_count"]}` / `{strings["total"]}`
- Asset files indexed: `{assets["file_count"]}`
""",
    )

    write_md(
        WIKI / "Overview.md",
        f"""# Overview

## APK

| Field | Value |
| --- | --- |
| Package | `{manifest.get("package_name", "")}` |
| Name | `{manifest.get("name", "")}` |
| Version | `{manifest.get("version_name", "")}` |
| Version code | `{manifest.get("version_code", "")}` |
| Min SDK | `{manifest.get("min_sdk_version", "")}` |
| Target SDK | `{manifest.get("target_sdk_version", "")}` |
| Split configs | `{", ".join(manifest.get("split_configs", []))}` |

## Unity Services

{table(service_pairs[:80], ["key", "value"])}
""",
    )

    write_md(
        WIKI / "Systems.md",
        "# Systems Index\n\n" + table(systems_rows, ["field", "type", "json", "offset"]),
    )

    save_sections = ["# Save Data Structure\n"]
    for type_name, info in save_types.items():
        rows = [
            {
                "json": f.get("json") or "",
                "field": f["name"],
                "type": f["type"],
                "offset": f["offset"],
            }
            for f in info.get("json_fields", [])
        ]
        save_sections.append(f"## {type_name}\n\n" + table(rows, ["json", "field", "type", "offset"]))
    write_md(WIKI / "SaveData.md", "\n\n".join(save_sections))

    enum_sections = ["# Enums\n"]
    for name in sorted(enums):
        values = enums[name]["values"]
        if not values:
            continue
        enum_sections.append(
            f"## {name}\n\n"
            + table(
                [{"name": v["name"], "value": v["value"]} for v in values[:400]],
                ["name", "value"],
            )
        )
    write_md(WIKI / "Enums.md", "\n\n".join(enum_sections))

    string_sections = [
        "# String Literals\n",
        f"Printable strings: `{strings['printable_count']}` / `{strings['total']}`.",
    ]
    for key, items in strings["keyword_buckets"].items():
        string_sections.append(
            f"## {key}\n\n"
            + table(
                [{"value": item["value"], "address": item["address"]} for item in items[:80]],
                ["value", "address"],
            )
        )
    write_md(WIKI / "Strings.md", "\n\n".join(string_sections))

    asset_rows = [
        {"ext": e["ext"], "count": e["count"], "size": e["size"]}
        for e in assets["extensions"]
    ]
    largest_rows = [
        {"path": f["path"], "size": f["size"], "ext": f["ext"]}
        for f in assets["largest_files"][:100]
    ]
    write_md(
        WIKI / "Assets.md",
        "# Assets\n\n"
        f"Total files: `{assets['file_count']}`. Total size: `{assets['total_size']}` bytes.\n\n"
        "## By Extension\n\n"
        + table(asset_rows, ["ext", "count", "size"])
        + "\n\n## Largest Files\n\n"
        + table(largest_rows, ["path", "size", "ext"]),
    )

    emit_weapons_page(enums, types, methods, strings, assets)
    emit_science_page(enums, types, methods, strings)


def emit_weapons_page(enums, types, methods, strings, assets):
    weapon_setting_fields = [
        {"field": "CooltimeMin", "type": "double", "meaning": "Global minimum cooldown clamp for weapons."},
        {"field": "StandardPower", "type": "double", "meaning": "Baseline power scalar used by weapon calculations."},
        {"field": "EnhanceDmgBonus", "type": "int", "meaning": "Damage bonus per enhance/upgrade step."},
        {"field": "EnhanceMaxDefault", "type": "int", "meaning": "Default maximum enhance level."},
        {"field": "EnhancePriceUnit", "type": "int", "meaning": "Base unit for enhance pricing."},
        {"field": "EnhancePriceMultiplier", "type": "double", "meaning": "Multiplier applied to enhance price growth."},
        {"field": "SpecialGradeEnhances", "type": "int[]", "meaning": "Enhance thresholds or requirements for special grades."},
        {"field": "SpecialGrade1Power", "type": "double", "meaning": "Power scalar for special grade 1."},
        {"field": "SpecialGrade2Dmg", "type": "double", "meaning": "Damage scalar/bonus for special grade 2."},
        {"field": "SpecialGrade3Dmg", "type": "double", "meaning": "Damage scalar/bonus for special grade 3."},
    ]
    master_weapon_fields = [
        {"field": "Id", "type": "int", "meaning": "Weapon id/key."},
        {"field": "Name", "type": "string", "meaning": "Localization key/name token."},
        {"field": "Sort", "type": "int", "meaning": "Display/order value."},
        {"field": "IsDefault", "type": "bool", "meaning": "Whether this is a default starting weapon."},
        {"field": "UnlockIndex", "type": "int", "meaning": "Unlock ordering/index."},
        {"field": "UnlockWeapons", "type": "string[]", "meaning": "Weapon ids/keys required to unlock this weapon."},
        {"field": "UnlockWeaponEnhance", "type": "int", "meaning": "Enhance requirement for unlock."},
        {"field": "Price", "type": "string", "meaning": "LargeNumber string for buy price."},
        {"field": "EnhanceCost", "type": "string", "meaning": "LargeNumber string for enhance base cost."},
        {"field": "EnhanceCostUp", "type": "double", "meaning": "Enhance cost growth multiplier."},
        {"field": "SoulDmgUpgradeCost", "type": "string", "meaning": "Soul damage upgrade base cost."},
        {"field": "SoulCostUpgradeCost", "type": "string", "meaning": "Soul cost upgrade base cost."},
        {"field": "SoulUpgradeCostUp", "type": "double", "meaning": "Soul upgrade cost growth multiplier."},
        {"field": "SoulDmgEnhanceCost", "type": "string", "meaning": "Soul damage enhance base cost."},
        {"field": "SoulCostEnhanceCost", "type": "string", "meaning": "Soul cost enhance base cost."},
        {"field": "SoulEnhanceCostUp", "type": "double", "meaning": "Soul enhance cost growth multiplier."},
        {"field": "EpicUpgradeCost", "type": "int", "meaning": "Epic upgrade cost id/value."},
        {"field": "Power", "type": "string", "meaning": "LargeNumber string for base weapon power."},
        {"field": "RagrangeBattleBonus", "type": "double", "meaning": "Battle bonus for Lagrange mode."},
        {"field": "NoSpeed", "type": "bool[]", "meaning": "Flags disabling speed bonuses/options by grade/index."},
        {"field": "NoDuration", "type": "bool[]", "meaning": "Flags disabling duration bonuses/options by grade/index."},
        {"field": "NoSize", "type": "bool[]", "meaning": "Flags disabling size bonuses/options by grade/index."},
        {"field": "UseSkill", "type": "bool", "meaning": "Whether weapon uses a skill implementation."},
        {"field": "Cooldown", "type": "float", "meaning": "Weapon-specific cooldown."},
        {"field": "BasicData", "type": "string", "meaning": "Key into weapon basic data/scriptable object."},
        {"field": "InfoData", "type": "string[]", "meaning": "Extra info/localization/data tokens."},
    ]

    user_weapon_rows = []
    for type_name in ["UserData.WeaponSlotData", "UserData.WeaponData"]:
        info = types.get(type_name)
        if not info:
            continue
        for field in info.get("json_fields", []):
            user_weapon_rows.append(
                {
                    "type": type_name,
                    "json": field.get("json") or "",
                    "field": field["name"],
                    "field_type": field["type"],
                    "offset": field["offset"],
                }
            )

    enum_rows = []
    enum_names = [
        "MasterElementType",
        "MasterUpgradeType",
        "MasterUpgradeReferType",
        "MasterSystemAssistType",
        "MasterGameConstraintType",
        "MasterGameGoalType",
        "GameWeaponOptionFlag",
        "BattleWeaponSlotView.ButtonActionType",
    ]
    for enum_name in enum_names:
        enum = enums.get(enum_name)
        if not enum:
            continue
        for value in enum["values"]:
            if "Weapon" in value["name"] or enum_name in {"GameWeaponOptionFlag", "BattleWeaponSlotView.ButtonActionType"}:
                enum_rows.append({"enum": enum_name, "name": value["name"], "value": value["value"]})

    method_rows = []
    for method in methods:
        full = f"{method['type']}.{method['name']}"
        if "Weapon" in full and len(method_rows) < 250:
            method_rows.append(
                {
                    "type": method["type"],
                    "method": method["name"],
                    "return": method["return"],
                    "rva": method["rva"],
                }
            )

    string_rows = []
    for item in strings["keyword_buckets"].get("weapon", []):
        string_rows.append({"value": item["value"], "address": item["address"]})

    asset_rows = []
    token_rows = []
    for item in assets["largest_files"]:
        path = item["path"].lower()
        if "weapon" in path or "hammer" in path or "blade" in path or "cannon" in path:
            asset_rows.append({"path": item["path"], "size": item["size"], "ext": item["ext"]})
    for item in assets["addressables_files"]:
        path = item["path"].lower()
        if ("weapon" in path or "hammer" in path or "blade" in path or "cannon" in path) and len(asset_rows) < 200:
            row = {"path": item["path"], "size": item["size"], "ext": item["ext"]}
            if row not in asset_rows:
                asset_rows.append(row)
    icon_paths = export_weapon_icons(assets.get("weapon_tokens", []))
    for item in assets.get("weapon_tokens", []):
        icon = icon_paths.get(item["token"], "")
        token_rows.append(
            {
                "token": item["token"],
                "icon": f"![{item['token']}]({icon})" if icon else "",
                "source_count": item["source_count"],
                "sources": ", ".join(item["sources"]),
            }
        )
    decoded = decode_master_weapons()
    decoded_setting_rows = [
        {"field": key, "value": json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value}
        for key, value in decoded.get("setting", {}).items()
    ]
    decoded_weapon_rows = []
    for row in decoded.get("weapons", []):
        icon = icon_paths.get(f"ui_{row['NameKey']}", "")
        decoded_weapon_rows.append(
            {
                "icon": f"![{row['NameKey']}]({icon})" if icon else "",
                "Id": row["Id"],
                "English": row["English"],
                "NameKey": row["NameKey"],
                "Description": row["Description"],
                "Sort": row["Sort"],
                "IsDefault": row["IsDefault"],
                "UnlockIndex": row["UnlockIndex"],
                "Price": row["Price"],
                "EnhanceCost": row["EnhanceCost"],
                "Power": row["Power"],
                "Cooldown": row["Cooldown"],
                "UseSkill": row["UseSkill"],
                "SpecialText": " / ".join(row["SpecialText"]),
                "InfoData": ", ".join(row["InfoData"]),
            }
        )

    data = {
        "weapon_setting_fields": weapon_setting_fields,
        "master_weapon_fields": master_weapon_fields,
        "decoded_master_weapons": decoded,
        "user_weapon_fields": user_weapon_rows,
        "weapon_related_enums": enum_rows,
        "weapon_methods_sample": method_rows,
        "weapon_strings": string_rows,
        "weapon_assets": asset_rows,
        "weapon_asset_tokens": token_rows,
    }
    write_json(WIKI / "data" / "weapons.json", data)
    write_csv(
        WIKI / "data" / "weapons.csv",
        decoded_weapon_rows,
        [
            "Id",
            "English",
            "NameKey",
            "Description",
            "Sort",
            "IsDefault",
            "UnlockIndex",
            "Price",
            "EnhanceCost",
            "Power",
            "Cooldown",
            "UseSkill",
            "SpecialText",
            "InfoData",
        ],
    )

    write_md(
        WIKI / "Weapons.md",
        "# Weapons\n\n"
        "This page is generated from the APK's IL2CPP schema plus decoded FlatBuffer master data from the `M` TextAsset.\n\n"
        f"Decoded weapon rows: `{decoded.get('weapon_count', 0)}`. Decoded text rows available for localization: `{decoded.get('text_count', 0)}`.\n\n"
        "## Decoded MasterWeaponSetting\n\n"
        + table(decoded_setting_rows, ["field", "value"])
        + "\n\n## Decoded MasterWeapon Rows\n\n"
        + table(
            decoded_weapon_rows,
            [
                "icon",
                "Id",
                "English",
                "NameKey",
                "Description",
                "Sort",
                "IsDefault",
                "UnlockIndex",
                "Price",
                "EnhanceCost",
                "Power",
                "Cooldown",
                "UseSkill",
                "SpecialText",
                "InfoData",
            ],
        )
        + "\n\n## MasterWeaponSetting Schema\n\n"
        + table(weapon_setting_fields, ["field", "type", "meaning"])
        + "\n\n## MasterWeapon Row Schema\n\n"
        + table(master_weapon_fields, ["field", "type", "meaning"])
        + "\n\n## Player Save Fields\n\n"
        + table(user_weapon_rows, ["type", "json", "field", "field_type", "offset"])
        + "\n\n## Weapon-Related Enums\n\n"
        + table(enum_rows, ["enum", "name", "value"])
        + "\n\n## Weapon String Keys\n\n"
        + table(string_rows[:120], ["value", "address"])
        + "\n\n## Weapon Methods Sample\n\n"
        + table(method_rows[:120], ["type", "method", "return", "rva"])
        + "\n\n## Weapon Asset Tokens\n\n"
        + table(token_rows[:160], ["icon", "token", "source_count", "sources"])
        + "\n\n## Weapon Asset Candidates\n\n"
        + table(asset_rows[:120], ["path", "size", "ext"]),
    )


def emit_science_page(enums, types, methods, strings):
    decoded = decode_master_science(enums)
    science_setting_rows = [
        {"field": key, "value": value}
        for key, value in decoded.get("science_setting", {}).items()
    ]
    mode_content_rows = [
        {"field": key, "value": value}
        for key, value in decoded.get("mode_content_science_setting", {}).items()
    ]
    science_rows = []
    for row in decoded.get("mode_science", []):
        science_rows.append(
            {
                "Id": row["Id"],
                "English": row["English"],
                "NameKey": row["NameKey"],
                "Description": row["Description"],
                "Sort": row["Sort"],
                "Types": ", ".join(row["TypeList"]),
                "ValueTypes": ", ".join(row["ValueTypeList"]),
                "MaxLevel": row["MaxLevel"],
                "ValueUp": ", ".join(str(value) for value in row["ValueUp"]),
                "Cost": row["Cost"],
                "CostUp": row["CostUp"],
            }
        )

    method_rows = []
    for method in methods:
        full = f"{method['type']}.{method['name']}"
        if "Science" in full and len(method_rows) < 200:
            method_rows.append(
                {
                    "type": method["type"],
                    "method": method["name"],
                    "return": method["return"],
                    "rva": method["rva"],
                }
            )

    enum_rows = []
    for enum_name in ["MasterUpgradeType", "MasterUpgradeValueType", "MasterElementType", "MasterMissionType"]:
        enum = enums.get(enum_name)
        if not enum:
            continue
        for value in enum["values"]:
            if "Science" in value["name"] or value["name"] in {
                item for row in decoded.get("mode_science", []) for item in row.get("TypeList", [])
            }:
                enum_rows.append({"enum": enum_name, "name": value["name"], "value": value["value"]})

    string_rows = [
        {"value": item["value"], "address": item["address"]}
        for item in strings["keyword_buckets"].get("science", [])
    ]

    data = {
        "decoded_master_science": decoded,
        "science_methods_sample": method_rows,
        "science_related_enums": enum_rows,
        "science_strings": string_rows,
    }
    write_json(WIKI / "data" / "science.json", data)
    write_csv(
        WIKI / "data" / "science.csv",
        science_rows,
        [
            "Id",
            "English",
            "NameKey",
            "Description",
            "Sort",
            "Types",
            "ValueTypes",
            "MaxLevel",
            "ValueUp",
            "Cost",
            "CostUp",
        ],
    )

    write_md(
        WIKI / "Science.md",
        "# Science\n\n"
        "This page is generated from the APK's IL2CPP schema plus decoded FlatBuffer master data from the `M` TextAsset.\n\n"
        f"Decoded ModeScience rows: `{decoded.get('mode_science_count', 0)}`. Decoded text rows available for localization: `{decoded.get('text_count', 0)}`.\n\n"
        "## MasterScienceSetting\n\n"
        + table(science_setting_rows, ["field", "value"])
        + "\n\n## MasterModeContentScienceSetting\n\n"
        + table(mode_content_rows, ["field", "value"])
        + "\n\n## ModeScience Rows\n\n"
        + table(
            science_rows,
            [
                "Id",
                "English",
                "NameKey",
                "Description",
                "Sort",
                "Types",
                "ValueTypes",
                "MaxLevel",
                "ValueUp",
                "Cost",
                "CostUp",
            ],
        )
        + "\n\n## Science-Related Enums\n\n"
        + table(enum_rows, ["enum", "name", "value"])
        + "\n\n## Science String Keys\n\n"
        + table(string_rows[:120], ["value", "address"])
        + "\n\n## Science Methods Sample\n\n"
        + table(method_rows[:120], ["type", "method", "return", "rva"]),
    )


def main():
    WIKI.mkdir(parents=True, exist_ok=True)
    enums, types, methods, master_settings = parse_dump()
    strings = summarize_strings()
    assets = summarize_assets()
    configs = load_configs()

    write_json(WIKI / "data" / "enums.json", enums)
    write_json(WIKI / "data" / "types_interesting.json", types)
    write_json(WIKI / "data" / "methods.json", methods)
    write_json(WIKI / "data" / "master_settings.json", master_settings)
    write_json(WIKI / "data" / "strings_summary.json", strings)
    write_json(WIKI / "data" / "assets_index.json", assets)
    write_json(WIKI / "data" / "configs.json", configs)

    emit_markdown(enums, types, methods, master_settings, strings, assets, configs)

    print(f"Wrote {WIKI}")
    print(f"Master systems: {len(master_settings)}")
    print(f"Interesting types: {len(types)}")
    print(f"Enums: {len(enums)}")
    print(f"Assets: {assets['file_count']}")


if __name__ == "__main__":
    main()
