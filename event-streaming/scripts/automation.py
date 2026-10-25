import argparse
from importlib import import_module
from pathlib import Path

cpu = import_module(".automation-cpu", package=__package__)
mps = import_module(".automation-mps", package=__package__)

STREAMING_DIR = cpu.STREAMING_DIR
ENVIRONMENTS = ("cpu", "mps")
SUITES = ("cpu", "mps", "all", "non-ocr", "ocr", "text")
CASES = tuple((backend, file_type) for backend in ENVIRONMENTS for file_type in cpu.DATASET_PATHS)


def run_case(*, environment: str, file_type: str, results_root: Path) -> list[str]:
    automation_class = {"cpu": cpu.StreamingAutomation, "mps": mps.StreamingAutomation}[environment]
    return automation_class().run(file_type=file_type, results_root=results_root)


def run_suite(*, suite: str, results_root: Path) -> Path:
    if suite not in SUITES:
        raise ValueError(f"Unknown suite {suite!r}; choose from {', '.join(SUITES)}")
    cases = [(backend, file_type) for backend, file_type in CASES if suite in ("all", backend, file_type)]
    for file_type in dict.fromkeys(file_type for _, file_type in cases):
        cpu.get_dataset_folders(file_type=file_type)

    suite_dir = cpu.create_results_directory(root=results_root, name=f"suite_{suite}")
    print(f"Suite: {suite}; {len(cases)} cases; reports: {suite_dir}", flush=True)
    for index, (backend, file_type) in enumerate(cases, start=1):
        print(f"\n=== CASE {index}/{len(cases)}: {backend.upper()} / {file_type} ===", flush=True)
        run_case(environment=backend, file_type=file_type, results_root=suite_dir)
        print(f"PASSED: {backend}/{file_type}", flush=True)

    print(f"\nSuite finished: {len(cases)} passed; reports: {suite_dir}", flush=True)
    return suite_dir


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--environment", choices=ENVIRONMENTS)
    mode.add_argument(
        "--suite", choices=SUITES,
        help="cpu/mps: all file types; all: all six cases; ocr/non-ocr/text: both backends for that file type.",
    )
    parser.add_argument("--type", dest="file_type", choices=tuple(cpu.DATASET_PATHS))
    parser.add_argument(
        "--results-root", type=Path, default=STREAMING_DIR / "results",
        help="Parent folder for timestamped results.",
    )
    args = parser.parse_args()
    if args.environment is not None:
        if args.file_type is None:
            parser.error("--type is required with --environment")
        run_case(environment=args.environment, file_type=args.file_type, results_root=args.results_root)
    else:
        if args.file_type is not None:
            parser.error("--type cannot be combined with --suite")
        run_suite(suite=args.suite, results_root=args.results_root)


if __name__ == "__main__":
    main()
