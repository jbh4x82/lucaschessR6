"""Build the FasterCode extension on macOS (clang, arm64 or x86_64).

Mirrors setup_windows.py but links against the libirina.a produced by
fastercode_macos.sh, which sits one level up in _fastercode/.
"""

import os

from setuptools import Extension, setup
from Cython.Build import cythonize

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IRINA_LIB_DIR = os.path.dirname(BASE_DIR)  # _fastercode/, where libirina.a is moved to

extensions = [
    Extension(
        name="FasterCode",
        sources=["FasterCode.pyx"],
        libraries=["irina"],
        library_dirs=[IRINA_LIB_DIR, BASE_DIR],
        include_dirs=[BASE_DIR, os.path.join(BASE_DIR, "irina")],
        extra_compile_args=["-O2", "-DNDEBUG", "-fno-strict-aliasing", "-Wno-unreachable-code"],
        language="c",
    )
]

setup(
    name="FasterCode",
    version="1.0.0",
    description="High-performance Cython bindings for Irina chess engine (macOS/clang)",
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": False,
            "initializedcheck": False,
            "cdivision": True,
        },
        annotate=False,
    ),
    zip_safe=False,
)
