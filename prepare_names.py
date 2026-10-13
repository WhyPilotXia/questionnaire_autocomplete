"""Extract only resume.name from local source JSON; never copy other profile fields."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def extract_names(source: Path) -> list[str]:
    names: set[str] = set()
    files = sorted(source.glob("*.json"))
    if not files:
        raise ValueError("姓名源目录中没有 JSON 文件")
    for path in files:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        data = value.get("data") if isinstance(value, dict) else None
        if not isinstance(data, dict):
            raise ValueError(f"姓名源格式不符：{path.name}")
        for section in ("campusResumes", "socialResumes"):
            records = data.get(section) or []
            if not isinstance(records, list):
                raise ValueError(f"姓名源列表格式不符：{path.name}")
            for record in records:
                resume = record.get("resume", {}) if isinstance(record, dict) else {}
                name = resume.get("name") if isinstance(resume, dict) else None
                if isinstance(name, str) and name.strip():
                    names.add(name.strip())
    if not names:
        raise ValueError("未发现 resume.name 姓名字段")
    return sorted(names)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "name-source")
    parser.add_argument("--output", type=Path, default=ROOT / "names.json")
    args = parser.parse_args()
    names = extract_names(args.source)
    args.output.write_text(json.dumps(names, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已提取 {len(names)} 个去重姓名 -> {args.output.name}（仅姓名，无其他资料）")


if __name__ == "__main__":
    main()
