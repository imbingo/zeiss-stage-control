"""Generate the adapter's stubs from a local ZEISS 2026.11.1 proto tree."""

import argparse
from pathlib import Path
import re

import grpc_tools
from grpc_tools import protoc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="proto_2026.11.1 directory")
    parser.add_argument("--output", type=Path, default=Path("gateway_stubs"))
    args = parser.parse_args()
    pending = [
        "zen_api/hardware/v1/stage_service.proto",
        "zen_api/hardware/v1/simple_stage_service.proto",
        "zen_api/acquisition/v1beta/experiment_service.proto",
        "zen_api/lm/acquisition/v1/experiment_sw_autofocus_service.proto",
        "zen_api/lm/hardware/v2/focus_service.proto",
        "zen_api/lm/hardware/v2/objective_changer_service.proto",
        "zen_api/lm/hardware/v2/optovar_service.proto",
        "zen_api/acquisition/v1beta/experiment_streaming_service.proto",
        "zen_api/lm/acquisition/v1/zstack_service.proto",
        "zen_api/lm/hardware/v2/camera_service.proto",
    ]
    sources = set()
    while pending:
        name = pending.pop()
        if name in sources:
            continue
        try:
            source = (args.source / name).read_text(encoding="utf-8-sig")
        except OSError as exc:
            parser.error(f"Cannot read {name}: {exc}")
        sources.add(name)
        pending.extend(re.findall(r'^\s*import "(zen_api/[^"]+)";', source, re.MULTILINE))

    args.output.mkdir(parents=True, exist_ok=True)
    includes = Path(grpc_tools.__file__).parent / "_proto"
    return protoc.main([
        "protoc", f"-I{args.source}", f"-I{includes}",
        f"--python_out={args.output}", f"--grpc_python_out={args.output}", *sorted(sources),
    ])


if __name__ == "__main__":
    raise SystemExit(main())
