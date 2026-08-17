"""将用户下载的 Olist 数据导入标准数据层。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.olist_adapter import import_olist_demo


def main() -> None:
    parser = argparse.ArgumentParser(description="导入 Olist 真实公开电商数据")
    parser.add_argument("dataset_dir", help="解压后的 Olist CSV 目录")
    parser.add_argument("--tenant-id", default="olist-demo")
    parser.add_argument("--max-orders-per-period", type=int, default=5000)
    parser.add_argument("--output", default="olist_import_result.json")
    args = parser.parse_args()

    result = import_olist_demo(
        args.dataset_dir,
        tenant_id=args.tenant_id,
        max_orders_per_period=args.max_orders_per_period,
    )
    output_path = Path(args.output)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n导入清单已写入: {output_path.resolve()}")


if __name__ == "__main__":
    main()
