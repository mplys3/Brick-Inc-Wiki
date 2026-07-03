from __future__ import annotations

import sys
from pathlib import Path

import UnityPy


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "$out" / "base_apk" / "assets"


def interesting(value: str) -> bool:
    v = value.lower()
    return any(
        token in v
        for token in [
            "master",
            "weapon",
            "m.root",
            "m_root",
            "content",
            "table",
            "balance",
            "localization",
            "string",
        ]
    )


def main() -> int:
    paths = list((ASSETS / "aa").rglob("*")) + list((ASSETS / "bin" / "Data").rglob("*"))
    hits = []
    for idx, path in enumerate(paths, 1):
        if not path.is_file():
            continue
        if path.stat().st_size == 0:
            continue
        try:
            env = UnityPy.load(str(path))
        except Exception:
            continue
        for obj in env.objects:
            try:
                typ = obj.type.name
                data = obj.read()
                name = getattr(data, "name", "") or ""
                if typ in {"TextAsset", "MonoBehaviour", "AssetBundle"} or interesting(name):
                    if interesting(name) or typ == "TextAsset":
                        hits.append(
                            {
                                "file": str(path.relative_to(ROOT)),
                                "type": typ,
                                "name": name,
                                "size": path.stat().st_size,
                            }
                        )
                        print(f"{len(hits):04d} {typ:18} {name:60} {path.relative_to(ROOT)}")
            except Exception:
                continue
    print(f"hits={len(hits)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
