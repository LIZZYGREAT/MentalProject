#!/usr/bin/env python3

from __future__ import annotations

import re
import shutil
from pathlib import Path


# ============================================================
# 路径
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

ENV_EXAMPLE = PROJECT_DIR / ".env.example"
ENV_FILE = PROJECT_DIR / ".env"
ENV_BACKUP = PROJECT_DIR / ".env.bak"


ENV_PATTERN = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?)"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<separator>\s*=\s*)"
    r"(?P<value>.*)$"
)


def parse_env_assignment(line: str):
    """
    解析：
        KEY=value
        export KEY=value

    注释和空行返回 None。
    """
    stripped = line.rstrip("\r\n")

    if not stripped.strip():
        return None

    if stripped.lstrip().startswith("#"):
        return None

    match = ENV_PATTERN.match(stripped)

    if not match:
        return None

    return match.groupdict()


def normalize_value(value: str) -> str:
    return value.strip()


def is_non_empty_value(value: str) -> bool:
    """
    以下视为空：

        KEY=
        KEY=""
        KEY=''
        KEY=   # TODO

    其他情况视为已有配置。
    """
    value = normalize_value(value)

    if not value:
        return False

    if value.startswith("#"):
        return False

    if value in ('""', "''"):
        return False

    return True


def load_env(path: Path):
    """
    返回：

        values:
            {
                "KEY": {
                    "prefix": "",
                    "key": "KEY",
                    "separator": "=",
                    "value": "xxx",
                }
            }

        order:
            ["KEY1", "KEY2", ...]
    """
    values = {}
    order = []

    if not path.exists():
        return values, order

    text = path.read_text(encoding="utf-8-sig")

    for line in text.splitlines():
        parsed = parse_env_assignment(line)

        if not parsed:
            continue

        key = parsed["key"]

        if key not in values:
            order.append(key)

        # 如果出现重复字段，以最后一次定义为准
        values[key] = parsed

    return values, order


def mask_value(key: str, value: str) -> str:
    """
    输出差异报告时避免直接把 Secret / Password / Token 打印出来。
    """
    value = value.strip()

    sensitive_words = (
        "PASSWORD",
        "PASSWD",
        "SECRET",
        "TOKEN",
        "API_KEY",
        "APIKEY",
        "PRIVATE_KEY",
        "ACCESS_KEY",
    )

    key_upper = key.upper()

    if any(word in key_upper for word in sensitive_words):
        if not value:
            return "<empty>"

        return "<configured>"

    if not value:
        return "<empty>"

    return value


def main():
    # ========================================================
    # 前置检查
    # ========================================================

    if not ENV_EXAMPLE.exists():
        raise FileNotFoundError(
            f"找不到模板文件：{ENV_EXAMPLE}"
        )

    existing_values, existing_order = load_env(ENV_FILE)

    example_text = ENV_EXAMPLE.read_text(
        encoding="utf-8-sig"
    )

    example_lines = example_text.splitlines()

    # ========================================================
    # 差异统计
    # ========================================================

    added_keys = []
    preserved_different_keys = []
    filled_from_example_keys = []
    same_keys = []
    extra_keys = []

    example_keys = set()

    # 最终 key -> value
    final_values = {}

    output_lines = []

    # ========================================================
    # 备份旧 .env
    # ========================================================

    if ENV_FILE.exists():
        shutil.copy2(
            ENV_FILE,
            ENV_BACKUP,
        )

        print(
            f"[backup] {ENV_FILE.name} -> "
            f"{ENV_BACKUP.name}"
        )

    # ========================================================
    # 按 .env.example 结构生成新的 .env
    # ========================================================

    for line in example_lines:
        parsed_example = parse_env_assignment(line)

        # 注释 / 空行原样保留
        if not parsed_example:
            output_lines.append(line)
            continue

        key = parsed_example["key"]
        example_value = parsed_example["value"]

        example_keys.add(key)

        existing = existing_values.get(key)

        # ----------------------------------------------------
        # 情况 1：
        # .env 完全不存在这个字段
        # ----------------------------------------------------
        if existing is None:
            added_keys.append(key)

            final_value = example_value

            output_lines.append(line)
            final_values[key] = final_value

            continue

        existing_value = existing["value"]

        # ----------------------------------------------------
        # 情况 2：
        # .env 已有有效值
        # -> 始终保留 .env
        # ----------------------------------------------------
        if is_non_empty_value(existing_value):
            final_value = existing_value

            output_lines.append(
                f"{parsed_example['prefix']}"
                f"{key}"
                f"{parsed_example['separator']}"
                f"{existing_value}"
            )

            final_values[key] = final_value

            if (
                normalize_value(existing_value)
                != normalize_value(example_value)
            ):
                preserved_different_keys.append(
                    (
                        key,
                        existing_value,
                        example_value,
                    )
                )
            else:
                same_keys.append(key)

            continue

        # ----------------------------------------------------
        # 情况 3：
        # .env 中存在，但为空
        # -> 使用 .env.example
        # ----------------------------------------------------
        final_value = example_value

        output_lines.append(line)
        final_values[key] = final_value

        if is_non_empty_value(example_value):
            filled_from_example_keys.append(
                (
                    key,
                    example_value,
                )
            )

    # ========================================================
    # 找出只存在于旧 .env 的字段
    # ========================================================

    extra_keys = [
        key
        for key in existing_order
        if key not in example_keys
    ]

    if extra_keys:
        # 清理末尾多余空行
        while output_lines and not output_lines[-1].strip():
            output_lines.pop()

        output_lines.extend(
            [
                "",
                "",
                "# ============================================================",
                "# Variables preserved from existing .env",
                "# These variables are not defined in .env.example",
                "# ============================================================",
            ]
        )

        for key in extra_keys:
            item = existing_values[key]

            output_lines.append(
                f"{item['prefix']}"
                f"{key}"
                f"{item['separator']}"
                f"{item['value']}"
            )

            final_values[key] = item["value"]

    # ========================================================
    # 检查最终仍为空的字段
    # ========================================================

    missing_value_keys = [
        key
        for key, value in final_values.items()
        if not is_non_empty_value(value)
    ]

    # ========================================================
    # 写入 .env
    # ========================================================

    final_text = "\n".join(output_lines).rstrip() + "\n"

    with ENV_FILE.open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as f:
        f.write(final_text)

    # ========================================================
    # 输出同步结果
    # ========================================================

    print()
    print("=" * 70)
    print("ENV SYNC RESULT")
    print("=" * 70)

    print(f"[done] updated: {ENV_FILE}")
    print(f"[info] template fields: {len(example_keys)}")
    print(f"[info] existing fields: {len(existing_values)}")
    print(f"[info] final fields: {len(final_values)}")

    # ========================================================
    # 新增字段
    # ========================================================

    print()
    print("-" * 70)
    print(
        f"[1] Fields added from .env.example "
        f"({len(added_keys)})"
    )
    print("-" * 70)

    if added_keys:
        for key in added_keys:
            value = final_values.get(key, "")

            print(
                f"  + {key}="
                f"{mask_value(key, value)}"
            )
    else:
        print("  None")

    # ========================================================
    # 值不同但保留 .env
    # ========================================================

    print()
    print("-" * 70)
    print(
        f"[2] Different values preserved from .env "
        f"({len(preserved_different_keys)})"
    )
    print("-" * 70)

    if preserved_different_keys:
        for (
            key,
            env_value,
            example_value,
        ) in preserved_different_keys:

            print(f"  * {key}")
            print(
                "      .env         = "
                f"{mask_value(key, env_value)}"
            )
            print(
                "      .env.example = "
                f"{mask_value(key, example_value)}"
            )
            print(
                "      result       = keep .env"
            )
    else:
        print("  None")

    # ========================================================
    # 原 .env 为空，用 example 填充
    # ========================================================

    print()
    print("-" * 70)
    print(
        f"[3] Empty .env values filled from "
        f".env.example "
        f"({len(filled_from_example_keys)})"
    )
    print("-" * 70)

    if filled_from_example_keys:
        for key, value in filled_from_example_keys:
            print(
                f"  > {key}="
                f"{mask_value(key, value)}"
            )
    else:
        print("  None")

    # ========================================================
    # .env 独有字段
    # ========================================================

    print()
    print("-" * 70)
    print(
        f"[4] Extra fields preserved from existing .env "
        f"({len(extra_keys)})"
    )
    print("-" * 70)

    if extra_keys:
        for key in extra_keys:
            value = final_values.get(key, "")

            print(
                f"  ! {key}="
                f"{mask_value(key, value)}"
            )

        print()
        print(
            "  These fields were moved to the end "
            "of .env."
        )
    else:
        print("  None")

    # ========================================================
    # 最重要：仍需人工填写
    # ========================================================

    print()
    print("=" * 70)
    print(
        f"[5] Fields still missing values "
        f"({len(missing_value_keys)})"
    )
    print("=" * 70)

    if missing_value_keys:
        for key in missing_value_keys:
            print(f"  [MISSING] {key}")

        print()
        print(
            "WARNING: The fields above are still empty "
            "in the final .env."
        )
        print(
            "Please check whether they need to be "
            "configured before starting the service."
        )
    else:
        print("  No empty environment variables found.")

    print()
    print("=" * 70)
    print("SYNC COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()