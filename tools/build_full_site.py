from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import html
import json
import re
import shutil
from pathlib import Path

import extract_wiki


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "$out"
DUMP_CS = OUT / "il2cpp_dump" / "dump.cs"
WIKI = ROOT / "wiki_extract"
SITE = ROOT / "site"
DOCS = ROOT / "docs"


STRUCT_RE = re.compile(r"^public struct (?P<name>Master[A-Za-z0-9_]+) : IFlatbufferObject")
CREATE_RE = re.compile(r"public static Offset<(?P<name>Master[A-Za-z0-9_]+)> Create(?P=name)\(FlatBufferBuilder builder(?:, (?P<params>.*?))?\) \{ \}")
VECTOR_METHOD_RE = re.compile(r"public (?P<return>[A-Za-z0-9_.<>]+) (?P<name>[A-Za-z0-9_]+)\(int j\) \{ \}")
ROOT_SETTING_RE = re.compile(r"public Nullable<(?P<type>Master[A-Za-z0-9_]+)> (?P<name>[A-Za-z0-9_]+) \{ get; \}")
ROOT_VECTOR_RE = re.compile(r"public int (?P<name>[A-Za-z0-9_]+)Length \{ get; \}")
WEAPON_LARGE_NUMBER_FIELDS = {
    "Price",
    "EnhanceCost",
    "SoulDmgUpgradeCost",
    "SoulCostUpgradeCost",
    "SoulDmgEnhanceCost",
    "SoulCostEnhanceCost",
    "SoulEnhanceCostUp",
    "EpicUpgradeCost",
    "Power",
}
STANDARD_NUMBER_SUFFIXES = ["", "K", "M", "B", "T"]


def split_params(text: str) -> list[str]:
    if not text:
        return []
    params = []
    depth = 0
    start = 0
    for index, char in enumerate(text):
        if char in "(<[":
            depth += 1
        elif char in ")>]":
            depth -= 1
        elif char == "," and depth == 0:
            params.append(text[start:index].strip())
            start = index + 1
    params.append(text[start:].strip())
    return [p for p in params if p]


def pascal(name: str) -> str:
    if name.endswith("Offset"):
        name = name[:-6]
    if not name:
        return name
    return name[0].upper() + name[1:]


def parse_schema() -> tuple[dict, list[dict], list[dict]]:
    text = DUMP_CS.read_text(encoding="utf-8", errors="replace").splitlines()
    schemas: dict[str, dict] = {}
    root_settings: list[dict] = []
    root_vectors: list[dict] = []

    current: str | None = None
    vector_returns: dict[str, str] = {}
    in_root = False
    root_property_index = 0

    for line in text:
        stripped = line.strip()
        struct_match = STRUCT_RE.match(stripped)
        if struct_match:
            if current and current in schemas:
                schemas[current]["vector_returns"] = vector_returns
            current = struct_match.group("name")
            vector_returns = {}
            in_root = current == "Master_Content"
            continue

        if current:
            vector_match = VECTOR_METHOD_RE.search(stripped)
            if vector_match:
                vector_returns[vector_match.group("name")] = vector_match.group("return")

            create_match = CREATE_RE.search(stripped)
            if create_match:
                fields = []
                for field_index, param in enumerate(split_params(create_match.group("params") or "")):
                    param = param.split("=", 1)[0].strip()
                    parts = param.split()
                    if len(parts) < 2:
                        continue
                    kind, raw_name = parts[0], parts[-1]
                    field_name = pascal(raw_name)
                    field = {"name": field_name, "param": raw_name, "kind": kind, "index": field_index}
                    if kind == "VectorOffset":
                        field["element"] = vector_returns.get(field_name, "unknown")
                    fields.append(field)
                schemas[current] = {"name": current, "fields": fields, "vector_returns": dict(vector_returns)}

        if in_root:
            setting_match = ROOT_SETTING_RE.search(stripped)
            if setting_match:
                root_settings.append(
                    {
                        "name": setting_match.group("name"),
                        "type": setting_match.group("type"),
                        "index": root_property_index,
                    }
                )
                root_property_index += 1
                continue
            vector_match = ROOT_VECTOR_RE.search(stripped)
            if vector_match:
                name = vector_match.group("name")
                root_vectors.append(
                    {
                        "name": name,
                        "type": f"Master{name}",
                        "index": root_property_index,
                    }
                )
                root_property_index += 1

    if current and current in schemas:
        schemas[current]["vector_returns"] = vector_returns
    return schemas, root_settings, root_vectors


def enum_maps(enums: dict) -> dict[str, dict[int, str]]:
    return {
        name: {item["value"]: item["name"] for item in enum.get("values", [])}
        for name, enum in enums.items()
    }


def decode_value(reader: extract_wiki.FlatReader, table: int, field: dict, maps: dict) -> object:
    offset = reader.field(table, field["index"])
    if not offset:
        if field["kind"] in {"int", "double", "float"} or field["kind"].startswith("Master"):
            return 0
        if field["kind"] == "bool":
            return False
        if field["kind"] == "StringOffset":
            return ""
        if field["kind"] == "VectorOffset":
            return []
        return ""

    kind = field["kind"]
    if kind == "int":
        return reader.i32(offset)
    if kind == "double":
        return reader.f64(offset)
    if kind == "float":
        return reader.f32(offset)
    if kind == "bool":
        return bool(reader.u8(offset))
    if kind == "StringOffset":
        return reader.string(offset)
    if kind == "VectorOffset":
        element = field.get("element", "unknown")
        start, length = reader.vector(offset)
        values = []
        for index in range(length):
            if element == "string":
                values.append(reader.string(start + index * 4))
            elif element == "bool":
                values.append(bool(reader.data[start + index]))
            elif element == "double":
                values.append(reader.f64(start + index * 8))
            elif element == "float":
                values.append(reader.f32(start + index * 4))
            else:
                raw = reader.i32(start + index * 4)
                values.append(maps.get(element, {}).get(raw, raw))
        return values
    if kind in maps:
        raw = reader.i32(offset)
        return maps[kind].get(raw, raw)
    if kind.startswith("Master"):
        raw = reader.i32(offset)
        return maps.get(kind, {}).get(raw, raw)
    return ""


def localize_row(row: dict, texts: dict) -> None:
    key = row.get("Name") or row.get("NameKey") or row.get("Id")
    if not isinstance(key, str) or not key:
        return
    candidates = [key, f"name_{key}", f"title_{key}"]
    for candidate in candidates:
        if candidate in texts and texts[candidate].get("en"):
            row["English"] = texts[candidate]["en"]
            break
    for candidate in [f"desc_{key}", f"description_{key}"]:
        if candidate in texts and texts[candidate].get("en"):
            row["Description"] = texts[candidate]["en"]
            break


def humanize_weapon_key(key: str) -> str:
    if not isinstance(key, str):
        return ""
    clean = key
    for prefix in ("we_", "ui_we_"):
        if clean.startswith(prefix):
            clean = clean[len(prefix):]
            break
    return " ".join(part.capitalize() for part in clean.split("_") if part)


def alphabetic_suffix(index: int) -> str:
    letters = "abcdefghijklmnopqrstuvwxyz"
    text = ""
    while True:
        text = letters[index % 26] + text
        index = (index // 26) - 1
        if index < 0:
            break
    return text.rjust(2, "a")


def large_number_suffix(group: int) -> str:
    if group < len(STANDARD_NUMBER_SUFFIXES):
        return STANDARD_NUMBER_SUFFIXES[group]
    return alphabetic_suffix(group - len(STANDARD_NUMBER_SUFFIXES))


def trim_number(value: Decimal, decimals: int = 2) -> str:
    quantized = value.quantize(Decimal(1).scaleb(-decimals)).normalize()
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_large_number(value: object) -> object:
    if isinstance(value, bool) or value == "":
        return value
    if not isinstance(value, (str, int, float, Decimal)):
        return value
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return value
    if number.is_zero():
        return "0"

    sign = "-" if number < 0 else ""
    number = abs(number)
    if number < Decimal("1000"):
        return sign + trim_number(number)

    exponent = number.adjusted()
    group = int((Decimal(exponent) / Decimal(3)).to_integral_value(rounding=ROUND_FLOOR))
    scaled = number / (Decimal(10) ** (group * 3))
    if scaled >= Decimal("1000"):
        scaled /= Decimal(1000)
        group += 1
    return f"{sign}{trim_number(scaled)}{large_number_suffix(group)}"


def apply_weapon_presentation(rows: list[dict]) -> None:
    names_by_key = {
        row["Name"]: row.get("English") or humanize_weapon_key(row["Name"])
        for row in rows
        if isinstance(row.get("Name"), str) and row.get("Name")
    }

    for row in rows:
        key = row.get("Name")
        if not isinstance(key, str) or not key:
            continue

        display_name = names_by_key.get(key) or humanize_weapon_key(key)
        presented = {}
        for field_name, value in row.items():
            if field_name == "Name":
                presented["Name"] = display_name
                presented["Key"] = key
            elif field_name == "UnlockWeapons":
                presented["UnlockWeapons"] = [
                    names_by_key.get(unlock_key, humanize_weapon_key(unlock_key))
                    for unlock_key in value
                ]
                presented["UnlockWeaponKeys"] = value
            elif field_name in WEAPON_LARGE_NUMBER_FIELDS:
                presented[field_name] = format_large_number(value)
            elif field_name != "English":
                presented[field_name] = value

        row.clear()
        row.update(presented)


def flatten(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = []
    for row in rows:
        for key in row:
            if key not in headers:
                headers.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: flatten(value) for key, value in row.items()})


def decode_all() -> dict:
    enums, _, _, _ = extract_wiki.parse_dump()
    maps = enum_maps(enums)
    schemas, root_settings, root_vectors = parse_schema()
    master_assets = load_master_assets()
    reader = extract_wiki.FlatReader(master_assets["M"])
    texts = decode_texts(master_assets["MText"])
    root = reader.root()

    settings = {}
    for item in root_settings:
        schema = schemas.get(item["type"])
        offset = reader.field(root, item["index"])
        if not schema or not offset:
            continue
        table = reader.indirect(offset)
        row = {field["name"]: decode_value(reader, table, field, maps) for field in schema["fields"]}
        settings[item["name"]] = {"type": item["type"], "index": item["index"], "row": row}

    tables = {}
    for item in root_vectors:
        schema = schemas.get(item["type"])
        offset = reader.field(root, item["index"])
        if not schema or not offset:
            continue
        start, length = reader.vector(offset)
        rows = []
        for row_index in range(length):
            table = reader.indirect(start + row_index * 4)
            row = {field["name"]: decode_value(reader, table, field, maps) for field in schema["fields"]}
            localize_row(row, texts)
            if item["name"] == "Weapon" and isinstance(row.get("Name"), str):
                icon_path = f"assets/weapons/icons/ui_{row['Name']}.png"
                if (WIKI / icon_path).exists():
                    row["Icon"] = icon_path
            rows.append(row)
        if item["name"] == "Weapon":
            apply_weapon_presentation(rows)
        tables[item["name"]] = {
            "type": item["type"],
            "index": item["index"],
            "count": len(rows),
            "fields": schema["fields"],
            "rows": rows,
        }

    return {
        "settings": settings,
        "tables": tables,
        "texts": texts,
        "schema_count": len(schemas),
        "table_count": len(tables),
        "setting_count": len(settings),
    }


def load_master_assets() -> dict[str, bytes]:
    import UnityPy

    wanted = {"M", "MText"}
    found: dict[str, bytes] = {}
    for path in (ROOT / "$out" / "base_apk" / "assets" / "bin" / "Data").rglob("*"):
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
            name = getattr(data, "m_Name", "")
            if name not in wanted:
                continue
            script = getattr(data, "m_Script", None)
            if isinstance(script, str):
                found[name] = script.encode("utf-8", errors="surrogateescape")
            elif script is not None:
                found[name] = bytes(script)
        if wanted <= found.keys():
            return found
    missing = ", ".join(sorted(wanted - found.keys()))
    raise RuntimeError(f"Missing TextAsset(s): {missing}")


def decode_texts(raw: bytes) -> dict[str, dict]:
    reader = extract_wiki.FlatReader(raw)
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


def render_table(rows: list[dict], limit: int = 200, asset_prefix: str = "") -> str:
    if not rows:
        return "<p class=\"muted\">No rows decoded.</p>"
    headers = []
    for row in rows[:limit]:
        for key in row:
            if key not in headers:
                headers.append(key)
    cells = ["<div class=\"table-wrap\"><table><thead><tr>"]
    cells.extend(f"<th>{html.escape(key)}</th>" for key in headers)
    cells.append("</tr></thead><tbody>")
    for row in rows[:limit]:
        cells.append("<tr>")
        for key in headers:
            value = flatten(row.get(key, ""))
            if key == "Icon" and value:
                cells.append(f"<td><img class=\"icon\" src=\"{asset_prefix}{html.escape(value)}\" alt=\"\"></td>")
            else:
                cells.append(f"<td>{html.escape(value)}</td>")
        cells.append("</tr>")
    cells.append("</tbody></table></div>")
    if len(rows) > limit:
        cells.append(f"<p class=\"muted\">Showing first {limit} of {len(rows)} rows. Full data is in JSON/CSV.</p>")
    return "".join(cells)


def write_site(data: dict) -> None:
    if SITE.exists():
        shutil.rmtree(SITE)
    (SITE / "data" / "tables").mkdir(parents=True)
    (SITE / "tables").mkdir(parents=True)
    (SITE / ".nojekyll").write_text("", encoding="utf-8")
    icon_source = WIKI / "assets" / "weapons" / "icons"
    if icon_source.exists():
        shutil.copytree(icon_source, SITE / "assets" / "weapons" / "icons", dirs_exist_ok=True)

    for name, table in data["tables"].items():
        (SITE / "data" / "tables" / f"{name}.json").write_text(
            json.dumps(table, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        write_csv(SITE / "data" / "tables" / f"{name}.csv", table["rows"])

    settings_rows = [
        {"Name": name, "Type": item["type"], "Fields": len(item["row"])}
        for name, item in data["settings"].items()
    ]
    table_rows = [
        {"Name": name, "Type": item["type"], "Rows": item["count"], "Fields": len(item["fields"])}
        for name, item in sorted(data["tables"].items(), key=lambda pair: pair[0])
    ]
    (SITE / "data" / "all_master_data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    css = """
:root{color-scheme:light;--bg:#f6f7f9;--panel:#fff;--ink:#1e252c;--muted:#68717c;--line:#d8dee6;--accent:#166a6f;--accent2:#a53f2b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.shell{display:grid;grid-template-columns:280px 1fr;min-height:100vh}.side{background:#162127;color:#eef5f5;padding:24px;position:sticky;top:0;height:100vh;overflow:auto}
.side h1{font-size:22px;margin:0 0 6px}.side p{color:#b7c6ca;margin:0 0 18px}.nav a{display:block;color:#eef5f5;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.08)}
.main{padding:28px;max-width:1500px}.hero{margin-bottom:22px}.hero h2{font-size:34px;margin:0 0 8px}.hero p{color:var(--muted);margin:0;max-width:850px}
.stats{display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:12px;margin:20px 0}.stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px}.stat b{display:block;font-size:24px}
.section{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:18px;margin:18px 0}.section h3{margin:0 0 12px;font-size:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px}.card{display:block;background:#fff;border:1px solid var(--line);border-radius:8px;padding:12px;color:var(--ink)}.card b{display:block}.card span{color:var(--muted)}
.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:8px}table{width:100%;border-collapse:collapse;background:#fff}th,td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top;white-space:nowrap}th{position:sticky;top:0;background:#edf1f4;text-align:left}td{max-width:520px;overflow:hidden;text-overflow:ellipsis}.muted{color:var(--muted)}.icon{width:34px;height:34px;object-fit:contain;display:block}
.toolbar{display:flex;gap:10px;align-items:center;margin:12px 0}.search{width:min(420px,100%);padding:10px;border:1px solid var(--line);border-radius:6px}
@media(max-width:800px){.shell{display:block}.side{position:relative;height:auto}.main{padding:16px}.stats{grid-template-columns:1fr 1fr}}
"""
    (SITE / "styles.css").write_text(css.strip(), encoding="utf-8")

    nav_index = "".join(
        f"<a href=\"tables/{html.escape(name)}.html\">{html.escape(name)} <small>({table['count']})</small></a>"
        for name, table in sorted(data["tables"].items(), key=lambda pair: pair[0])
    )
    nav_table = "".join(
        f"<a href=\"{html.escape(name)}.html\">{html.escape(name)} <small>({table['count']})</small></a>"
        for name, table in sorted(data["tables"].items(), key=lambda pair: pair[0])
    )
    featured_names = ["Weapon", "Upgrade", "Card", "Rank", "Mission", "Reward", "Shop", "Product", "ModeScience", "Innovation", "DivineBeast", "Forge"]
    featured = "".join(
        f"<a class=\"card\" href=\"tables/{name}.html\"><b>{name}</b><span>{data['tables'][name]['count']} rows · {data['tables'][name]['type']}</span></a>"
        for name in featured_names
        if name in data["tables"]
    )
    index_html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Brick Inc Wiki</title><link rel="stylesheet" href="styles.css"></head>
<body><div class="shell"><aside class="side"><h1>Brick Inc Wiki</h1><p>Decoded from APK master data.</p><nav class="nav">{nav_index}</nav></aside>
<main class="main"><section class="hero"><h2>Brick Inc Master Data Wiki</h2><p>Generated from the local APK/XAPK. This site contains decoded FlatBuffer tables, settings, localization-backed names, CSV exports, and JSON data suitable for further wiki writing.</p></section>
<section class="stats"><div class="stat"><b>{data['table_count']}</b><span>decoded tables</span></div><div class="stat"><b>{sum(t['count'] for t in data['tables'].values())}</b><span>decoded rows</span></div><div class="stat"><b>{data['setting_count']}</b><span>settings tables</span></div><div class="stat"><b>{len(data['texts'])}</b><span>text rows</span></div></section>
<section class="section"><h3>Featured</h3><div class="grid">{featured}</div></section>
<section class="section"><h3>Decoded Tables</h3>{render_table(table_rows, 300)}</section>
<section class="section"><h3>Settings</h3>{render_table(settings_rows, 120)}</section></main></div></body></html>"""
    (SITE / "index.html").write_text(index_html, encoding="utf-8")

    for name, table in sorted(data["tables"].items(), key=lambda pair: pair[0]):
        rows = table["rows"]
        page_nav = f"<a href=\"../index.html\">Overview</a>{nav_table}"
        page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(name)} - Brick Inc Wiki</title><link rel="stylesheet" href="../styles.css"></head>
<body><div class="shell"><aside class="side"><h1>Brick Inc Wiki</h1><p>{html.escape(table['type'])}</p><nav class="nav">{page_nav}</nav></aside>
<main class="main"><section class="hero"><h2>{html.escape(name)}</h2><p>{table['count']} decoded rows from {html.escape(table['type'])}. Download: <a href="../data/tables/{html.escape(name)}.csv">CSV</a> · <a href="../data/tables/{html.escape(name)}.json">JSON</a></p></section>
<section class="section"><h3>Rows</h3><div class="toolbar"><input class="search" placeholder="Search this table" oninput="filterRows(this.value)"></div>{render_table(rows, 500, '../')}</section></main></div>
<script>function filterRows(q){{q=q.toLowerCase();document.querySelectorAll('tbody tr').forEach(r=>{{r.style.display=r.innerText.toLowerCase().includes(q)?'':'none'}})}}</script>
</body></html>"""
        (SITE / "tables" / f"{name}.html").write_text(page, encoding="utf-8")


def main() -> None:
    data = decode_all()
    write_site(data)
    if DOCS.exists():
        shutil.rmtree(DOCS)
    shutil.copytree(SITE, DOCS)
    (WIKI / "data" / "all_master_data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Decoded {data['table_count']} tables, {sum(t['count'] for t in data['tables'].values())} rows")
    print(f"Wrote {SITE}")
    print(f"Copied GitHub Pages site to {DOCS}")


if __name__ == "__main__":
    main()
