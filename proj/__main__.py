from pathlib import Path
from typing import (
    Any,
    Sequence,
    Union,
    TextIO,
)
import subprocess
import os
import shutil
import multiprocessing
import shlex
import sys
from .config_file import (
    get_config,
    get_config_root,
)
from .dtgen import run_dtgen
from .format import run_formatter
from .lint import run_linter
from .dtgen.find_outdated import find_outdated
import proj.fix_compile_commands as fix_compile_commands
import logging
from dataclasses import dataclass
from .verbosity import (
    add_verbosity_args,
    calculate_log_level,
)

_l = logging.getLogger(name='proj')

DIR = Path(__file__).resolve().parent

@dataclass(frozen=True)
class MainRootArgs:
    path: Path
    verbosity: int

def main_root(args: MainRootArgs) -> None:
    config_root = get_config_root(args.path)
    print(config_root)

def xdg_open(path: Path):
    subprocess_check_call(
        ['xdg-open', str(path)],
        stderr=sys.stdout,
        env=os.environ,
    )

def subprocess_check_call(command, **kwargs):
    if kwargs.get("shell", False):
        pretty_cmd = " ".join(command)
        _l.info(f"+++ $ {pretty_cmd}")
        subprocess.check_call(pretty_cmd, **kwargs)
    else:
        pretty_cmd = shlex.join(command)
        _l.info(f"+++ $ {pretty_cmd}")
        subprocess.check_call(command, **kwargs)


def subprocess_run(command, **kwargs):
    if kwargs.get("shell", False):
        pretty_cmd = " ".join(command)
        _l.info(f"+++ $ {pretty_cmd}")
        subprocess.check_call(pretty_cmd, **kwargs)
    else:
        pretty_cmd = shlex.join(command)
        _l.info(f"+++ $ {pretty_cmd}")
        subprocess.check_call(command, **kwargs)

def cmake(cmake_args, config, is_coverage):
    if is_coverage:
        cwd = config.cov_dir
    else:
        cwd = config.build_dir
    subprocess_check_call(
        [
            "cmake",
            *cmake_args,
            "../..",
        ],
        stderr=sys.stdout,
        cwd=cwd,
        env=os.environ,
        shell=config.cmake_require_shell,
    )

@dataclass(frozen=True)
class MainCmakeArgs:
    path: Path
    fast: bool
    trace: bool
    dtgen_skip: bool
    verbosity: int

def main_cmake(args: MainCmakeArgs) -> None:
    if not args.dtgen_skip:
        main_dtgen(args=MainDtgenArgs(
            path=args.path,
            files=[],
            no_delete_outdated=False,
            force=False,
            verbosity=args.verbosity,
        ))

    config = get_config(args.path)
    if not args.fast:
        if config.build_dir.exists():
            shutil.rmtree(config.build_dir)
        if config.cov_dir.exists():
            shutil.rmtree(config.cov_dir)
    config.build_dir.mkdir(exist_ok=True, parents=True)
    config.cov_dir.mkdir(exist_ok=True, parents=True)
    cmake_args = [f"-D{k}={v}" for k, v in config.cmake_flags.items()]
    cmake_args += shlex.split(os.environ.get("CMAKE_FLAGS", ""))
    if args.trace:
        cmake_args += ["--trace", "--trace-expand", "--trace-redirect=trace.log"]
    cmake(cmake_args, config, False)
    COMPILE_COMMANDS_FNAME = "compile_commands.json"
    if config.fix_compile_commands:
        fix_compile_commands.fix_file(
            compile_commands=config.build_dir / COMPILE_COMMANDS_FNAME,
            base_dir=config.base,
        )

    with (config.base / COMPILE_COMMANDS_FNAME).open("w") as f:
        subprocess_check_call(
            [
                "compdb",
                "-p",
                ".",
                "list",
            ],
            stdout=f,
            cwd=config.build_dir,
            env=os.environ,
        )
        
    cmake(cmake_args + ["-DFF_USE_CODE_COVERAGE=ON"], config, True)


@dataclass(frozen=True)
class MainBuildArgs:
    path: Path
    verbosity: int
    jobs: int
    dtgen_skip: bool

def main_build(args: MainBuildArgs) -> None:
    if not args.dtgen_skip:
        main_dtgen(args=MainDtgenArgs(
            path=args.path,
            files=[],
            no_delete_outdated=False,
            force=False,
            verbosity=args.verbosity,
        ))

    config = get_config(args.path)
    subprocess_check_call(
        [
            "make",
            "-j",
            str(args.jobs),
            *config.build_targets,
        ],
        env={
            **os.environ,
            "CCACHE_BASEDIR": config.base,
            **({"VERBOSE": "1"} if args.verbosity <= logging.DEBUG else {}),
        },
        stderr=sys.stdout,
        cwd=config.build_dir,
    )


@dataclass(frozen=True)
class MainTestArgs:
    path: Path
    coverage: bool
    verbosity: int
    jobs: int
    dtgen_force: bool
    dtgen_skip: bool
    browser: bool
    skip_gpu_tests: bool

def main_test(args: MainTestArgs) -> None:
    if not args.dtgen_skip:
        main_dtgen(args=MainDtgenArgs(
            path=args.path,
            files=[],
            no_delete_outdated=False,
            force=args.dtgen_force,
            verbosity=args.verbosity,
        ))

    config = get_config(args.path)
    if args.coverage:
        cwd = config.cov_dir
    else:
        cwd = config.build_dir

    # Currently hardcode GPU tests as 'kernels-tests'
    gpu_test_targets = ["kernels-tests"]
    cpu_test_targets = [target for target in config.test_targets if target not in gpu_test_targets]

    # check if GPU is available
    gpu_available = False
    try:
        result = subprocess.run(['nvidia-smi'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode == 0:
            gpu_available = True
    except FileNotFoundError:
        pass
    gpu_available = gpu_available and not args.skip_gpu_tests
    
    # Build targets
    test_targets = cpu_test_targets + gpu_test_targets if gpu_available else cpu_test_targets
    print("============ Building tests ============")
    subprocess_check_call(
        [
            "make",
            "-j",
            str(args.jobs),
            *test_targets,
        ],
        env={
            **os.environ,
            "CCACHE_BASEDIR": config.base,
            # "CCACHE_NOHASHDIR": "1",
            **({"VERBOSE": "1"} if args.verbosity <= logging.DEBUG else {}),
        },
        stderr=sys.stdout,
        cwd=cwd,
    )
    
    if args.skip_gpu_tests:
        print("\033[33mGPU tests are set to be skipped\033[0m")
        print(f"skipped targets: {gpu_test_targets}")
    elif not gpu_available:
        print("\033[31mError: GPU driver not found or failed to load. Skipping GPU tests.\033[0m")
        print(f"skipped targets: {gpu_test_targets}")

    # CPU tests
    print("============ Running tests ============")
    target_regex = "^(" + "|".join(test_targets) + ")$"
    subprocess_run(
        [
            "ctest",
            "--progress",
            "--output-on-failure",
            "-L",
            target_regex,
        ],
        stderr=sys.stdout,
        cwd=cwd,
        env=os.environ,
    )
    

    if args.coverage:
        subprocess_run(
            [
                "lcov",
                "--capture",
                "--directory",
                ".",
                "--output-file",
                "main_coverage.info",
            ],
            stderr=sys.stdout,
            cwd=cwd,
            env=os.environ,
        )
        
        # only keep the coverage info of the lib directory
        subprocess_run(
            [
                "lcov", 
                "--extract",
                "main_coverage.info",
                f"{config.base}/lib/*",
                "--output-file",
                "main_coverage.info",
            ],
            stderr=sys.stdout,
            cwd=cwd,
            env=os.environ,
        )
        
        # filter out dtg.h and .dtg.cc
        subprocess_run(
            [
                "lcov",
                "--remove",
                "main_coverage.info",
                f"{config.base}/lib/*.dtg.h",
                f"{config.base}/lib/*.dtg.cc",
                "--output-file",
                "main_coverage.info",
            ],
            stderr=sys.stdout,
            cwd=cwd,
            env=os.environ,
        )
        
        if args.browser:
            print("opening coverage info in browser")
            subprocess_run(
                [
                    "genhtml",
                    "main_coverage.info",
                    "--output-directory",
                    "code_coverage",
                ],
                stderr=sys.stdout,
                cwd=config.build_dir,
                env=os.environ,
            )

            # run xdg-open to open the browser
            # not able to test it now as I am running on remote linux
            subprocess_run(
                [
                    "xdg-open",
                    "code_coverage/index.html",
                ],
                stderr=sys.stdout,
                cwd=config.cov_dir,
                env=os.environ,
            )
        else:
            subprocess_run(
                [
                    "lcov",
                    "--list",
                    "main_coverage.info",
                ],
                stderr=sys.stdout,
                cwd=config.cov_dir,
                env=os.environ,
            )
    


@dataclass(frozen=True)
class MainLintArgs:
    path: Path
    files: Sequence[Path]
    profile_checks: bool
    verbosity: int

def main_lint(args: MainLintArgs) -> None:
    root = get_config_root(args.path)
    config = get_config(args.path)
    if len(args.files) == 0:
        files = None
    else:
        for file in args.files:
            assert file.is_file()
        files = list(args.files)
    run_linter(root, config, files, profile_checks=args.profile_checks)

@dataclass(frozen=True)
class MainFormatArgs:
    path: Path
    files: Sequence[Path]
    verbosity: int

def main_format(args: Any) -> None:
    root = get_config_root(args.path)
    config = get_config(args.path)
    if len(args.files) == 0:
        files = None
    else:
        for file in args.files:
            assert file.is_file()
        files = list(args.files)
    run_formatter(root, config, files)

@dataclass(frozen=True)
class MainDtgenArgs:
    path: Path
    files: Sequence[Path]
    no_delete_outdated: bool
    force: bool
    verbosity: int

def main_dtgen(args: MainDtgenArgs) -> None:
    root = get_config_root(args.path)
    config = get_config(args.path)
    if len(args.files) == 0:
        files = None
    else:
        for file in args.files:
            assert file.is_file()
        files = list(args.files)
    run_dtgen(
        root=root,
        config=config,
        files=files,
        force=args.force,
    )
    for outdated in find_outdated(root, config):
        if args.no_delete_outdated:
            _l.warning(f'Possible out-of-date file at {outdated}')
        else:
            _l.info(f'Removing out-of-date file at {outdated}')
            outdated.unlink()

@dataclass(frozen=True)
class MainDoxygenArgs:
    path: Path
    browser: bool
    verbosity: int

def main_doxygen(args: MainDoxygenArgs) -> None:
    root = get_config_root(args.path)
    config = get_config(args.path)

    env = {
        **os.environ,
        'FF_HOME': root,
    }
    stderr: Union[int, TextIO] = sys.stderr
    stdout: Union[int, TextIO] = sys.stdout

    if args.verbosity > logging.INFO:
        env['DOXYGEN_QUIET'] = 'YES'
    if args.verbosity > logging.WARN:
        env['DOXYGEN_WARNINGS'] = 'NO'
    if args.verbosity > logging.CRITICAL:
        stderr = subprocess.DEVNULL
        stdout = subprocess.DEVNULL

    config.doxygen_dir.mkdir(exist_ok=True, parents=True)
    subprocess_check_call(
        ['doxygen', 'docs/doxygen/Doxyfile'],
        env=env,
        stdout=stdout,
        stderr=stderr,
        cwd=root,
    )

    if args.browser:
        xdg_open(config.doxygen_dir / 'html/index.html') 


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    subparsers = p.add_subparsers()

    def set_main_signature(parser, func, args_type):
        def _f(args: argparse.Namespace, func=func, args_type=args_type):
            func(args_type(**{k: v for k, v in vars(args).items() if k != 'func'}))
        parser.set_defaults(func=_f)

    root_p = subparsers.add_parser("root")
    set_main_signature(root_p, main_root, MainRootArgs)
    root_p.set_defaults(func=main_root)
    root_p.add_argument("--path", "-p", type=Path, default=Path.cwd())
    add_verbosity_args(root_p)

    test_p = subparsers.add_parser("test")
    set_main_signature(test_p, main_test, MainTestArgs)
    test_p.set_defaults(func=main_test)
    test_p.add_argument("--path", "-p", type=Path, default=Path.cwd())
    test_p.add_argument("--jobs", "-j", type=int, default=multiprocessing.cpu_count())
    test_p.add_argument("--coverage", "-c", action="store_true")   
    test_p.add_argument("--dtgen-force", action="store_true")   
    test_p.add_argument("--dtgen-skip", action="store_true")
    test_p.add_argument(
        "--browser", "-b", action="store_true", help="open coverage info in browser"
    )
    test_p.add_argument("--skip-gpu-tests", action="store_true")
    add_verbosity_args(test_p)

    build_p = subparsers.add_parser("build")
    set_main_signature(build_p, main_build, MainBuildArgs)
    build_p.set_defaults(func=main_build)
    build_p.add_argument("--path", "-p", type=Path, default=Path.cwd())
    build_p.add_argument("--jobs", "-j", type=int, default=multiprocessing.cpu_count())
    build_p.add_argument("--dtgen-skip", action="store_true")
    add_verbosity_args(build_p)

    cmake_p = subparsers.add_parser("cmake")
    set_main_signature(cmake_p, main_cmake, MainCmakeArgs)
    cmake_p.add_argument("--path", "-p", type=Path, default=Path.cwd())
    cmake_p.add_argument("--fast", action="store_true")
    cmake_p.add_argument("--trace", action="store_true")
    cmake_p.add_argument("--dtgen-skip", action="store_true")
    add_verbosity_args(cmake_p)

    dtgen_p = subparsers.add_parser('dtgen')
    dtgen_p.set_defaults(func=main_dtgen)
    dtgen_p.add_argument('--path', '-p', type=Path, default=Path.cwd())
    dtgen_p.add_argument('--force', action='store_true', help='Disable incremental toml->c++ generation')
    dtgen_p.add_argument('--no-delete-outdated', action='store_true')
    dtgen_p.add_argument('files', nargs='*', type=Path)
    add_verbosity_args(dtgen_p)

    format_p = subparsers.add_parser('format')
    set_main_signature(format_p, main_format, MainFormatArgs)
    format_p.add_argument('--path', '-p', type=Path, default=Path.cwd())
    format_p.add_argument('files', nargs='*', type=Path)
    add_verbosity_args(format_p)

    lint_p = subparsers.add_parser('lint')
    set_main_signature(lint_p, main_lint, MainLintArgs)
    lint_p.add_argument('--path', '-p', type=Path, default=Path.cwd())
    lint_p.add_argument('--profile-checks', action='store_true')
    lint_p.add_argument('files', nargs='*', type=Path)
    add_verbosity_args(lint_p)

    doxygen_p = subparsers.add_parser('doxygen')
    set_main_signature(doxygen_p, main_doxygen, MainDoxygenArgs)
    doxygen_p.add_argument('--path', '-p', type=Path, default=Path.cwd())
    doxygen_p.add_argument(
        "--browser", "-b", action="store_true", help="open generated documentation in browser"
    )
    add_verbosity_args(doxygen_p)

    args = p.parse_args()

    logging.basicConfig(
        level=calculate_log_level(args),
    )

    if hasattr(args, "func") and args.func is not None:
        args.func(args)
    else:
        p.print_help()
        exit(1)


if __name__ == "__main__":
    main()
