from conan import ConanFile, conan_version
from conan.tools.env import Environment
from conan.tools.files import replace_in_file
from conan.tools.scm import Version
from pathlib import Path
from contextlib import contextmanager
from zipfile import ZipInfo, ZipFile
from pip._vendor.distlib.resources import finder
from pip._vendor.distlib.util import FileOperator, get_platform
from io import BytesIO

import os
import pathlib
import sys
import json
import textwrap
import itertools
import operator
import subprocess
import struct
import binascii
import codecs
import time
import re


class PythonVirtualEnvironmentPackage(ConanFile):
    name = "pyvenv"
    version = "0.2.0"
    url = "https://github.com/samuel-emrys/pyvenv.git"
    homepage = "https://github.com/samuel-emrys/pyvenv.git"
    license = "MIT"
    description = "A python_requires library providing a management class for python virtual environments and a CMake generator to expose executables in those virtual environments as CMake targets"
    topics = ("Python", "Virtual Environment", "CMake", "venv")
    no_copy_source = True
    package_type = "python-require"


def args_to_string(args):
    """
    Convert a list of arguments to a command line string in an operating system agnostic way

    :param args: A list of the arguments to provide to the command line
    :type args: list(str)
    """
    if not args:
        return ""
    if sys.platform == 'win32':
        return subprocess.list2cmdline(args)
    else:
        return " ".join("'" + arg.replace("'", r"'\''") + "'" for arg in args)

def _which(files, paths, access=os.F_OK | os.X_OK):
    """
    Mostly like shutil.which, but allows searching for alternate filenames,
    and never falls back to %PATH% or curdir
    """
    if isinstance(files, str):
        files = [files]
    if sys.platform == "win32":
        pathext = os.environ.get("PATHEXT", "").split(os.pathsep)

        def expand_pathext(cmd):
            if any(cmd.lower().endswith(ext.lower()) for ext in pathext):
                yield cmd  # already has an extension, so check only that one
            else:
                yield from (cmd + ext for ext in pathext)  # check all possibilities

        files = [x for cmd in files for x in expand_pathext(cmd)]

        # Windows filesystems are (usually) case-insensitive, so match might be spelled differently than the searched name
        # And in particular, the extensions from PATHEXT are usually uppercase, and yet the real file seldom is.
        # Using pathlib.resolve() for now because os.path.realpath() was a no-op on win32
        # until nt symlink support landed in python 3.9 (based on GetFinalPathNameByHandleW)
        # https://github.com/python/cpython/commit/75e064962ee0e31ec19a8081e9d9cc957baf6415
        #
        # realname() canonicalizes *only* the searched-for filename, but keeps the caller-provided path verbatim:
        # they might have been short paths, or via some symlink, and that's fine

        def realname(file):
            path = Path(file)
            realname = path.resolve(strict=True).name
            return str(path.with_name(realname))

    else:

        def realname(path):
            return path  # no-op

    for path in paths:
        for file in files:
            filepath = os.path.join(path, file)
            if (
                os.path.exists(filepath)
                and os.access(filepath, access)
                and not os.path.isdir(filepath)
            ):  # is executable
                return realname(filepath)
    return None

def _default_python():
    """
    Identify the default python interpreter.
    """
    base_exec_prefix = sys.base_exec_prefix

    if hasattr(
        sys, "real_prefix"
    ):  # in a virtualenv, which sets this instead of base_exec_prefix like venv
        base_exec_prefix = getattr(sys, "real_prefix")

    if sys.exec_prefix != base_exec_prefix:  # alread running in a venv
        # we want to create the new virtualenv off the base python installation,
        # rather than create a grandchild (child of of the current venv)
        names = [os.path.basename(sys.executable), "python3", "python"]

        prefixes = [base_exec_prefix]

        suffixes = ["bin", "Scripts"]
        exec_prefix_suffix = os.path.relpath(
            os.path.dirname(sys.executable), sys.exec_prefix
        )  # e.g. bin or Scripts
        if exec_prefix_suffix and exec_prefix_suffix != ".":
            suffixes.insert(0, exec_prefix_suffix)

        def add_suffix(prefix, suffixes):
            yield prefix
            yield from (os.path.join(prefix, suffix) for suffix in suffixes)

        dirs = [x for prefix in prefixes for x in add_suffix(prefix, suffixes)]
        return _which(names, dirs)
    else:
        return sys.executable

def _write_activate_this(env_dir, bin_dir, lib_dirs):
    """
    Write an activate_this.py to env_dir. This fills a gap where this isn't
    created by default by the `venv` module. This borrows the implementation from `virtualenv`.

    :param env_dir: The path to the virtual environment directory
    :type env_dir: str
    :param bin_dir: The name of the platform specific binary directory. `Scripts` or `bin`.
    :type bin_dir: str
    :param lib_dirs: A list of the environment library directories to search when activate_this.py is used
    :type lib_dirs: list(str)
    """
    win_py2 = sys.platform == "win32" and sys.version_info.major == 2
    decode_path = ("yes" if win_py2 else "")
    lib_dirs = [os.path.relpath(libdir, os.path.join(env_dir, bin_dir)) for libdir in lib_dirs]
    lib_dirs = os.pathsep.join(lib_dirs)
    contents = textwrap.dedent(f"""\
        import os
        import site
        import sys

        try:
            abs_file = os.path.abspath(__file__)
        except NameError:
            raise AssertionError("You must use exec(open(this_file).read(), {{'__file__': this_file}}))")

        bin_dir = os.path.dirname(abs_file)
        base = bin_dir[: -len("{bin_dir}") - 1]  # strip away the bin part from the __file__, plus the path separator

        # prepend bin to PATH (this file is inside the bin directory)
        os.environ["PATH"] = os.pathsep.join([bin_dir] + os.environ.get("PATH", "").split(os.pathsep))
        os.environ["VIRTUAL_ENV"] = base  # virtual env is right above bin directory

        # add the virtual environments libraries to the host python import mechanism
        prev_length = len(sys.path)
        for lib in "{lib_dirs}".split(os.pathsep):
            path = os.path.realpath(os.path.join(bin_dir, lib))
            site.addsitedir(path.decode("utf-8") if "{decode_path}" else path)
        sys.path[:] = sys.path[prev_length:] + sys.path[0:prev_length]

        sys.real_prefix = sys.prefix
        sys.prefix = base
    """)

    with open(os.path.join(env_dir, bin_dir, "activate_this.py"), "w") as f:
        f.write(contents)

# build helper for making and managing python virtual environments
class PythonVirtualEnv:
    """
    A build helper for creating and managing python virtual environments
    """
    def __init__(self, conanfile, interpreter=_default_python(), env_folder=None):
        """
        Create a PythonVirtualEnv object

        :param conanfile: A reference to the conanfile invoking the PythonVirtualEnv object
        :type package: ConanFile
        :param interpreter: A path to the interpreter to use for the virtual environment. Defaults
        to the first python discovered on the PATH.
        :type package: str
        :param env_folder: The directory for the python virtual environment to manage or create. Defaults to `None`.
        :type package: str
        """
        self._conanfile = conanfile
        self.base_python = interpreter
        self.env_folder = env_folder
        self._debug = self._conanfile.output.debug if (Version(conan_version).major >= 2) else self._conanfile.output.info
        self._conanfile.output.info(f"Version = {Version(conan_version).major}")

    def create(self, folder, *, clear=True, symlinks=(os.name != "nt"), with_pip=True, requirements=[]):
        """
        Create a virtual environment
        symlink logic borrowed from python -m venv
        See venv.main() in /Lib/venv/__init__

        :param folder: The directory in which to create the virtual environment
        :type folder: str
        :param clear: Delete the contents of the environment directory if it already exists, before environment creation
        :type clear: str
        :param symlinks: Try to use symlinks rather than copies, when symlinks are not the default for the platform.
        Defaults to `False for windows, and `True` otherwise.
        :type symlinks: str
        :param with_pip: Install pip in the virtual environment. Defaults to `True`.
        :type with_pip: str
        :param requirements: A list of requirements to install in the virtual environment
        :type requirements: list(str)
        """
        self.env_folder = folder

        self._conanfile.output.info(
            f"creating venv at {self.env_folder} based on {self.base_python or '<conanfile>'}"
        )

        if self.base_python:
            # another alternative (if we ever wanted to support more customization) would be to launch
            # a `python -` subprocess and feed it the script text `import venv venv.EnvBuilder() ...` on stdin
            venv_options = ["--symlinks" if symlinks else "--copies"]
            if clear:
                venv_options.append("--clear")
            if not with_pip:
                venv_options.append("--without-pip")

            env = Environment()
            env.define("__PYVENV_LAUNCHER__", None)
            envvars = env.vars(self._conanfile, scope="build")
            envvars.save_script("build_python")
            self._conanfile.run(
                args_to_string(
                    [self.base_python, "-mvenv", *venv_options, self.env_folder]
                ),
                env="conanbuild"
            )
        else:
            # fallback to using the python this script is running in
            # (risks the new venv having an inadvertant dependency if conan itself is virtualized somehow, but it will *work*)
            import venv

            builder = venv.EnvBuilder(clear=clear, symlinks=symlinks, with_pip=with_pip)
            builder.create(self.env_folder)

        if requirements:
            self._conanfile.run(
                args_to_string([self.pip, "install", *requirements]),
                env="conanbuild"
            )

        _write_activate_this(env_dir=self.env_folder, bin_dir=self.bin_dir, lib_dirs=self.libpath)
        self.make_relocatable(env_folder=self.env_folder)

    def entry_points(self, package=None):
        """
        Retrieve the entry points available for a package
        :param package: The package to return entry points for. Default is `None`
        :type package: str
        """
        import importlib.metadata  # Python 3.8 or greater

        entry_points = itertools.chain.from_iterable(
            dist.entry_points
            for dist in importlib.metadata.distributions(
                name=package, path=self.libpath
            )
        )

        by_group = operator.attrgetter("group")
        ordered = sorted(entry_points, key=by_group)
        grouped = itertools.groupby(ordered, by_group)

        return {
            group: [x.name for x in entry_points] for group, entry_points in grouped
        }

    def setup_entry_points(self, package, folder, silent=False):
        """
        Add entry points for a package to the virtual environment. In most cases this shouldn't be required.

        :param package: The package to add entry points for
        :type package: str
        :param folder: The directory in which to configure the entry points.
        :type folder: str
        :param silent: Suppress log output. Default is `False`
        :type silent: bool
        """
        # create target folder
        try:
            os.makedirs(folder)
        except Exception:
            pass

        def copy_executable(name, target_folder, type):
            import shutil

            # locate script in venv
            try:
                path = self.which(name, required=True)
                self._conanfile.output.info(f"Found {name} at {path}")
            except FileNotFoundError as e:
                # avoid FileNotFound if the no launcher script for this name was found, or
                self._conanfile.output.warning(
                    f"pyvenv.setup_entry_points: FileNotFoundError: {e}"
                )
                return

            root, ext = os.path.splitext(path)
            self._conanfile.output.info(f"{name} split into {root}, {ext}")


            try:
                # copy venv script to target folder
                self._conanfile.output.info(f"Attempting to copy {path} to {target_folder}")
                shutil.copy2(path, target_folder)

                # copy entry point script
                # if it exists
                if type == "gui":
                    ext = "-script.pyw"
                else:
                    ext = "-script.py"

                entry_point_script = root + ext
                self._conanfile.output.info(f"Entry point script evaluated to {entry_point_script}")

                if os.path.isfile(entry_point_script):
                    self._conanfile.output.info(f"Attempting to copy {entry_point_script} to {target_folder}")
                    shutil.copy2(entry_point_script, target_folder)
            except shutil.SameFileError:
                # SameFileError if the launcher script is *already* in the target_folder
                # e.g. on posix systems the venv scripts are already in bin/
                if not silent:
                    self._conanfile.output.info(
                        f"pyvenv.setup_entry_points: command '{name}' already found in '{folder}'. Other entry_points may also be unintentionally visible."
                    )

        entry_points = self.entry_points(package)
        for name in entry_points.get("console_scripts", []):
            self._conanfile.output.info(f"Adding entry point for {name}")
            copy_executable(name, folder, type="console")
        for name in entry_points.get("gui_scripts", []):
            self._conanfile.output.info(f"Adding entry point for {name}")
            copy_executable(name, folder, type="gui")


    @property
    def _version(self):
        return "{}.{}".format(*sys.version_info)

    @property
    def _python_version(self):
        return f"python{self._version}"

    @property
    def _is_pypy(self):
        return hasattr(sys, "pypy_version_info")

    @property
    def _is_win(self):
        return sys.platform == "win32"

    @property
    def _abi_flags(self):
        return getattr(sys, "abiflags", "")

    @property
    def _no_shebang_scripts(self):
        return [
            "python",
            self._python_version,
            "activate",
            "activate.sh",
            "activate.bat",
            "activate_this.py",
            "activate.fish",
            "activate.csh",
            "activate.xsh",
            "activate.nu",
            "Activate.ps1",
        ]

    @property
    def bin_dir(self):
        return "Scripts" if self._is_win else "bin"

    @property
    def binpath(self):
        # this should be the same logic as as
        # context.bin_name = ... in venv.ensure_directories
        bindirs = [self.bin_dir]
        return [os.path.join(self.env_folder, x) for x in bindirs]

    @property
    def libpath(self):
        # this should be the same logic as as
        # libpath = ... in venv.ensure_directories
        if self._is_win:
            libpath = os.path.join(self.env_folder, "Lib", "site-packages")
        else:
            libpath = os.path.join(
                self.env_folder,
                "lib",
                "python%d.%d" % sys.version_info[:2],
                "site-packages",
            )
        return [libpath]

    # return the path to a command within the venv, None if only found outside
    def which(self, command, required=False, **kwargs):
        found = _which(command, self.binpath, **kwargs)
        if found:
            return found
        elif required:
            raise FileNotFoundError(
                f"command {command} not in venv binpath {os.pathsep.join(self.binpath)}"
            )
        else:
            return None

    @property
    def python(self):
        """
        Convenience wrapper for python. Can be used to avoid activating the environment when
        installing dependencies, i.e.:
        self.run(args_to_string([venv.python, "-mpip", "install", "sphinx"])
        """
        return self.which("python", required=True)

    @property
    def pip(self):
        """
        Convenience wrapper for pip. Ensure that this is used in conjunction with activate
        to ensure that the python interpreter inside the venv is used, i.e.
        with venv.activate():
            self.run(args_to_string([venv.pip, "install", "sphinx"])
        """
        return self.which("pip", required=True)

    @property
    def env(self):
        """
        environment variables like the usual venv `activate` script, i.e.
        with tools.environment_append(venv.env):
            ...
        """
        return {
            "__PYVENV_LAUNCHER__": None,  # this might already be set if conan was launched through a venv
            "PYTHONHOME": None,
            "VIRTUAL_ENV": self.env_folder,
            "PATH": self.binpath,
        }

    @contextmanager
    def activate(self):
        """
        Setup environment and add site_packages of this this venv to sys.path
        (importing from the venv only works if it contains python modules compatible
         with conan's python interrpreter as well as the venv one
        But they're generally the same per _default_python(), so this will let you try
        with venv.activate():
            ...
        """
        old_path = sys.path[:]
        sys.path.extend(self.libpath)
        env = Environment()
        for k, v in self.env.items():
            env.define(k, v)
        envvars = env.vars(self._conanfile, scope="build")
        with envvars.apply():
            yield
        sys.path = old_path

    def make_relocatable(self, env_folder):
        """
        Makes the already-existing environment use relative paths, and takes out
        the #!-based environment selection in scripts.

        This functionality does not make a virtual environment relocatable to any
        environment other than the one in which it was created in. This is only suitable
        for moving a venv directory to another directory in the same environment, such
        as would be achieved with `mv venv venv2`.

        This functionality is _NOT_ suitable for transplanting a venv for usage in a
        deployment environment, where it is on a different physical machine, with a
        different python interpreter, or different underlying library requirements, such
        as GLIBC. It is only suitable for usage within the same environment in which it
        was built.

        In a conan context, the resulting virtualenv should not be uploaded to a server.
        The `build_policy="missing"` and `upload_policy="skip"` attributes should be set.

        Derived from https://github.com/pypa/virtualenv/blob/fb6e546cc1dfd0d363dc4d769486805d2d8f04bc/virtualenv.py#L1890-L1903
        MIT License

        :param env_folder: The path to the virtual environment to make relocatable
        :type env_folder: str
        """
        home_dir, lib_dir, inc_dir, bin_dir = self._path_locations(env_folder)
        activate_this = os.path.join(bin_dir, "activate_this.py")
        if not os.path.exists(activate_this):
            self._conanfile.output.error(
                f"The environment doesn't have a file {activate_this} -- please re-run virtualenv " "on this environment to update it"
            )
        patcher = ScriptPatcher(bin_dir, bin_dir, self._conanfile)
        patcher.patch_scripts()
        # self._fixup_scripts(bin_dir)
        # self._fixup_executables(bin_dir)
        self._fixup_pth_and_egg_link(home_dir)
        self._patch_activate_scripts(bin_dir)

    def _path_locations(self, home_dir):
        """
        Return the path locations for the environment (where libraries are,
        where scripts go, etc)
        Derived from https://github.com/pypa/virtualenv/blob/fb6e546cc1dfd0d363dc4d769486805d2d8f04bc/virtualenv.py#L1199-L1241
        MIT License
        :param home_dir: The base path to the virtual environment
        :type home_dir: str
        """
        home_dir = pathlib.Path(home_dir).absolute()
        lib_dir, inc_dir, bin_dir = None, None, None
        # XXX: We'd use distutils.sysconfig.get_python_inc/lib but its
        # prefix arg is broken: http://bugs.python.org/issue3386
        if self._is_win:
            # Windows has lots of problems with executables with spaces in
            # the name; this function will remove them (using the ~1
            # format):
            home_dir.mkdir(parents=True, exist_ok=True)
            if " " in str(home_dir):
                import ctypes

                get_short_path_name = ctypes.windll.kernel32.GetShortPathNameW
                size = max(len(str(home_dir)) + 1, 256)
                buf = ctypes.create_unicode_buffer(size)
                try:
                    # noinspection PyUnresolvedReferences
                    u = unicode
                except NameError:
                    u = str
                ret = get_short_path_name(u(home_dir), buf, size)
                if not ret:
                    print('Error: the path "{}" has a space in it'.format(home_dir))
                    print("We could not determine the short pathname for it.")
                    print("Exiting.")
                    sys.exit(3)
                home_dir = str(buf.value)
            lib_dir = os.path.join(home_dir, "Lib")
            inc_dir = os.path.join(home_dir, "Include")
            bin_dir = os.path.join(home_dir, "Scripts")
        if self._is_pypy:
            lib_dir = home_dir
            inc_dir = os.path.join(home_dir, "include")
            bin_dir = os.path.join(home_dir, "bin")
        elif not self._is_win:
            lib_dir = os.path.join(home_dir, "lib", self._python_version)
            inc_dir = os.path.join(home_dir, "include", self._python_version + self._abi_flags)
            bin_dir = os.path.join(home_dir, "bin")
        return home_dir, lib_dir, inc_dir, bin_dir

    def _fixup_scripts(self, bin_dir):
        """
        Replaces the shebang (or windows equivalent) with a relative python path and invocation of activate_this.py
        Derived from https://github.com/pypa/virtualenv/blob/fb6e546cc1dfd0d363dc4d769486805d2d8f04bc/virtualenv.py#L1919-L1966
        MIT License
        :param bin_dir: The path to the virtual environment binary directory
        :type bin_dir: str
        """
        if self._is_win:
            new_shebang_args = ("{} /c".format(os.path.normcase(os.environ.get("COMSPEC", "cmd.exe"))), "", ".exe")
        else:
            new_shebang_args = ("/usr/bin/env", self._version, "")

        # This is what we expect at the top of scripts:
        shebang = "#!{}".format(
            os.path.normcase(os.path.join(os.path.abspath(bin_dir), "python{}".format(new_shebang_args[2])))
        )
        # This is what we'll put:
        new_shebang = "#!{} python{}{}".format(*new_shebang_args)

        for filename in os.listdir(bin_dir):
            filename = os.path.join(bin_dir, filename)
            if not os.path.isfile(filename):
                # ignore child directories, e.g. .svn ones.
                continue
            with open(filename, "rb") as f:
                try:
                    lines = f.read().decode("utf-8").splitlines()
                except UnicodeDecodeError:
                    # This is probably a binary program instead
                    # of a script, so just ignore it.
                    continue
            if not lines:
                self._conanfile.output.warning(f"Script {filename} is an empty file")
                continue

            old_shebang = lines[0].strip()
            old_shebang = old_shebang[0:2] + os.path.normcase(old_shebang[2:])

            if not old_shebang.startswith(shebang):
                if os.path.basename(filename) in self._no_shebang_scripts:
                    # These scripts will be patched by in a separate stage by self._patch_activate_scripts
                    continue
                elif lines[0].strip() == new_shebang:
                    self._debug(f"Script {filename} has already been made relative")
                else:
                    self._conanfile.output.warning(
                        f"Script {filename} cannot be made relative (it's not a normal script that starts with {shebang})"
                    )
                continue
            self._conanfile.output.info(f"Making script {filename} relative")
            script = self._relative_script([new_shebang] + lines[1:])
            with open(filename, "wb") as f:
                f.write("\n".join(script).encode("utf-8"))

    def _fixup_executables(self, bin_dir):

        if not self._is_win:
            # Unix binaries don't need to be fixed
            return
        decode_hex = codecs.getdecoder("hex_codec")
        encode_hex = codecs.getencoder("hex_codec")
        interpreter = "python.exe"
        dont_patch_files = [
            interpreter,
            "pythonw.exe",
        ]

        # The shebang line replacement
        shebang_search = f"#!{os.path.normcase(os.path.join(bin_dir, interpreter))}".encode("utf-8")
        self._debug(f"{shebang_search=}")
        cmd = os.path.normcase(os.path.join('C:\\', 'Windows', 'system32', 'cmd.exe'))
        shebang_replace = f"#!{cmd} /c {interpreter}".encode("utf-8")
        self._debug(f"{shebang_replace=}")

        # Pull in the activation script to run when executing the executable
        #utf_header = "-*- coding: utf-8 -*-".encode("utf-8")
        search_string = "import sys".encode("utf-8")
        activate = (
            "import os; "
            "activate_this=os.path.join(os.path.dirname(os.path.realpath(__file__)), 'activate_this.py'); "
            "print(activate_this); "
            "exec(compile(open(activate_this).read(), activate_this, 'exec'), { '__file__': activate_this}); "
            "del os, activate_this"
        )
        activation_insertion = f"".encode("utf-8")
        #activation_insertion = f"{activate}\n{search_string.decode('utf-8')}".encode("utf-8")
        #activation_insertion = f"{activate}\n{utf_header.decode('utf-8')}".encode("utf-8")
        #activation_insertion = f"{utf_header.decode('utf-8')}".encode("utf-8")
        self._debug(f"{activation_insertion=}")

        for filename in os.listdir(bin_dir):
            filename = os.path.join(bin_dir, filename)
            if not os.path.isfile(filename):
                continue
            if ".exe" in filename and filename not in dont_patch_files:
                self._conanfile.output.info(f"Making {filename} relative")
                contents_hex = []
                with open(filename, "rb") as f:
                    for chunk in iter(lambda: f.read(32), b''):
                        contents_hex.append(binascii.hexlify(chunk))
                # Combine all binary contents into one big byte string instead of a [(content, size), (content,size)]
                contents = b"".join([decode_hex(element)[0] for element in contents_hex])

                if b"system32" in contents:
                    self._debug(f"{filename} has already been patched.")
                    continue
                # Find and replace in big byte string
                # patched = contents.replace(shebang_search, shebang_replace)
                patched = contents.replace(search_string, activation_insertion)
                # Reconstruct the binary form of the file before re-writing it out
                patched_chunked = [patched[i:i+32] for i in range(0, len(patched), 32)]
                patched_hex = [encode_hex(element)[0] for element in patched_chunked]

                with open(filename, "wb") as f:
                    for chunk in patched_hex:
                        f.write(binascii.unhexlify(chunk))


    def _relative_script(self, lines):
        """
        Return a script that'll work in a relocatable environment.
        Derived from https://github.com/pypa/virtualenv/blob/fb6e546cc1dfd0d363dc4d769486805d2d8f04bc/virtualenv.py#L1969-L1987
        MIT License

        :param lines: Raw contents of the script to patch
        :type lines: list(str)
        """
        activate = (
            "import os; "
            "activate_this=os.path.join(os.path.dirname(os.path.realpath(__file__)), 'activate_this.py'); "
            "exec(compile(open(activate_this).read(), activate_this, 'exec'), { '__file__': activate_this}); "
            "del os, activate_this"
        )
        # Find the last future statement in the script. If we insert the activation
        # line before a future statement, Python will raise a SyntaxError.
        activate_at = None
        for idx, line in reversed(list(enumerate(lines))):
            if line.split()[:3] == ["from", "__future__", "import"]:
                activate_at = idx + 1
                break
        if activate_at is None:
            # Activate after the shebang.
            activate_at = 1
        return lines[:activate_at] + ["", activate, ""] + lines[activate_at:]



    def _fixup_pth_and_egg_link(self, home_dir, sys_path=None):
        """
        Makes .pth and .egg-link files use relative paths
        Derived from https://github.com/pypa/virtualenv/blob/fb6e546cc1dfd0d363dc4d769486805d2d8f04bc/virtualenv.py#L1990-L2015
        MIT License

        :param home_dir: The base path to the virtual environment
        :type home_dir: str
        :param sys_path: The system path to use
        :type sys_path: str
        """
        home_dir = os.path.normcase(os.path.abspath(home_dir))
        if sys_path is None:
            sys_path = sys.path
        for a_path in sys_path:
            if not a_path:
                a_path = "."
            if not os.path.isdir(a_path):
                continue
            a_path = os.path.normcase(os.path.abspath(a_path))
            if not a_path.startswith(home_dir):
                self._debug(f"Skipping system (non-environment) directory {a_path}")
                continue
            for filename in os.listdir(a_path):
                filename = os.path.join(a_path, filename)
                if filename.endswith(".pth"):
                    if not os.access(filename, os.W_OK):
                        self._conanfile.output.warning(f"Cannot write .pth file {filename}, skipping")
                    else:
                        self._fixup_pth_file(filename)
                if filename.endswith(".egg-link"):
                    if not os.access(filename, os.W_OK):
                        self._conanfile.output.warning(f"Cannot write .egg-link file {filename}, skipping")
                    else:
                        self._fixup_egg_link(filename)


    def _fixup_pth_file(self, filename):
        """
        Derived from https://github.com/pypa/virtualenv/blob/fb6e546cc1dfd0d363dc4d769486805d2d8f04bc/virtualenv.py#L2018-L2036
        MIT License

        :param filename: Path to the file to fix
        :type filename: str
        """
        lines = []
        with open(filename) as f:
            prev_lines = f.readlines()
        for line in prev_lines:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("import ") or os.path.abspath(line) != line:
                lines.append(line)
            else:
                new_value = self._make_relative_path(filename, line)
                if line != new_value:
                    self._debug("Rewriting path {} as {} (in {})".format(line, new_value, filename))
                lines.append(new_value)
        if lines == prev_lines:
            self._conanfile.output.info(f"No changes to .pth file {filename}")
            return
        self._conanfile.output.info(f"Making paths in .pth file {filename} relative")
        with open(filename, "w") as f:
            f.write("\n".join(lines) + "\n")


    def _fixup_egg_link(self, filename):
        """
        Derived from https://github.com/pypa/virtualenv/blob/fb6e546cc1dfd0d363dc4d769486805d2d8f04bc/virtualenv.py#L2039-L2048
        MIT License

        :param filename: Path to the file to fix
        :type filename: str
        """
        with open(filename) as f:
            link = f.readline().strip()
        if os.path.abspath(link) != link:
            self._debug(f"Link in {filename} already relative")
            return
        new_link = self._make_relative_path(filename, link)
        self._conanfile.output.info("Rewriting link {} in {} as {}".format(link, filename, new_link))
        with open(filename, "w") as f:
            f.write(new_link)


    def _make_relative_path(self, source, dest, dest_is_directory=True):
        """
        Make a filename relative, where the filename is dest, and it is
        being referred to from the filename source.
        Derived from https://github.com/pypa/virtualenv/blob/fb6e546cc1dfd0d363dc4d769486805d2d8f04bc/virtualenv.py#L2051-L2084
        MIT License
            >>> make_relative_path('/usr/share/something/a-file.pth',
            ...                    '/usr/share/another-place/src/Directory')
            '../another-place/src/Directory'
            >>> make_relative_path('/usr/share/something/a-file.pth',
            ...                    '/home/user/src/Directory')
            '../../../home/user/src/Directory'
            >>> make_relative_path('/usr/share/a-file.pth', '/usr/share/')
            './'

        :param source: The path from which the filename will be referred to
        :type source: str
        :param dest: The filename to be referred to from `source`
        :type dest: str
        :param dest_is_directory: Flag indicating whether `dest` is a directory. Defaults to `True`
        :type dest: bool
        """
        source = os.path.dirname(source)
        if not dest_is_directory:
            dest_filename = os.path.basename(dest)
            dest = os.path.dirname(dest)
        else:
            dest_filename = None
        dest = os.path.normpath(os.path.abspath(dest))
        source = os.path.normpath(os.path.abspath(source))
        dest_parts = dest.strip(os.path.sep).split(os.path.sep)
        source_parts = source.strip(os.path.sep).split(os.path.sep)
        while dest_parts and source_parts and dest_parts[0] == source_parts[0]:
            dest_parts.pop(0)
            source_parts.pop(0)
        full_parts = [".."] * len(source_parts) + dest_parts
        if not dest_is_directory and dest_filename is not None:
            full_parts.append(dest_filename)
        if not full_parts:
            # Special case for the current directory (otherwise it'd be '')
            return "./"
        return os.path.sep.join(full_parts)

    def _patch_activate_scripts(self, bin_dir):
        """
        Patch the virtualenvs activation scripts such that they discover the virtualenv path dynamically
        rather than hardcoding it to make them robust to relocation.

        :param bin_dir: The path to the folder in which the activation scripts to be patched lie.
        :type bin_dir: str
        """

        scripts = {
            pathlib.Path(bin_dir, "activate"): self._patch_activate,
            pathlib.Path(bin_dir, "activate.sh"): self._patch_activate,
            pathlib.Path(bin_dir, "activate.bat"): self._patch_activate_bat,
            pathlib.Path(bin_dir, "activate.fish"): self._patch_activate_fish,
            pathlib.Path(bin_dir, "activate.csh"): self._patch_activate_csh,
            #pathlib.Path(bin_dir, "activate.xsh"): self._patch_activate_xsh,
            #pathlib.Path(bin_dir, "activate.nu"): self._patch_activate_nu,
        }

        for script, patch in scripts.items():
            if script.is_file():
                self._conanfile.output.info(f"Making {script} relocatable.")
                patch(script)

    def _patch_activate(self, activate):
        """
        Patch a bash, sh, ksh, zsh or dash script such that it discovers the virtualenv path dynamically
        rather than hardcoding it to make it robust to relocation.
        Derived from https://github.com/jpenney/virtualenv/blob/611c4b4ff33540d84fb876daf7fddf460adfee20/virtualenv_embedded/activate.sh

        :param activate: Path to the script to patch
        :type activate: str
        """

        with open(activate) as f:
            if "ACTIVATE_PATH_FALLBACK" in f.read():
                self._debug(f"{activate} has already been made relocatable. Continuing.")
                return

        replace_in_file(
            self._conanfile,
            file_path=activate, 
            search='deactivate () {',
            replace=textwrap.dedent('''\
                ACTIVATE_PATH_FALLBACK="$_"
                deactivate () {
                '''
            ),
        )

        replace_in_file(
            self._conanfile,
            file_path=activate, 
            search=f'VIRTUAL_ENV="{os.path.abspath(self.env_folder)}"',
            replace=textwrap.dedent(f'''\
                # Attempt to determine VIRTUAL_ENV in relocatable way
                if [ ! -z "${{BASH_SOURCE:-}}" ]; then
                    # bash
                    ACTIVATE_PATH="${{BASH_SOURCE}}"
                elif [ ! -z "${{DASH_SOURCE:-}}" ]; then
                    # dash
                    ACTIVATE_PATH="${{DASH_SOURCE}}"
                elif [ ! -z "${{ZSH_VERSION:-}}" ]; then
                    # zsh
                    ACTIVATE_PATH="$0"
                elif [ ! -z "${{KSH_VERSION:-}}" ] || [ ! -z "${{.sh.version:}}" ]; then
                    # ksh - we have to use history, and unescape spaces before quoting
                    ACTIVATE_PATH="$(history -r -l -n | head -1 | sed -e 's/^[\t ]*\(\.\|source\) *//;s/\\ / /g')"
                elif [ "$(basename "$ACTIVATE_PATH_FALLBACK")" == "activate.sh" ]; then
                    ACTIVATE_PATH="${{ACTIVATE_PATH_FALLBACK}}"
                else
                    ACTIVATE_PATH=""
                fi

                # Default to non-relocatable path
                VIRTUAL_ENV="{os.path.abspath(self.env_folder)}"
                if [ ! -z "${{ACTIVATE_PATH:-}}" ]; then
                    VIRTUAL_ENV="$(cd "$(dirname "${{ACTIVATE_PATH}}")/.."; pwd)"
                fi
                unset ACTIVATE_PATH
                unset ACTIVATE_PATH_FALLBACK
                '''
            ),
        )


    def _patch_activate_bat(self, activate):
        """
        Patch a bat script such that it discovers the virtualenv path dynamically
        rather than hardcoding it to make it robust to relocation.
        Derived from https://github.com/jpenney/virtualenv/blob/611c4b4ff33540d84fb876daf7fddf460adfee20/virtualenv_embedded/activate.bat

        :param activate: Path to the script to patch
        :type activate: str
        """
        with open(activate) as f:
            if "~dp0" in f.read():
                self._debug(f"{activate} has already been made relocatable. Continuing.")
                return
        substitution = textwrap.dedent('''\
                        pushd %~dp0..
                        set "VIRTUAL_ENV=%CD%"
                        popd
                        ''')
        # These search patterns account for the variations in the contents of activate.bat
        # based on whether it was created using `virtualenv venv` or `python -m venv venv`
        search_patterns = [f'set "VIRTUAL_ENV={os.path.abspath(self.env_folder)}"',
                           f'set VIRTUAL_ENV={os.path.abspath(self.env_folder)}']
        missed_patterns = 0

        for pattern in search_patterns:
            try:
                replace_in_file(
                    self._conanfile,
                    file_path=activate,
                    search=pattern,
                    replace=substitution,
                )
                break
            except Exception:
                missed_patterns += 1
                if missed_patterns == len(search_patterns):
                    self._conanfile.output.error(f"Couldn't find any of the following patterns in {activate}: {','.join('`' + pattern + '`' for pattern in search_patterns)}")

    def _patch_activate_fish(self, activate):
        """
        Patch a fish script such that it discovers the virtualenv path dynamically
        rather than hardcoding it to make it robust to relocation.
        Derived from https://github.com/jpenney/virtualenv/blob/611c4b4ff33540d84fb876daf7fddf460adfee20/virtualenv_embedded/activate.fish

        :param activate: Path to the script to patch
        :type activate: str
        """
        with open(activate) as f:
            if "dirname" in f.read():
                self._debug(f"{activate} has already been made relocatable. Continuing.")
                return

        replace_in_file(
            self._conanfile,
            file_path=activate, 
            search=f'set -gx VIRTUAL_ENV "{os.path.abspath(self.env_folder)}"',
            replace='set -gx VIRTUAL_ENV (cd (dirname (status -f)); cd ..; pwd)',
        )

    def _patch_activate_csh(self, activate):
        """
        Patch a csh script such that it discovers the virtualenv path dynamically
        rather than hardcoding it to make it robust to relocation.

        :param activate: Path to the script to patch
        :type activate: str
        """
        with open(activate) as f:
            if "dirname" in f.read():
                self._debug(f"{activate} has already been made relocatable. Continuing.")
                return

        replace_in_file(
            self._conanfile,
            file_path=activate, 
            search=f'setenv VIRTUAL_ENV "{os.path.abspath(self.env_folder)}"',
            replace=textwrap.dedent('''\
                set scriptpath=`find /proc/$$/fd -type l -lname '*activate.csh' -printf '%l' | xargs dirname`
                setenv VIRTUAL_ENV `cd $scriptpath/.. && pwd`
                '''
            ),
        )

    def _patch_activate_xsh(self, activate):
        """
        Patch a xonsh script such that it discovers the virtualenv path dynamically
        rather than hardcoding it to make it robust to relocation.

        :param activate: Path to the script to patch
        :type activate: str
        """
        self._conanfile.output.error(f"No patch algorithm for 'xonsh' is available to patch {activate}. Contributions are welcome. Unable to make relocatable.")
        return

    def _patch_activate_nu(self, activate):
        """
        Patch a nushell script such that it discovers the virtualenv path dynamically
        rather than hardcoding it to make it robust to relocation.

        :param activate: Path to the script to patch
        :type activate: str
        """
        self._conanfile.output.error(f"No patch algorithm for 'nu' is available to patch {activate}. Contributions are welcome. Unable to make relocatable.")
        return

class ScriptPatcher:

    def __init__(self, source_dir, target_dir, conanfile, add_launchers=True, fileop=None):
        self.source_dir = source_dir
        self.target_dir = target_dir
        self.add_launchers = add_launchers
        self.clobber = True
        self.set_mode = (os.name == 'posix') or (os.name == 'java' and os._name == 'posix')
        self._fileop = fileop or FileOperator()
        self._is_nt = os.name == 'nt' or (os.name == 'java' and os._name == 'nt')
        self._conanfile = conanfile

    @property
    def _version(self):
        return "{}.{}".format(*sys.version_info)

    def _read_contents(self, file):
        # Read in the contents of the script
        if ".exe" in file and self._is_nt:
            # Fortunately the exe's can be read as zip files
            zip_contents = ZipFile(file)
            with zip_contents.open("__main__.py") as zf:
                contents = zf.read().decode("utf-8")
        else:
            with open(file) as f:
                contents = f.read()
        return contents

    def _build_shebang(self, executable, interpreter, executable_args=[], interpreter_args=[]):
        executable_args = "" if not executable_args else f" {' '.join(executable_args)}"
        interpreter_args = "" if not interpreter_args else f" {' '.join(interpreter_args)}"
        return f"#!{executable}{executable_args} {interpreter}{interpreter_args}\n"

    def _make_shebang(self):
        if self._is_nt:
            executable = os.path.normcase(os.environ.get("COMSPEC", "cmd.exe"))
            executable_args = ["/c"]
            interpreter = "python.exe"
            interpreter_args = []
        else:
            executable = "/usr/bin/env"
            executable_args = []
            interpreter = f"python{self._version}"
            interpreter_args = []

        return self._build_shebang(executable, interpreter, executable_args, interpreter_args)

    def _remove_shebang(self, contents):
        shebang_pattern = re.compile(r'^#!.*$')
        contents = "\n".join([line for line in contents.splitlines() if not shebang_pattern.match(line)])
        return contents

    def _patch_contents(self, contents):
        # Patch the contents of a file

        search_string = "import re"
        # file needs to account for the fact that __file__ is within a zip file
        # on windows but not on *nix
        activate = (
            "import os; "
            "import sys; "
            "file=os.path.dirname(os.path.realpath(__file__)) if sys.platform=='win32' else os.path.realpath(__file__); "
            "activate_this=os.path.join(os.path.dirname(file), 'activate_this.py'); "
            "exec(compile(open(activate_this).read(), activate_this, 'exec'), { '__file__': activate_this}); "
            "del os, sys, file, activate_this"
        )
        substitution_string = f"{activate}\n{search_string}"
        # Insert activate_this
        #contents = contents.replace(search_string, substitution_string)
        contents = activate + "\n" + contents
        return contents

    def _write_script(self, name, shebang, script_bytes, ext=None):
        # We only want to patch a file with a launcher if it's already using a
        # launcher (i.e., a .exe file. Leave .py files alone)
        use_launcher = self.add_launchers and self._is_nt and name.endswith('.exe')
        linesep = os.linesep.encode('utf-8')
        if not shebang.endswith(linesep):
            shebang += linesep
        if not use_launcher:
            script_bytes = shebang + script_bytes
        else:  # pragma: no cover
            if ext == 'py':
                launcher = self._get_launcher('t')
            else:
                launcher = self._get_launcher('w')
            stream = BytesIO()
            with ZipFile(stream, 'w') as zf:
                source_date_epoch = os.environ.get('SOURCE_DATE_EPOCH')
                if source_date_epoch:
                    date_time = time.gmtime(int(source_date_epoch))[:6]
                    zinfo = ZipInfo(filename='__main__.py', date_time=date_time)
                    zf.writestr(zinfo, script_bytes)
                else:
                    zf.writestr('__main__.py', script_bytes)
            zip_data = stream.getvalue()
            script_bytes = launcher + shebang + zip_data

        outname = os.path.join(self.target_dir, name)
        if use_launcher:  # pragma: no cover
            n, e = os.path.splitext(outname)
            if e.startswith('.py') or e.startswith('.exe'):
                outname = n
            outname = '%s.exe' % outname
            try:
                self._conanfile.output.info(f"Writing {outname=}")
                self._fileop.write_binary_file(outname, script_bytes)
            except Exception:
                # Failed writing an executable - it might be in use.
                self._conanfile.output.warning('Failed to write executable - trying to '
                               'use .deleteme logic')
                dfname = '%s.deleteme' % outname
                if os.path.exists(dfname):
                    os.remove(dfname)       # Not allowed to fail here
                os.rename(outname, dfname)  # nor here
                self._fileop.write_binary_file(outname, script_bytes)
                self._conanfile.output.debug('Able to replace executable using '
                             '.deleteme logic')
                try:
                    os.remove(dfname)
                except Exception:
                    pass    # still in use - ignore error
        else:
            if self._is_nt and not outname.endswith('.' + ext):  # pragma: no cover
                outname = '%s.%s' % (outname, ext)
                self._conanfile.output.info(f"Renaming {outname=}")
            if os.path.exists(outname) and not self.clobber:
                self._conanfile.output.warning('Skipping existing file %s', outname)
                return
            self._conanfile.output.info(f"Writing {outname=}")
            self._fileop.write_binary_file(outname, script_bytes)
            if self.set_mode:
                self._fileop.set_executable_mode([outname])

    if os.name == 'nt' or (os.name == 'java' and os._name == 'nt'):  # pragma: no cover
        # Executable launcher support.
        # Launchers are from https://bitbucket.org/vinay.sajip/simple_launcher/

        def _get_launcher(self, kind):
            if struct.calcsize('P') == 8:   # 64-bit
                bits = '64'
            else:
                bits = '32'
            platform_suffix = '-arm' if get_platform() == 'win-arm64' else ''
            name = '%s%s%s.exe' % (kind, bits, platform_suffix)
            # Use distlib from pip
            # Issue 31 in distlib repo isn't a concern, we don't need dynamic
            # discovery
            distlib_package = 'pip._vendor.distlib'
            resource = finder(distlib_package).find(name)
            if not resource:
                msg = ('Unable to find resource %s in package %s' % (name,
                       distlib_package))
                raise ValueError(msg)
            return resource.bytes

    def _patch_script(self, filename):
        contents = self._read_contents(filename)
        if "activate_this" in contents:
            self._conanfile.output.info(f"{filename} has already been patched")
            return 
        contents = self._remove_shebang(contents)
        shebang = self._make_shebang().encode("utf-8")
        script = self._patch_contents(contents).encode("utf-8")
        ext = "py"
        self._write_script(filename, shebang, script, ext)

    @property
    def _version(self):
        return "{}.{}".format(*sys.version_info)

    @property
    def _python_version(self):
        return f"python{self._version}"

    # Public API follows

    def patch(self, filename):
        """
        Patch a script.
        """
        self._patch_script(filename)

    def patch_scripts(self):
        interpreter = "python"
        dont_patch_files = [
            "python",
            "python.exe",
            "python3.exe",
            "pythonw.exe",
            self._python_version,
            "activate",
            "activate.sh",
            "activate.bat",
            "activate_this.py",
            "activate.fish",
            "activate.csh",
            "activate.xsh",
            "activate.nu",
            "Activate.ps1",
            "deactivate.bat",
        ]
        for filename in os.listdir(self.source_dir):
            filename = os.path.join(self.source_dir, filename)
            if not os.path.isfile(filename):
                continue
            if os.path.basename(filename) not in dont_patch_files:
                self.patch(filename)

