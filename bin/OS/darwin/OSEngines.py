"""macOS engine catalogue.

Code/__init__.py puts OS/<platform> on sys.path, so this is the module the rest
of the program imports as `OSEngines`.

The engine metadata (names, authors, elo, UCI defaults, the Maia weight setup)
is identical on every platform, so instead of duplicating the ~500 line
catalogue we reuse OS/linux/OSEngines.py and change only what is
platform-specific:

* engines are looked up in OS/darwin/Engines, which holds the macOS binaries
  produced by build_engines.py;
* only engines whose binary actually exists are registered - not every engine
  bundled for Linux can be compiled for macOS, and the upstream catalogue
  assumes all of them are present.
"""

import contextlib
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_LINUX_CATALOGUE = os.path.join(os.path.dirname(_HERE), "linux", "OSEngines.py")


def _load_linux_catalogue():
    spec = importlib.util.spec_from_file_location("OSEnginesLinuxCatalogue", _LINUX_CATALOGUE)
    module = importlib.util.module_from_spec(spec)
    # Registering it keeps a single instance around across reset() calls.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_catalogue = _load_linux_catalogue()


@contextlib.contextmanager
def _tolerate_missing_engines():
    """The upstream catalogue chmods every engine as it is registered, which
    raises FileNotFoundError for the engines we have no macOS build of. Neutralise
    the permission handling while the catalogue is read; build_engines.py already
    installs the binaries executable, and EngineRun re-checks the exec bit before
    launching one."""
    real_access, real_chmod, real_stat = os.access, os.chmod, os.stat

    def access(path, mode, *args, **kwargs):
        if mode == os.X_OK:
            return True
        return real_access(path, mode, *args, **kwargs)

    os.access = access
    os.chmod = lambda *args, **kwargs: None
    try:
        yield
    finally:
        os.access, os.chmod, os.stat = real_access, real_chmod, real_stat


def _is_available(engine) -> bool:
    path_exe = getattr(engine, "path_exe", None)
    return bool(path_exe) and os.path.isfile(path_exe)


def read_engines(folder_engines):
    with _tolerate_missing_engines():
        dic_engines = _catalogue.read_engines(folder_engines)

    available = {key: engine for key, engine in dic_engines.items() if _is_available(engine)}

    for engine in available.values():
        path_exe = engine.path_exe
        if not os.access(path_exe, os.X_OK):
            import stat

            os.chmod(path_exe, os.stat(path_exe).st_mode | stat.S_IXUSR)

    return available


def li_engines_fixed_elo() -> tuple:
    """Same fixed-elo engines as on Linux, minus the ones we cannot run here."""
    import Code

    folder_engines = Code.folder_engines
    with _tolerate_missing_engines():
        dic_engines = _catalogue.read_engines(folder_engines)

    return tuple(
        item
        for item in _catalogue.li_engines_fixed_elo()
        if item[0] in dic_engines and _is_available(dic_engines[item[0]])
    )
